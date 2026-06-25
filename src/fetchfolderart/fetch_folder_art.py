#!/usr/bin/env python3
"""Fetch missing folder.jpg artwork for album folders.

This script scans a local or mapped NAS music tree, reads album metadata with
mutagen, searches MusicBrainz by artist and album, and downloads front cover art
from the Cover Art Archive. It never modifies audio files.
"""

from __future__ import annotations

import argparse
import csv
import fnmatch
import io
import os
import re
import sys
import time
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path
from typing import Iterable, Optional


SCRIPT_VERSION = "1.0.2"
PACKAGE_DIR = Path(__file__).resolve().parent
SOURCE_ROOT = PACKAGE_DIR.parent.parent
APP_DIR = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else (
    SOURCE_ROOT if PACKAGE_DIR.parent.name == "src" else PACKAGE_DIR
)
DATA_DIR = APP_DIR / "data"
MUSICBRAINZ_RELEASE_SEARCH_URL = "https://musicbrainz.org/ws/2/release/"
COVER_ART_FRONT_URL = "https://coverartarchive.org/release/{mbid}/front"
ITUNES_SEARCH_URL = "https://itunes.apple.com/search"
DEEZER_ALBUM_SEARCH_URL = "https://api.deezer.com/search/album"
DISCOGS_DATABASE_SEARCH_URL = "https://api.discogs.com/database/search"
DISCOGS_TOKEN_ENV_VAR = "DISCOGS_TOKEN"

AUDIO_EXTENSIONS = {
    ".mp3",
    ".flac",
    ".m4a",
    ".mp4",
    ".ogg",
    ".opus",
    ".wma",
}

SKIP_ART_NAMES = {"folder.jpg", "cover.jpg", "front.jpg", "album.jpg"}
DEFAULT_USER_AGENT = (
    f"fetch-folder-art/{SCRIPT_VERSION} "
    "(local folder artwork script; set --user-agent with contact info)"
)

requests = None
MutagenFile = None
Image = None
UnidentifiedImageError = Exception


def get_discogs_token() -> str:
    token = os.environ.get(DISCOGS_TOKEN_ENV_VAR, "").strip()
    if token:
        return token

    if os.name != "nt":
        return ""

    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            value, _value_type = winreg.QueryValueEx(key, DISCOGS_TOKEN_ENV_VAR)
    except OSError:
        return ""

    return str(value).strip()


def _broadcast_environment_change() -> None:
    if os.name != "nt":
        return

    try:
        import ctypes

        ctypes.windll.user32.SendMessageTimeoutW(
            0xFFFF,
            0x001A,
            0,
            "Environment",
            0x0002,
            5000,
            None,
        )
    except Exception:
        pass


def set_discogs_token(token: str) -> None:
    token = token.strip()
    if not token:
        clear_discogs_token()
        return

    os.environ[DISCOGS_TOKEN_ENV_VAR] = token
    if os.name == "nt":
        import winreg

        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_SET_VALUE) as key:
            winreg.SetValueEx(key, DISCOGS_TOKEN_ENV_VAR, 0, winreg.REG_SZ, token)
        _broadcast_environment_change()


def clear_discogs_token() -> None:
    os.environ.pop(DISCOGS_TOKEN_ENV_VAR, None)
    if os.name != "nt":
        return

    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, DISCOGS_TOKEN_ENV_VAR)
    except OSError:
        pass
    _broadcast_environment_change()


@dataclass
class AlbumFolder:
    path: Path
    audio_files: list[Path]


@dataclass
class AlbumMetadata:
    album: str
    artist: str
    year: str
    source: str


@dataclass
class ReleaseCandidate:
    mbid: str
    title: str
    artist_credit: str
    date: str
    score: int


@dataclass
class ArtworkResult:
    source: str
    matched_artist: str
    matched_album: str
    score: int
    image_url: str
    mbid: str = ""
    error: str = ""


class MusicBrainzRateLimiter:
    """Simple one-request-per-second limiter for MusicBrainz calls."""

    def __init__(self, seconds: float = 1.05) -> None:
        self.seconds = seconds
        self._last_request = 0.0

    def wait(self) -> None:
        elapsed = time.monotonic() - self._last_request
        remaining = self.seconds - elapsed
        if remaining > 0:
            time.sleep(remaining)
        self._last_request = time.monotonic()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Recursively scan music folders and create missing folder.jpg files "
            "from MusicBrainz/Cover Art Archive."
        )
    )
    parser.add_argument(
        "root",
        help=r"Music root folder, for example M:\Music or \\NAS\Music.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be done without writing folder.jpg.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help=(
            "Fetch artwork even when folder.jpg or other common artwork files "
            "already exist. Existing folder.jpg may be overwritten."
        ),
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Process only the first N album folders found.",
    )
    parser.add_argument(
        "--log",
        default=str(DATA_DIR / "art_fetch_log.csv"),
        help=f"CSV log path. Default: {DATA_DIR / 'art_fetch_log.csv'}",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=20.0,
        help="HTTP timeout in seconds. Default: 20.",
    )
    parser.add_argument(
        "--retries",
        type=int,
        default=3,
        help="HTTP retry attempts for transient failures. Default: 3.",
    )
    parser.add_argument(
        "--candidates",
        type=int,
        default=5,
        help="MusicBrainz release candidates to try per folder. Default: 5.",
    )
    parser.add_argument(
        "--min-score",
        type=int,
        default=70,
        help="Minimum MusicBrainz search score to accept. Default: 70.",
    )
    parser.add_argument(
        "--user-agent",
        default=DEFAULT_USER_AGENT,
        help=(
            "User-Agent sent to MusicBrainz and Cover Art Archive. "
            "For regular use, include an app name/version and email or URL."
        ),
    )
    parser.add_argument(
        "--discogs-token",
        default=get_discogs_token(),
        help=(
            "Optional Discogs API token for the final fallback source. "
            "Can also be set with the DISCOGS_TOKEN environment variable."
        ),
    )
    parser.add_argument(
        "--pause",
        action="store_true",
        help="Wait for Enter before closing. Useful when launched by double-click.",
    )
    return parser.parse_args()


def load_dependencies() -> bool:
    global Image, MutagenFile, UnidentifiedImageError, requests

    missing = []

    try:
        import requests as requests_module
    except ImportError:
        missing.append("requests")
    else:
        requests = requests_module

    try:
        from mutagen import File as mutagen_file
    except ImportError:
        missing.append("mutagen")
    else:
        MutagenFile = mutagen_file

    try:
        from PIL import Image as pillow_image
        from PIL import UnidentifiedImageError as pillow_image_error
    except ImportError:
        missing.append("Pillow")
    else:
        Image = pillow_image
        UnidentifiedImageError = pillow_image_error

    if missing:
        print(
            "Missing required Python package(s): " + ", ".join(missing),
            file=sys.stderr,
        )
        print(
            "Install them with: python -m pip install -r requirements.txt",
            file=sys.stderr,
        )
        return False

    return True


def is_audio_file(path: Path) -> bool:
    return path.suffix.lower() in AUDIO_EXTENSIONS


def existing_art_files(folder: Path) -> list[Path]:
    found: list[Path] = []
    try:
        for child in folder.iterdir():
            if not child.is_file():
                continue
            name = child.name.lower()
            if name in SKIP_ART_NAMES or fnmatch.fnmatch(name, "albumart*.jpg"):
                found.append(child)
    except OSError as exc:
        print(f"  ! Could not inspect artwork files: {exc}")
    return found


def find_album_folders(root: Path, limit: Optional[int]) -> list[AlbumFolder]:
    album_folders: list[AlbumFolder] = []
    scanned_dirs = 0

    print(f"Scanning: {root}")
    for dirpath, _dirnames, filenames in os.walk(root):
        scanned_dirs += 1
        if scanned_dirs % 250 == 0:
            print(f"  scanned {scanned_dirs} folders, found {len(album_folders)} album folders...")

        folder = Path(dirpath)
        audio_files = [folder / name for name in filenames if is_audio_file(Path(name))]
        if audio_files:
            album_folders.append(AlbumFolder(path=folder, audio_files=audio_files))
            if limit is not None and len(album_folders) >= limit:
                break

    print(f"Found {len(album_folders)} album folders.")
    return album_folders


def first_value(values: object) -> str:
    if values is None:
        return ""
    if isinstance(values, (list, tuple)):
        for value in values:
            text = str(value).strip()
            if text:
                return text
        return ""
    return str(values).strip()


def read_easy_tag(tags: object, keys: Iterable[str]) -> str:
    if not tags:
        return ""
    for key in keys:
        try:
            value = tags.get(key)  # type: ignore[attr-defined]
        except AttributeError:
            value = None
        text = first_value(value)
        if text:
            return text
    return ""


def extract_year(date_text: str) -> str:
    match = re.search(r"\b(\d{4})\b", date_text or "")
    return match.group(1) if match else ""


def normalize_for_match(value: str) -> str:
    value = value.casefold()
    value = re.sub(r"&", " and ", value)
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return " ".join(value.split())


def text_score(expected: str, candidate: str) -> int:
    expected_norm = normalize_for_match(expected)
    candidate_norm = normalize_for_match(candidate)
    if not expected_norm or not candidate_norm:
        return 0
    if expected_norm == candidate_norm:
        return 100
    if expected_norm in candidate_norm or candidate_norm in expected_norm:
        return 92
    return round(SequenceMatcher(None, expected_norm, candidate_norm).ratio() * 100)


def metadata_match_score(
    metadata: AlbumMetadata,
    *,
    artist: str,
    album: str,
    year: str = "",
) -> int:
    album_score = text_score(metadata.album, album)
    artist_score = text_score(metadata.artist, artist) if metadata.artist else album_score
    score = round((album_score * 0.65) + (artist_score * 0.35))

    if metadata.year and year:
        try:
            year_delta = abs(int(metadata.year) - int(year))
        except ValueError:
            year_delta = 99
        if year_delta == 0:
            score = min(100, score + 5)
        elif year_delta <= 1:
            score = min(100, score + 2)
        elif year_delta >= 4:
            score = max(0, score - 10)

    return score


def most_common(values: Iterable[str]) -> str:
    cleaned = [value.strip() for value in values if value and value.strip()]
    if not cleaned:
        return ""
    return Counter(cleaned).most_common(1)[0][0]


def read_album_metadata(album_folder: AlbumFolder) -> AlbumMetadata:
    values: dict[str, list[str]] = defaultdict(list)

    for audio_path in album_folder.audio_files:
        try:
            audio = MutagenFile(audio_path, easy=True)
        except Exception as exc:  # Mutagen may raise parser-specific exceptions.
            print(f"  ! Could not read tags from {audio_path.name}: {exc}")
            continue

        if audio is None or not getattr(audio, "tags", None):
            continue

        tags = audio.tags
        values["album"].append(read_easy_tag(tags, ["album"]))
        values["albumartist"].append(
            read_easy_tag(tags, ["albumartist", "albumartistsort", "performer"])
        )
        values["artist"].append(read_easy_tag(tags, ["artist", "artistsort", "composer"]))
        values["date"].append(read_easy_tag(tags, ["date", "year", "originaldate"]))

    album = most_common(values["album"])
    artist = most_common(values["albumartist"]) or most_common(values["artist"])
    year = extract_year(most_common(values["date"]))
    source = "tags"

    if not album:
        album = album_folder.path.name
        source = "folder fallback"
    if not artist:
        artist = album_folder.path.parent.name if album_folder.path.parent else ""
        if source == "tags":
            source = "partial tags + folder fallback"

    return AlbumMetadata(album=album, artist=artist, year=year, source=source)


def lucene_quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def build_musicbrainz_query(metadata: AlbumMetadata) -> str:
    parts = []
    if metadata.artist:
        parts.append(f"artist:{lucene_quote(metadata.artist)}")
    if metadata.album:
        parts.append(f"release:{lucene_quote(metadata.album)}")
    return " AND ".join(parts)


def request_with_retries(
    session: requests.Session,
    url: str,
    *,
    params: Optional[dict[str, object]] = None,
    timeout: float,
    retries: int,
    rate_limiter: Optional[MusicBrainzRateLimiter] = None,
) -> requests.Response:
    retry_statuses = {429, 500, 502, 503, 504}
    last_exc: Optional[BaseException] = None

    for attempt in range(1, retries + 1):
        if rate_limiter is not None:
            rate_limiter.wait()

        try:
            response = session.get(url, params=params, timeout=timeout, allow_redirects=True)
            if response.status_code not in retry_statuses:
                return response
            if attempt >= retries:
                return response

            retry_after = response.headers.get("Retry-After")
            if retry_after and retry_after.isdigit():
                sleep_for = float(retry_after)
            else:
                sleep_for = min(2 ** (attempt - 1), 10)
            print(f"  ! HTTP {response.status_code}; retrying in {sleep_for:.1f}s")
            time.sleep(sleep_for)
        except requests.RequestException as exc:
            last_exc = exc
            if attempt >= retries:
                break
            sleep_for = min(2 ** (attempt - 1), 10)
            print(f"  ! Network error: {exc}; retrying in {sleep_for:.1f}s")
            time.sleep(sleep_for)

    if last_exc is not None:
        raise RuntimeError(f"request failed after {retries} attempts: {last_exc}") from last_exc

    response.raise_for_status()
    return response


def search_musicbrainz(
    session: requests.Session,
    metadata: AlbumMetadata,
    *,
    timeout: float,
    retries: int,
    candidates: int,
    min_score: int,
    rate_limiter: MusicBrainzRateLimiter,
) -> list[ReleaseCandidate]:
    query = build_musicbrainz_query(metadata)
    if not query:
        return []

    params = {
        "query": query,
        "fmt": "json",
        "limit": max(candidates, 1),
    }
    response = request_with_retries(
        session,
        MUSICBRAINZ_RELEASE_SEARCH_URL,
        params=params,
        timeout=timeout,
        retries=retries,
        rate_limiter=rate_limiter,
    )
    if response.status_code != 200:
        print(f"  ! MusicBrainz search returned HTTP {response.status_code}")
        return []

    payload = response.json()
    releases = payload.get("releases", [])
    results: list[ReleaseCandidate] = []

    for release in releases:
        try:
            score = int(release.get("score", 0))
        except (TypeError, ValueError):
            score = 0
        if score < min_score:
            continue

        artist_credit = ""
        credits = release.get("artist-credit") or []
        if credits:
            names = []
            for credit in credits:
                if isinstance(credit, dict):
                    names.append(str(credit.get("name", "")).strip())
                elif isinstance(credit, str):
                    names.append(credit.strip())
            artist_credit = "".join(names).strip()

        results.append(
            ReleaseCandidate(
                mbid=str(release.get("id", "")).strip(),
                title=str(release.get("title", "")).strip(),
                artist_credit=artist_credit,
                date=str(release.get("date", "")).strip(),
                score=score,
            )
        )

    if metadata.year:
        results.sort(
            key=lambda item: (
                extract_year(item.date) == metadata.year,
                item.score,
            ),
            reverse=True,
        )
    else:
        results.sort(key=lambda item: item.score, reverse=True)

    return [item for item in results if item.mbid]


def download_front_cover(
    session: requests.Session,
    mbid: str,
    *,
    timeout: float,
    retries: int,
) -> tuple[bytes, str]:
    url = COVER_ART_FRONT_URL.format(mbid=mbid)
    response = request_with_retries(
        session,
        url,
        timeout=timeout,
        retries=retries,
    )
    if response.status_code == 404:
        raise FileNotFoundError("Cover Art Archive has no front cover for this release")
    if response.status_code != 200:
        raise RuntimeError(f"Cover Art Archive returned HTTP {response.status_code}")

    content_type = response.headers.get("Content-Type", "")
    if not content_type.lower().startswith("image/"):
        raise RuntimeError(f"Cover Art Archive returned non-image content: {content_type}")

    return response.content, response.url


def download_image(
    session: requests.Session,
    url: str,
    *,
    timeout: float,
    retries: int,
) -> tuple[bytes, str]:
    response = request_with_retries(
        session,
        url,
        timeout=timeout,
        retries=retries,
    )
    if response.status_code == 404:
        raise FileNotFoundError("artwork image was not found")
    if response.status_code != 200:
        raise RuntimeError(f"artwork image returned HTTP {response.status_code}")

    content_type = response.headers.get("Content-Type", "")
    if not content_type.lower().startswith("image/"):
        raise RuntimeError(f"artwork URL returned non-image content: {content_type}")

    return response.content, response.url


def upgraded_itunes_artwork_urls(url: str) -> list[str]:
    if not url:
        return []
    upgraded = []
    for size in ("1200x1200bb.jpg", "600x600bb.jpg"):
        candidate = re.sub(r"\d+x\d+bb\.(jpg|png)$", size, url)
        if candidate not in upgraded:
            upgraded.append(candidate)
    if url not in upgraded:
        upgraded.append(url)
    return upgraded


def fetch_from_itunes(
    session: requests.Session,
    metadata: AlbumMetadata,
    *,
    timeout: float,
    retries: int,
    min_score: int,
) -> list[ArtworkResult]:
    try:
        response = request_with_retries(
            session,
            ITUNES_SEARCH_URL,
            params={
                "term": f"{metadata.artist} {metadata.album}".strip(),
                "media": "music",
                "entity": "album",
                "limit": 10,
            },
            timeout=timeout,
            retries=retries,
        )
        if response.status_code != 200:
            print(f"  ! iTunes search returned HTTP {response.status_code}")
            return []
        payload = response.json()
    except Exception as exc:
        print(f"  ! iTunes search failed: {exc}")
        return []

    results: list[ArtworkResult] = []
    for item in payload.get("results", []):
        artist = str(item.get("artistName", "")).strip()
        album = str(item.get("collectionName", "")).strip()
        score = metadata_match_score(metadata, artist=artist, album=album)
        if score < min_score:
            continue
        for image_url in upgraded_itunes_artwork_urls(str(item.get("artworkUrl100", "")).strip()):
            results.append(
                ArtworkResult(
                    source="iTunes",
                    matched_artist=artist,
                    matched_album=album,
                    score=score,
                    image_url=image_url,
                )
            )

    results.sort(key=lambda item: item.score, reverse=True)
    return results


def fetch_from_deezer(
    session: requests.Session,
    metadata: AlbumMetadata,
    *,
    timeout: float,
    retries: int,
    min_score: int,
) -> list[ArtworkResult]:
    try:
        response = request_with_retries(
            session,
            DEEZER_ALBUM_SEARCH_URL,
            params={"q": f"{metadata.artist} {metadata.album}".strip()},
            timeout=timeout,
            retries=retries,
        )
        if response.status_code != 200:
            print(f"  ! Deezer search returned HTTP {response.status_code}")
            return []
        payload = response.json()
    except Exception as exc:
        print(f"  ! Deezer search failed: {exc}")
        return []

    results: list[ArtworkResult] = []
    for item in payload.get("data", []):
        artist_payload = item.get("artist") or {}
        artist = str(artist_payload.get("name", "")).strip()
        album = str(item.get("title", "")).strip()
        score = metadata_match_score(metadata, artist=artist, album=album)
        if score < min_score:
            continue
        for key in ("cover_xl", "cover_big", "cover_medium", "cover"):
            image_url = str(item.get(key, "")).strip()
            if image_url:
                results.append(
                    ArtworkResult(
                        source="Deezer",
                        matched_artist=artist,
                        matched_album=album,
                        score=score,
                        image_url=image_url,
                    )
                )
                break

    results.sort(key=lambda item: item.score, reverse=True)
    return results


def fetch_from_discogs(
    session: requests.Session,
    metadata: AlbumMetadata,
    *,
    timeout: float,
    retries: int,
    min_score: int,
    token: str,
) -> list[ArtworkResult]:
    token = token.strip()
    if not token:
        return []

    try:
        response = request_with_retries(
            session,
            DISCOGS_DATABASE_SEARCH_URL,
            params={
                "q": f"{metadata.artist} {metadata.album}".strip(),
                "type": "release",
                "token": token,
            },
            timeout=timeout,
            retries=retries,
        )
        if response.status_code != 200:
            print(f"  ! Discogs search returned HTTP {response.status_code}")
            return []
        payload = response.json()
    except Exception as exc:
        print(f"  ! Discogs search failed: {exc}")
        return []

    expected_title = f"{metadata.artist} {metadata.album}".strip()
    results: list[ArtworkResult] = []
    for item in payload.get("results", []):
        title = str(item.get("title", "")).strip()
        image_url = str(item.get("cover_image", "")).strip()
        if not title or not image_url:
            continue
        year = str(item.get("year", "")).strip()
        score = metadata_match_score(
            AlbumMetadata(album=expected_title, artist="", year=metadata.year, source=metadata.source),
            artist="",
            album=title,
            year=year,
        )
        if score < min_score:
            continue
        results.append(
            ArtworkResult(
                source="Discogs",
                matched_artist="",
                matched_album=title,
                score=score,
                image_url=image_url,
            )
        )

    results.sort(key=lambda item: item.score, reverse=True)
    return results


def find_artwork(
    session: requests.Session,
    metadata: AlbumMetadata,
    *,
    timeout: float,
    retries: int,
    candidates: int,
    min_score: int,
    rate_limiter: MusicBrainzRateLimiter,
    discogs_token: str = "",
) -> tuple[Optional[ArtworkResult], bytes, str]:
    last_error = ""

    releases = search_musicbrainz(
        session,
        metadata,
        timeout=timeout,
        retries=retries,
        candidates=candidates,
        min_score=min_score,
        rate_limiter=rate_limiter,
    )
    if releases:
        for release in releases:
            print(
                f"  - Trying MusicBrainz/Cover Art Archive MBID {release.mbid} "
                f"(score {release.score}, {release.artist_credit} - {release.title})"
            )
            try:
                cover_bytes, image_url = download_front_cover(
                    session,
                    release.mbid,
                    timeout=timeout,
                    retries=retries,
                )
                result = ArtworkResult(
                    source="MusicBrainz/Cover Art Archive",
                    matched_artist=release.artist_credit,
                    matched_album=release.title,
                    score=release.score,
                    image_url=image_url,
                    mbid=release.mbid,
                )
                return result, encode_as_jpeg(cover_bytes), ""
            except FileNotFoundError as exc:
                last_error = str(exc)
                print(f"    no front cover: {last_error}")
            except Exception as exc:
                last_error = str(exc)
                print(f"    cover fetch failed: {last_error}")
    else:
        last_error = "no MusicBrainz release match"
        print(f"  - {last_error}")

    fallback_sources = [
        (
            "iTunes",
            lambda: fetch_from_itunes(
                session,
                metadata,
                timeout=timeout,
                retries=retries,
                min_score=min_score,
            ),
        ),
        (
            "Deezer",
            lambda: fetch_from_deezer(
                session,
                metadata,
                timeout=timeout,
                retries=retries,
                min_score=min_score,
            ),
        ),
    ]
    if discogs_token:
        fallback_sources.append(
            (
                "Discogs",
                lambda: fetch_from_discogs(
                    session,
                    metadata,
                    timeout=timeout,
                    retries=retries,
                    min_score=min_score,
                    token=discogs_token,
                ),
            )
        )

    for source_name, fetch_candidates in fallback_sources:
        artwork_candidates = fetch_candidates()
        if not artwork_candidates:
            print(f"  - {source_name}: no matching artwork candidate")
            continue
        for artwork in artwork_candidates:
            print(
                f"  - Trying {artwork.source} "
                f"(score {artwork.score}, {artwork.matched_artist} - {artwork.matched_album})"
            )
            try:
                image_bytes, image_url = download_image(
                    session,
                    artwork.image_url,
                    timeout=timeout,
                    retries=retries,
                )
                artwork.image_url = image_url
                return artwork, encode_as_jpeg(image_bytes), ""
            except FileNotFoundError as exc:
                last_error = f"{artwork.source}: {exc}"
                print(f"    artwork missing: {last_error}")
            except Exception as exc:
                last_error = f"{artwork.source}: {exc}"
                print(f"    artwork fetch failed: {last_error}")

    return None, b"", last_error or "NO_ART_FOUND"


def encode_as_jpeg(image_bytes: bytes) -> bytes:
    try:
        with Image.open(io.BytesIO(image_bytes)) as image:
            if image.mode not in ("RGB", "L"):
                image = image.convert("RGB")
            output = io.BytesIO()
            image.save(output, format="JPEG", quality=90, optimize=True)
            return output.getvalue()
    except UnidentifiedImageError as exc:
        raise RuntimeError("downloaded cover art is not a readable image") from exc


def append_log(log_path: Path, row: dict[str, object]) -> None:
    fieldnames = [
        "timestamp",
        "folder",
        "status",
        "album",
        "artist",
        "year",
        "metadata_source",
        "artwork_source",
        "mbid",
        "image_url",
        "message",
    ]

    log_path.parent.mkdir(parents=True, exist_ok=True)
    should_write_header = not log_path.exists() or log_path.stat().st_size == 0

    with log_path.open("a", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        if should_write_header:
            writer.writeheader()
        writer.writerow({name: row.get(name, "") for name in fieldnames})


def display_status(status: str) -> str:
    return {
        "dry_run": "Matched Results",
        "matched_result": "Matched Results",
        "dry_run_would_write": "Matched Results",
    }.get(status, status)


def log_result(
    log_path: Path,
    folder: Path,
    status: str,
    metadata: Optional[AlbumMetadata] = None,
    *,
    artwork_source: str = "",
    mbid: str = "",
    image_url: str = "",
    message: str = "",
) -> None:
    append_log(
        log_path,
        {
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "folder": str(folder),
            "status": status,
            "album": metadata.album if metadata else "",
            "artist": metadata.artist if metadata else "",
            "year": metadata.year if metadata else "",
            "metadata_source": metadata.source if metadata else "",
            "artwork_source": artwork_source,
            "mbid": mbid,
            "image_url": image_url,
            "message": message,
        },
    )


def process_album_folder(
    album_folder: AlbumFolder,
    *,
    session: requests.Session,
    log_path: Path,
    dry_run: bool,
    force: bool,
    timeout: float,
    retries: int,
    candidates: int,
    min_score: int,
    rate_limiter: MusicBrainzRateLimiter,
    discogs_token: str = "",
) -> str:
    folder = album_folder.path
    target = folder / "folder.jpg"

    existing_art = existing_art_files(folder)
    if existing_art and not force:
        names = ", ".join(path.name for path in existing_art)
        print(f"  - Skipping; existing artwork found: {names}")
        log_result(log_path, folder, "skipped_existing_art", message=names)
        return "skipped"

    metadata = read_album_metadata(album_folder)
    if not metadata.album:
        message = "no album name from tags or folder"
        print(f"  - Skipping; {message}")
        log_result(log_path, folder, "skipped_missing_metadata", metadata, message=message)
        return "skipped"

    print(
        f"  - Album: {metadata.artist or '(unknown artist)'} - "
        f"{metadata.album}{f' ({metadata.year})' if metadata.year else ''}"
    )

    artwork, jpeg_bytes, last_error = find_artwork(
        session,
        metadata,
        timeout=timeout,
        retries=retries,
        candidates=candidates,
        min_score=min_score,
        rate_limiter=rate_limiter,
        discogs_token=discogs_token,
    )
    if artwork is None:
        message = f"NO_ART_FOUND: {last_error or 'no artwork found'}"
        print(f"  - {message}")
        log_result(log_path, folder, "NO_ART_FOUND", metadata, message=message)
        return "not_found"

    if dry_run:
        message = f"would write {target} from {artwork.source}"
        print(f"  + MATCHED RESULT: {message}")
        log_result(
            log_path,
            folder,
            "matched_result",
            metadata,
            artwork_source=artwork.source,
            mbid=artwork.mbid,
            image_url=artwork.image_url,
            message=message,
        )
        return "matched_result"

    try:
        with target.open("wb") as handle:
            handle.write(jpeg_bytes)
    except OSError as exc:
        message = f"could not write folder.jpg: {exc}"
        print(f"  ! {message}")
        log_result(
            log_path,
            folder,
            "write_failed",
            metadata,
            artwork_source=artwork.source,
            mbid=artwork.mbid,
            image_url=artwork.image_url,
            message=message,
        )
        return "error"

    message = f"saved {target.name} from {artwork.source} ({len(jpeg_bytes):,} bytes)"
    print(f"  + {message}")
    log_result(
        log_path,
        folder,
        "saved",
        metadata,
        artwork_source=artwork.source,
        mbid=artwork.mbid,
        image_url=artwork.image_url,
        message=message,
    )
    return "saved"


def main() -> int:
    args = parse_args()
    root = Path(args.root).expanduser()
    log_path = Path(args.log).expanduser()

    if args.limit is not None and args.limit < 1:
        print("--limit must be 1 or greater.", file=sys.stderr)
        return 2
    if args.retries < 1:
        print("--retries must be 1 or greater.", file=sys.stderr)
        return 2
    if args.candidates < 1:
        print("--candidates must be 1 or greater.", file=sys.stderr)
        return 2
    if args.min_score < 0 or args.min_score > 100:
        print("--min-score must be between 0 and 100.", file=sys.stderr)
        return 2
    if not root.exists() or not root.is_dir():
        print(f"Music root does not exist or is not a directory: {root}", file=sys.stderr)
        return 2
    if not load_dependencies():
        return 2

    print("fetch_folder_art.py")
    print(f"Root: {root}")
    print(f"Log: {log_path}")
    print(f"Matched Results mode: {'yes' if args.dry_run else 'no'}")
    print(f"Force: {'yes' if args.force else 'no'}")
    print()

    album_folders = find_album_folders(root, args.limit)
    if not album_folders:
        print("No album folders with supported audio files were found.")
        return 0

    session = requests.Session()
    session.headers.update({"User-Agent": args.user_agent, "Accept": "application/json, image/*"})
    rate_limiter = MusicBrainzRateLimiter()

    counts = Counter()
    total = len(album_folders)
    for index, album_folder in enumerate(album_folders, start=1):
        print()
        print(f"[{index}/{total}] {album_folder.path}")
        try:
            status = process_album_folder(
                album_folder,
                session=session,
                log_path=log_path,
                dry_run=args.dry_run,
                force=args.force,
                timeout=args.timeout,
                retries=args.retries,
                candidates=args.candidates,
                min_score=args.min_score,
                rate_limiter=rate_limiter,
                discogs_token=args.discogs_token,
            )
        except KeyboardInterrupt:
            raise
        except Exception as exc:
            status = "error"
            print(f"  ! Unexpected error: {exc}")
            log_result(log_path, album_folder.path, "error", message=str(exc))
        counts[status] += 1

    print()
    print("Done.")
    print(
        "Summary: "
        + ", ".join(f"{display_status(name)}={count}" for name, count in sorted(counts.items()))
    )
    return 0


def should_pause_before_exit() -> bool:
    return os.name == "nt" and "--pause" in sys.argv[1:]


def pause_before_exit() -> None:
    sys.stdout.flush()
    sys.stderr.flush()
    try:
        input("\nPress Enter to close...")
    except EOFError:
        pass


if __name__ == "__main__":
    exit_code = 0
    try:
        exit_code = main()
    except KeyboardInterrupt:
        print("\nStopped by user.", file=sys.stderr)
        exit_code = 130
    except SystemExit as exc:
        if isinstance(exc.code, int):
            exit_code = exc.code
        elif exc.code is None:
            exit_code = 0
        else:
            print(exc.code, file=sys.stderr)
            exit_code = 1
    finally:
        if should_pause_before_exit():
            pause_before_exit()
    raise SystemExit(exit_code)

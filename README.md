# FetchFolderArt

FetchFolderArt by devphaZe foundry is a Windows Python utility for scanning local or mapped NAS music folders and adding missing `folder.jpg` album artwork. While there are other programs and web scrapers that will do the same, I wanted a solution that was local & private. FetchFolderArt will check 4 different sources for album art.

The app recursively scans selected music folders, reads album tags with Mutagen, searches artwork sources, previews matched covers, and writes `folder.jpg` only after confirmation or when Automatic Commits is enabled.

## Features

- Windows GUI for selecting one or multiple music folders.
- Recursively scans nested album folders on local drives, mapped drives, and UNC/NAS paths.
- Creates `folder.jpg` only in album folders.
- Never renames, moves, retags, embeds artwork into, or otherwise modifies audio files.
- Matched Results workflow previews covers before committing changes.
- Optional Automatic Commits for unattended multi-folder runs.
- Light and dark themes.
- Preview gallery for browsing covers before commit.
- Uses MusicBrainz/Cover Art Archive first, then iTunes, Deezer, and optional Discogs fallback.
- Optional Discogs token manager in `Options > Discogs Token...`.

## Screenshots

Screenshots can be added to the [`screenshots`](screenshots) folder.

## Requirements

- Windows 10 or newer.
- Python 3.10 or newer.
- A local music folder, mapped drive, or UNC/NAS music path.

Install dependencies:

```powershell
python -m pip install -r requirements.txt
```

## Run From Source

From the repository root:

```powershell
$env:PYTHONPATH = "$PWD\src"
python -m fetchfolderart
```

Or launch the GUI with:

```powershell
scripts\fetch_folder_art_gui.bat
```

Run the command-line utility:

```powershell
$env:PYTHONPATH = "$PWD\src"
python -m fetchfolderart.fetch_folder_art "M:\Music" --dry-run
```

## Install Locally

From the repository root:

```powershell
python -m pip install .
fetch-folder-art-gui
```

CLI entry point:

```powershell
fetch-folder-art "M:\Music" --dry-run
```

## Standalone Windows Executable

Users who do not want to install Python can download the standalone Windows
build from the GitHub Releases page:

[FetchFolderArt Releases](https://github.com/phazecrypto/FetchFolderArt/releases)

Download `FetchFolderArt-Windows-x64.zip`, extract it, and run
`FetchFolderArt.exe`.

Maintainers can build the executable locally:

```powershell
scripts\build_windows_exe.ps1
```

The ZIP is written to `dist\FetchFolderArt-Windows-x64.zip`.

## Discogs Token

Discogs is optional and is only used as the final fallback source.

To configure it in the GUI, open `Options > Discogs Token...` and save your personal token. The token is stored in the current Windows user's `DISCOGS_TOKEN` environment variable, not in the source code.

For command-line use, you can also set:

```powershell
[Environment]::SetEnvironmentVariable("DISCOGS_TOKEN", "your-token-here", "User")
```

Do not commit personal tokens, logs, cache files, or generated artwork to the repository.

## Safety

FetchFolderArt only writes `folder.jpg` files during the commit step. It does not alter audio files. For extra caution, run against a small folder first and review Matched Results before committing.

## Project Layout

```text
FetchFolderArt/
  src/fetchfolderart/       Python package and app source
  scripts/                  Windows launcher scripts
  screenshots/              App screenshots for GitHub
  data/                     Runtime logs/cache, ignored except .gitkeep
  .github/ISSUE_TEMPLATE/   GitHub issue templates
```

## License

This project is released under the MIT License. See [LICENSE](LICENSE).

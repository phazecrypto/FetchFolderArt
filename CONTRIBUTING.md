# Contributing

Thanks for helping improve FetchFolderArt.

## Development Setup

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
$env:PYTHONPATH = "$PWD\src"
python -m fetchfolderart
```

## Guidelines

- Keep audio files read-only. The app must never retag, rename, move, or embed artwork into audio files.
- Keep generated logs, caches, downloaded images, personal paths, and tokens out of Git.
- Use a proper User-Agent for MusicBrainz requests and keep request rates respectful.
- Test with small folders before scanning large music libraries.

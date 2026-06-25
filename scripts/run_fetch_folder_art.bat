@echo off
setlocal
set PYTHONDONTWRITEBYTECODE=1
set "REPO_ROOT=%~dp0.."
cd /d "%REPO_ROOT%"
set "PYTHONPATH=%REPO_ROOT%\src"

echo fetch_folder_art.py
echo.
set /p MUSIC_ROOT=Music folder path, for example M:\Music or \\NAS\Music: 

if "%MUSIC_ROOT%"=="" (
    echo.
    echo No music folder entered.
    pause
    exit /b 2
)

echo.
echo Running a safe dry run first...
echo.
python -m fetchfolderart.fetch_folder_art "%MUSIC_ROOT%" --dry-run --log "%REPO_ROOT%\data\art_fetch_log.csv"

echo.
echo To write folder.jpg files for real, run this from PowerShell:
echo python -m fetchfolderart.fetch_folder_art "%MUSIC_ROOT%" --log "%REPO_ROOT%\data\art_fetch_log.csv"
echo.
pause

@echo off
setlocal
set PYTHONDONTWRITEBYTECODE=1
set "REPO_ROOT=%~dp0.."
cd /d "%REPO_ROOT%"
set "PYTHONPATH=%REPO_ROOT%\src"
where pythonw >nul 2>nul
if %errorlevel%==0 (
    start "" pythonw -m fetchfolderart.fetch_folder_art_gui
    exit /b 0
)
python -m fetchfolderart.fetch_folder_art_gui
if errorlevel 1 pause

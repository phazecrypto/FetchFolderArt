$ErrorActionPreference = "Stop"

$repoRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
$srcRoot = Join-Path $repoRoot "src"
$distRoot = Join-Path $repoRoot "dist"
$buildRoot = Join-Path $repoRoot "build"
$packageRoot = Join-Path $distRoot "FetchFolderArt-Windows-x64"
$zipPath = Join-Path $distRoot "FetchFolderArt-Windows-x64.zip"
$tempZipPath = Join-Path $repoRoot "FetchFolderArt-Windows-x64.zip"
$entryPoint = Join-Path $srcRoot "fetchfolderart\fetch_folder_art_gui.py"

Set-Location $repoRoot

python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -m pip install "pyinstaller>=6.0"

Remove-Item -LiteralPath $buildRoot -Recurse -Force -ErrorAction SilentlyContinue
Remove-Item -LiteralPath $distRoot -Recurse -Force -ErrorAction SilentlyContinue

$env:PYTHONPATH = $srcRoot

python -m PyInstaller `
    --noconfirm `
    --clean `
    --onefile `
    --windowed `
    --name FetchFolderArt `
    --paths $srcRoot `
    --specpath $buildRoot `
    --workpath $buildRoot `
    --distpath $distRoot `
    --collect-submodules mutagen `
    --collect-submodules PIL `
    $entryPoint

New-Item -ItemType Directory -Force -Path $packageRoot | Out-Null
Move-Item -LiteralPath (Join-Path $distRoot "FetchFolderArt.exe") -Destination (Join-Path $packageRoot "FetchFolderArt.exe")
Copy-Item -LiteralPath (Join-Path $repoRoot "README.md") -Destination $packageRoot
Copy-Item -LiteralPath (Join-Path $repoRoot "install.txt") -Destination $packageRoot
Copy-Item -LiteralPath (Join-Path $repoRoot "LICENSE") -Destination $packageRoot
New-Item -ItemType Directory -Force -Path (Join-Path $packageRoot "data") | Out-Null
Copy-Item -LiteralPath (Join-Path $repoRoot "data\README.md") -Destination (Join-Path $packageRoot "data\README.md")

Remove-Item -LiteralPath $zipPath -Force -ErrorAction SilentlyContinue
Remove-Item -LiteralPath $tempZipPath -Force -ErrorAction SilentlyContinue
Compress-Archive -Path (Join-Path $packageRoot "*") -DestinationPath $tempZipPath
Move-Item -LiteralPath $tempZipPath -Destination $zipPath

Write-Host "Built $zipPath"

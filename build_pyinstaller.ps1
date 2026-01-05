param(
    [string]$Version = "0.0.0-dev",
    [string]$Channel = "stable",
    [string]$RepoOwner = "REPO_OWNER",
    [string]$RepoName = "REPO_NAME"
)

$ErrorActionPreference = 'Stop'

# Build a one-folder distribution (recommended for PyQt).
# Run from the repo root.

$RepoRoot = (Resolve-Path '.').Path

function Get-TempBuildRoot {
    $base = if ($env:TEMP) { $env:TEMP } else { [System.IO.Path]::GetTempPath() }
    return (Join-Path $base 'TouchpadExperimentManager_build')
}

function Resolve-Ffmpeg {
    $psycho = 'C:\Program Files\PsychoPy\share\ffpyplayer\ffmpeg\bin\ffmpeg.exe'
    if (Test-Path $psycho) { return $psycho }

    $cmd = Get-Command ffmpeg -ErrorAction SilentlyContinue
    if ($cmd -and $cmd.Source) { return $cmd.Source }

    return $null
}

function Resolve-Ffprobe([string]$ffmpegPath) {
    if ($ffmpegPath) {
        $cand = Join-Path (Split-Path $ffmpegPath -Parent) 'ffprobe.exe'
        if (Test-Path $cand) { return $cand }
    }

    $cmd = Get-Command ffprobe -ErrorAction SilentlyContinue
    if ($cmd -and $cmd.Source) { return $cmd.Source }

    return $null
}

function Get-TempVenvPath {
    $base = if ($env:TEMP) { $env:TEMP } else { [System.IO.Path]::GetTempPath() }
    return (Join-Path $base 'TouchpadExperimentManager_venv')
}

Write-Host 'Creating temporary build venv (ASCII-only path)...'
$VenvDir = Get-TempVenvPath
if (Test-Path $VenvDir) {
    Remove-Item -Recurse -Force $VenvDir
}
py -m venv $VenvDir

$Py = Join-Path $VenvDir 'Scripts\python.exe'

Write-Host 'Installing deps into build venv...'
& $Py -m pip install -U pip
& $Py -m pip install -r (Join-Path $RepoRoot 'requirements.txt')
& $Py -m pip install -U pyinstaller

Write-Host 'Preparing temporary build directory (ASCII-only path)...'
$BuildRoot = Get-TempBuildRoot
if (Test-Path $BuildRoot) {
    Remove-Item -Recurse -Force $BuildRoot
}
New-Item -ItemType Directory -Path $BuildRoot | Out-Null

# Hydrate update_config.json with repo info (supports stable/beta channels)
$UpdateConfigTemplate = Join-Path $RepoRoot 'update_config.json'
if (Test-Path $UpdateConfigTemplate) {
    $raw = Get-Content -Raw -Path $UpdateConfigTemplate
    $hydrated = $raw.Replace('REPO_OWNER', $RepoOwner).Replace('REPO_NAME', $RepoName)
    Set-Content -Path (Join-Path $BuildRoot 'update_config.json') -Value $hydrated -Encoding UTF8
}

# Generate version.json for the build
$buildInfo = [ordered]@{
    version = $Version
    channel = $Channel
    built   = (Get-Date).ToString('s')
}
$VersionPath = Join-Path $BuildRoot 'version.json'
$buildInfo | ConvertTo-Json | Set-Content -Path $VersionPath -Encoding UTF8

Write-Host 'Locating ffmpeg/ffprobe to bundle...'
$Ffmpeg = Resolve-Ffmpeg
if (-not $Ffmpeg) {
    throw 'ffmpeg.exe not found. Install ffmpeg (e.g. winget install ffmpeg) or install PsychoPy, then rerun build.'
}
$Ffprobe = Resolve-Ffprobe $Ffmpeg
if (-not $Ffprobe) {
    Write-Warning 'ffprobe.exe not found; continuing without it (duration/metadata may be slower).'
}

# Copy source files to temp build dir (avoid non-ASCII path issues in packaging)
$rootFiles = @(
    'main_interface.py',
    'gui_menu.py',
    'exp_initializer.py',
    'audio_processor.py',
    'convert_audio.py',
    'analyzer_refactored.py',
    'tablet_experiment.py',
    'qt_bootstrap.py',
    'app_paths.py'
)

foreach ($f in $rootFiles) {
    $src = Join-Path $RepoRoot $f
    if (Test-Path $src) {
        Copy-Item -Force $src (Join-Path $BuildRoot $f)
    }
}

foreach ($dir in @('src', 'MLM')) {
    $srcDir = Join-Path $RepoRoot $dir
    if (Test-Path $srcDir) {
        Copy-Item -Recurse -Force $srcDir (Join-Path $BuildRoot $dir)
    }
}

# Bundle ffmpeg into assets/bin
$AssetsBin = Join-Path $BuildRoot 'assets\bin'
New-Item -ItemType Directory -Force -Path $AssetsBin | Out-Null
Copy-Item -Force $Ffmpeg (Join-Path $AssetsBin 'ffmpeg.exe')
if ($Ffprobe) {
    Copy-Item -Force $Ffprobe (Join-Path $AssetsBin 'ffprobe.exe')
}

# If ffmpeg is a shared build (common on Windows), it depends on DLLs in the same folder.
$FfmpegDir = Split-Path $Ffmpeg -Parent
if (Test-Path $FfmpegDir) {
    Get-ChildItem -Path $FfmpegDir -Filter '*.dll' -File -ErrorAction SilentlyContinue |
    ForEach-Object { Copy-Item -Force $_.FullName (Join-Path $AssetsBin $_.Name) }
}

# Work around Qt plugin path resolution issues under non-ASCII paths by
# explicitly setting plugin env vars based on PyQt5's install location.
$PluginDir = & $Py -c "import PyQt5; from pathlib import Path; print(Path(PyQt5.__file__).resolve().parent / 'Qt5' / 'plugins')"
$PlatformDir = Join-Path $PluginDir 'platforms'
$env:QT_PLUGIN_PATH = $PluginDir
$env:QT_QPA_PLATFORM_PLUGIN_PATH = $PlatformDir

Write-Host 'Building...'
Set-Location $BuildRoot
& $Py -m PyInstaller --noconfirm --windowed --name TouchpadExperimentManager `
    --collect-submodules PyQt5.QtMultimedia `
    --add-data "version.json;." `
    --add-data "update_config.json;." `
    --add-data "assets;assets" `
    --add-data "src;src" `
    --add-data "MLM;MLM" `
    main_interface.py

Write-Host 'Copying dist back to repo...'
$RepoDist = Join-Path $RepoRoot 'dist'
if (Test-Path $RepoDist) {
    Remove-Item -Recurse -Force $RepoDist
}
Copy-Item -Recurse -Force (Join-Path $BuildRoot 'dist') $RepoDist

# Produce a portable ZIP for GitHub releases
$PortableZip = Join-Path $RepoRoot "TouchpadExperimentManager-portable.zip"
if (Test-Path $PortableZip) {
    Remove-Item -Force $PortableZip
}
Compress-Archive -Path (Join-Path $RepoDist 'TouchpadExperimentManager') -DestinationPath $PortableZip -Force

# Update channel metadata in the repo (commit/push these to enable update checks)
$ReleaseDir = Join-Path $RepoRoot (Join-Path 'releases' $Channel)
New-Item -ItemType Directory -Force -Path $ReleaseDir | Out-Null

$hash = (Get-FileHash -Algorithm SHA256 -Path $PortableZip).Hash.ToLower()
"$hash  TouchpadExperimentManager-portable.zip" | Set-Content -Path (Join-Path $ReleaseDir 'version.sha256') -Encoding UTF8

$releaseInfo = [ordered]@{
    version   = $Version
    channel   = $Channel
    published = (Get-Date).ToString('yyyy-MM-dd')
    notes     = ""
}
$releaseInfo | ConvertTo-Json | Set-Content -Path (Join-Path $ReleaseDir 'version.json') -Encoding UTF8

Set-Location $RepoRoot
Write-Host 'Done. Output in .\dist\TouchpadExperimentManager'
Write-Host "Portable ZIP: $PortableZip"
Write-Host "Updated metadata: $ReleaseDir\\version.json and version.sha256 (commit & push these)"

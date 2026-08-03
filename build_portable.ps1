# Build Portable Package Script
# This script creates a versioned portable .zip distribution of the Touchpad Experiment Manager

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Push-Location $projectRoot

try {
    $appVersion = (python -c "from project_version import APP_VERSION; print(APP_VERSION)" 2>&1).Trim()
    if ($LASTEXITCODE -ne 0 -or $appVersion -notmatch '^\d+\.\d+\.\d+([.-][0-9A-Za-z.-]+)?$') {
        Write-Host "[ERROR] Could not read a valid version from project_version.py" -ForegroundColor Red
        exit 1
    }

    Write-Host "=== Touchpad Experiment Manager v$appVersion - Build Script ===" -ForegroundColor Cyan
    Write-Host ""

    # Step 1: Check Python installation
    Write-Host "Step 1: Checking Python installation..." -ForegroundColor Yellow
    try {
        $pythonVersion = python --version 2>&1
        Write-Host "  [OK] Found: $pythonVersion" -ForegroundColor Green
    }
    catch {
        Write-Host "  [ERROR] Python not found. Please install Python 3.8 or higher." -ForegroundColor Red
        exit 1
    }

    # Step 2: Install dependencies
    Write-Host ""
    Write-Host "Step 2: Installing dependencies..." -ForegroundColor Yellow
    Write-Host "  Upgrading pip and pinning setuptools for PyInstaller compatibility..." -ForegroundColor Gray
    python -m pip install --upgrade pip wheel "setuptools==80.10.1"

    Write-Host "  Installing packages from requirements.txt (using pre-built wheels only)..." -ForegroundColor Gray
    python -m pip install --only-binary :all: -r requirements.txt 2>$null
    if ($LASTEXITCODE -ne 0) {
        Write-Host "  Some packages don't have wheels, installing normally..." -ForegroundColor Yellow
        python -m pip install -r requirements.txt
        if ($LASTEXITCODE -ne 0) {
            Write-Host "  [ERROR] Failed to install dependencies" -ForegroundColor Red
            Write-Host "  Note: If using Python 3.13, some packages may not be compatible yet." -ForegroundColor Yellow
            Write-Host "  Consider using Python 3.11 or 3.12 instead." -ForegroundColor Yellow
            exit 1
        }
    }
    Write-Host "  [OK] Dependencies installed successfully" -ForegroundColor Green

    # Step 3: Build with PyInstaller
    Write-Host ""
    Write-Host "Step 3: Building executable with PyInstaller..." -ForegroundColor Yellow
    Write-Host "  This may take a few minutes..." -ForegroundColor Gray

    # Pre-step: Stage ffmpeg/ffprobe into assets/bin if available
    Write-Host ""
    Write-Host "Pre-step: Staging ffmpeg into assets/bin (if available)..." -ForegroundColor Yellow
    $assetsBin = "assets\bin"
    New-Item -ItemType Directory -Force -Path $assetsBin | Out-Null

    $ffmpegPath = $null
    try {
        $ffmpegPath = (Get-Command ffmpeg -ErrorAction Stop).Source
    }
    catch {
        $ffmpegPath = $null
    }

    if ($ffmpegPath -and (Test-Path $ffmpegPath)) {
        Copy-Item $ffmpegPath "$assetsBin\ffmpeg.exe" -Force -ErrorAction SilentlyContinue
        $ffprobePath = "$(Split-Path $ffmpegPath -Parent)\ffprobe.exe"
        if (Test-Path $ffprobePath) {
            Copy-Item $ffprobePath "$assetsBin\ffprobe.exe" -Force -ErrorAction SilentlyContinue
        }
        Write-Host "  [OK] Staged ffmpeg/ffprobe into assets/bin" -ForegroundColor Green
    }
    else {
        Write-Host "  [WARNING] ffmpeg not found; MP3/M4A support will be missing" -ForegroundColor Yellow
    }

    if (Test-Path "build") {
        Remove-Item -Recurse -Force "build"
    }
    if (Test-Path "dist") {
        Remove-Item -Recurse -Force "dist"
    }

    pyinstaller --clean TouchpadExperiment.spec

    if ($LASTEXITCODE -ne 0) {
        Write-Host "  [ERROR] PyInstaller build failed" -ForegroundColor Red
        exit 1
    }
    Write-Host "  [OK] Executable built successfully" -ForegroundColor Green

    # Step 4: Check if ffmpeg is needed (optional but recommended)
    Write-Host ""
    Write-Host "Step 4: Checking for ffmpeg (for audio processing)..." -ForegroundColor Yellow

    $ffmpegFound = $false
    try {
        $ffmpegVersion = ffmpeg -version 2>&1 | Select-String "ffmpeg version" | Select-Object -First 1
        if ($ffmpegVersion) {
            Write-Host "  [OK] ffmpeg found in system PATH" -ForegroundColor Green
            $ffmpegFound = $true
        }
    }
    catch {
        Write-Host "  [WARNING] ffmpeg not found in system PATH" -ForegroundColor Yellow
    }

    if (-not $ffmpegFound) {
        Write-Host "  Note: ffmpeg is optional but recommended for audio features" -ForegroundColor Gray
        Write-Host "  You can download it from: https://ffmpeg.org/download.html" -ForegroundColor Gray
        Write-Host "  Or install via: winget install ffmpeg" -ForegroundColor Gray
    }

    # Step 5: Create portable package
    Write-Host ""
    Write-Host "Step 5: Creating portable ZIP package..." -ForegroundColor Yellow

    $distFolder = "dist\TouchpadExperiment"
    $zipName = "TouchpadExperiment-v$appVersion-portable.zip"

    if (Test-Path $distFolder) {
        if ($ffmpegFound) {
            $binFolder = "$distFolder\assets\bin"
            New-Item -ItemType Directory -Force -Path $binFolder | Out-Null

            try {
                $ffmpegPath = (Get-Command ffmpeg).Source
                if (Test-Path $ffmpegPath) {
                    Copy-Item $ffmpegPath "$binFolder\ffmpeg.exe" -ErrorAction SilentlyContinue
                    Copy-Item "$([System.IO.Path]::GetDirectoryName($ffmpegPath))\ffprobe.exe" "$binFolder\ffprobe.exe" -ErrorAction SilentlyContinue
                    Write-Host "  [OK] Bundled ffmpeg into package" -ForegroundColor Green
                }
            }
            catch {
                Write-Host "  [WARNING] Could not bundle ffmpeg (you may need to include it manually)" -ForegroundColor Yellow
            }
        }

        Compress-Archive -Path $distFolder -DestinationPath $zipName -Force

        if (Test-Path $zipName) {
            $zipSize = (Get-Item $zipName).Length / 1MB
            $zipSizeRounded = [math]::Round($zipSize, 2)
            Write-Host "  [OK] Created: $zipName ($zipSizeRounded MB)" -ForegroundColor Green
        }
        else {
            Write-Host "  [ERROR] Failed to create ZIP file" -ForegroundColor Red
            exit 1
        }
    }
    else {
        Write-Host "  [ERROR] Build folder not found at $distFolder" -ForegroundColor Red
        exit 1
    }

    Write-Host ""
    Write-Host "=== Build Complete: v$appVersion ===" -ForegroundColor Cyan
    Write-Host "Portable package created: $zipName" -ForegroundColor Green
    Write-Host ""
    Write-Host "To distribute:" -ForegroundColor White
    Write-Host "  1. Create or push the matching Git tag: v$appVersion" -ForegroundColor Gray
    Write-Host "  2. Attach $zipName to the GitHub release" -ForegroundColor Gray
    Write-Host "  3. Users extract it and run TouchpadExperiment.exe" -ForegroundColor Gray
    Write-Host ""
    Write-Host "No Python or dependencies needed on target machines!" -ForegroundColor Green
}
finally {
    Pop-Location
}

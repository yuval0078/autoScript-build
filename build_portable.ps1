# Build Portable Package Script
# Creates a portable, versioned ZIP distribution of the application.

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Push-Location $projectRoot

try {
    # Read all release metadata from the single Python source of truth.
    $releaseInfoLines = @(
        python -c "from project_version import APP_VERSION, RELEASE_TAG, PORTABLE_ARCHIVE_NAME; print(APP_VERSION); print(RELEASE_TAG); print(PORTABLE_ARCHIVE_NAME)" 2>&1
    )

    if ($LASTEXITCODE -ne 0 -or $releaseInfoLines.Count -ne 3) {
        Write-Host "[ERROR] Could not read release metadata from project_version.py" -ForegroundColor Red
        exit 1
    }

    $appVersion = $releaseInfoLines[0].Trim()
    $releaseTag = $releaseInfoLines[1].Trim()
    $zipName = $releaseInfoLines[2].Trim()

    if ($appVersion -notmatch '^\d+\.\d+\.\d+(\.\d+)?$') {
        Write-Host "[ERROR] APP_VERSION must contain three or four numeric components." -ForegroundColor Red
        exit 1
    }
    if ($releaseTag -ne "v$appVersion") {
        Write-Host "[ERROR] RELEASE_TAG does not match APP_VERSION." -ForegroundColor Red
        exit 1
    }
    if ($zipName -ne "TouchpadExperiment-$releaseTag-portable.zip") {
        Write-Host "[ERROR] PORTABLE_ARCHIVE_NAME does not match RELEASE_TAG." -ForegroundColor Red
        exit 1
    }

    Write-Host "=== Touchpad Experiment Manager $releaseTag - Build Script ===" -ForegroundColor Cyan
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
    if ($LASTEXITCODE -ne 0) {
        Write-Host "  [ERROR] Failed to prepare pip dependencies." -ForegroundColor Red
        exit 1
    }

    Write-Host "  Installing packages from requirements.txt (using pre-built wheels only)..." -ForegroundColor Gray
    python -m pip install --only-binary :all: -r requirements.txt 2>$null
    if ($LASTEXITCODE -ne 0) {
        Write-Host "  Some packages do not have wheels; installing normally..." -ForegroundColor Yellow
        python -m pip install -r requirements.txt
        if ($LASTEXITCODE -ne 0) {
            Write-Host "  [ERROR] Failed to install dependencies." -ForegroundColor Red
            Write-Host "  Consider using Python 3.11 or 3.12 if your Python version is unsupported." -ForegroundColor Yellow
            exit 1
        }
    }
    Write-Host "  [OK] Dependencies installed successfully" -ForegroundColor Green

    # Step 3: Stage ffmpeg/ffprobe when available
    Write-Host ""
    Write-Host "Step 3: Staging ffmpeg into assets/bin (if available)..." -ForegroundColor Yellow
    $assetsBin = "assets\bin"
    New-Item -ItemType Directory -Force -Path $assetsBin | Out-Null

    $ffmpegPath = $null
    $stagedFfmpegPath = "$assetsBin\ffmpeg.exe"
    if (Test-Path $stagedFfmpegPath) {
        $ffmpegPath = (Resolve-Path $stagedFfmpegPath).Path
    }
    else {
        try {
            $ffmpegPath = (Get-Command ffmpeg -ErrorAction Stop).Source
        }
        catch {
            $ffmpegPath = $null
        }
    }

    if ($ffmpegPath -and (Test-Path $ffmpegPath)) {
        if ((Resolve-Path $ffmpegPath).Path -ne (Join-Path $projectRoot $stagedFfmpegPath)) {
            Copy-Item $ffmpegPath $stagedFfmpegPath -Force
            $ffmpegPath = (Resolve-Path $stagedFfmpegPath).Path
        }
        $ffprobePath = "$(Split-Path $ffmpegPath -Parent)\ffprobe.exe"
        if (Test-Path $ffprobePath) {
            Copy-Item $ffprobePath "$assetsBin\ffprobe.exe" -Force -ErrorAction SilentlyContinue
        }
        Write-Host "  [OK] Staged ffmpeg/ffprobe into assets/bin" -ForegroundColor Green
    }
    else {
        Write-Host "  [WARNING] ffmpeg not found; MP3/M4A support will be missing." -ForegroundColor Yellow
    }

    # Step 4: Build with PyInstaller
    Write-Host ""
    Write-Host "Step 4: Building executables with PyInstaller..." -ForegroundColor Yellow

    if (Test-Path "build") {
        Remove-Item -Recurse -Force "build"
    }
    if (Test-Path "dist") {
        Remove-Item -Recurse -Force "dist"
    }

    python -m PyInstaller --clean TouchpadExperiment.spec
    if ($LASTEXITCODE -ne 0) {
        Write-Host "  [ERROR] PyInstaller build failed." -ForegroundColor Red
        exit 1
    }
    Write-Host "  [OK] Executables built successfully" -ForegroundColor Green

    # Step 5: Create portable package
    Write-Host ""
    Write-Host "Step 5: Creating portable ZIP package..." -ForegroundColor Yellow

    $distFolder = "dist\TouchpadExperiment"
    if (-not (Test-Path $distFolder)) {
        Write-Host "  [ERROR] Build folder not found at $distFolder" -ForegroundColor Red
        exit 1
    }

    # Keep a copy beside the built executables as a fallback, even though the
    # spec normally bundles these files from assets/bin.
    if ($ffmpegPath -and (Test-Path $ffmpegPath)) {
        $binFolder = "$distFolder\assets\bin"
        New-Item -ItemType Directory -Force -Path $binFolder | Out-Null
        Copy-Item $ffmpegPath "$binFolder\ffmpeg.exe" -Force -ErrorAction SilentlyContinue
        $ffprobePath = "$(Split-Path $ffmpegPath -Parent)\ffprobe.exe"
        if (Test-Path $ffprobePath) {
            Copy-Item $ffprobePath "$binFolder\ffprobe.exe" -Force -ErrorAction SilentlyContinue
        }
    }

    Compress-Archive -Path $distFolder -DestinationPath $zipName -Force
    if (-not (Test-Path $zipName)) {
        Write-Host "  [ERROR] Failed to create ZIP file." -ForegroundColor Red
        exit 1
    }

    $zipSize = (Get-Item $zipName).Length / 1MB
    $zipSizeRounded = [math]::Round($zipSize, 2)
    Write-Host "  [OK] Created: $zipName ($zipSizeRounded MB)" -ForegroundColor Green

    Write-Host ""
    Write-Host "=== Build Complete: $releaseTag ===" -ForegroundColor Cyan
    Write-Host "Portable package: $zipName" -ForegroundColor Green
    Write-Host "Expected Git tag: $releaseTag" -ForegroundColor Gray
    Write-Host "Use .\release.ps1 for the validated test/build/tag workflow." -ForegroundColor Gray
}
finally {
    Pop-Location
}

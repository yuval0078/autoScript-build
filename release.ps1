# Validated release workflow for the Touchpad Experiment Manager.
# Run this from a clean main branch after updating APP_VERSION.

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Push-Location $projectRoot

try {
    $releaseInfoLines = @(
        python -c "from project_version import APP_VERSION, RELEASE_TAG, PORTABLE_ARCHIVE_NAME; print(APP_VERSION); print(RELEASE_TAG); print(PORTABLE_ARCHIVE_NAME)" 2>&1
    )
    if ($LASTEXITCODE -ne 0 -or $releaseInfoLines.Count -ne 3) {
        throw "Could not read release metadata from project_version.py."
    }

    $appVersion = $releaseInfoLines[0].Trim()
    $releaseTag = $releaseInfoLines[1].Trim()
    $archiveName = $releaseInfoLines[2].Trim()

    if ($appVersion -notmatch '^\d+\.\d+\.\d+$') {
        throw "APP_VERSION must use semantic versioning (major.minor.patch)."
    }
    if ($releaseTag -ne "v$appVersion") {
        throw "RELEASE_TAG does not match APP_VERSION."
    }
    if ($archiveName -ne "TouchpadExperiment-$releaseTag-portable.zip") {
        throw "PORTABLE_ARCHIVE_NAME does not match RELEASE_TAG."
    }

    if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
        throw "Git is not installed or not available in PATH."
    }

    $currentBranch = (git branch --show-current).Trim()
    if ($LASTEXITCODE -ne 0 -or -not $currentBranch) {
        throw "Could not determine the current Git branch."
    }
    if ($currentBranch -ne "main") {
        throw "Official releases must be created from main; current branch is '$currentBranch'."
    }

    $workingTreeChanges = @(git status --porcelain)
    if ($LASTEXITCODE -ne 0) {
        throw "Could not inspect the Git working tree."
    }
    if ($workingTreeChanges.Count -gt 0) {
        throw "The Git working tree is not clean. Commit or stash changes before releasing."
    }

    Write-Host "=== Preparing release $releaseTag ===" -ForegroundColor Cyan

    Write-Host "Step 1: Running focused unit tests..." -ForegroundColor Yellow
    python -m unittest discover -s tests -p "test_*.py" -v
    if ($LASTEXITCODE -ne 0) {
        throw "Unit tests failed. No tag was created."
    }
    Write-Host "  [OK] Tests passed" -ForegroundColor Green

    Write-Host "Step 2: Building $archiveName..." -ForegroundColor Yellow
    & "$projectRoot\build_portable.ps1"
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path $archiveName)) {
        throw "Portable build failed. No tag was created."
    }
    Write-Host "  [OK] Portable build completed" -ForegroundColor Green

    $headCommit = (git rev-parse HEAD).Trim()
    git rev-parse -q --verify "refs/tags/$releaseTag" *> $null
    $tagExists = ($LASTEXITCODE -eq 0)

    if ($tagExists) {
        $tagCommit = (git rev-list -n 1 $releaseTag).Trim()
        if ($tagCommit -ne $headCommit) {
            throw "Tag $releaseTag already exists on a different commit. Increase APP_VERSION instead of moving a release tag."
        }
        Write-Host "Step 3: Tag $releaseTag already points to HEAD; leaving it unchanged." -ForegroundColor Yellow
    }
    else {
        Write-Host "Step 3: Creating annotated Git tag $releaseTag..." -ForegroundColor Yellow
        git tag -a $releaseTag -m "Release $releaseTag"
        if ($LASTEXITCODE -ne 0) {
            throw "Failed to create Git tag $releaseTag."
        }
        Write-Host "  [OK] Created tag $releaseTag" -ForegroundColor Green
    }

    Write-Host ""
    Write-Host "Release prepared successfully." -ForegroundColor Cyan
    Write-Host "Next commands:" -ForegroundColor White
    Write-Host "  git push origin main" -ForegroundColor Gray
    Write-Host "  git push origin $releaseTag" -ForegroundColor Gray
    Write-Host "Then create GitHub Release $releaseTag and attach $archiveName." -ForegroundColor Gray
}
finally {
    Pop-Location
}

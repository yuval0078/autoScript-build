param(
    [switch]$Push
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Push-Location $projectRoot

try {
    if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
        throw "Git is not installed or not available in PATH."
    }

    $version = (python -c "from project_version import APP_VERSION; print(APP_VERSION)" 2>&1).Trim()
    if ($LASTEXITCODE -ne 0 -or $version -notmatch '^\d+\.\d+\.\d+([.-][0-9A-Za-z.-]+)?$') {
        throw "project_version.py does not contain a valid semantic version."
    }

    $tag = "v$version"
    $status = git status --porcelain
    if ($status) {
        throw "The working tree is not clean. Commit or stash changes before creating $tag."
    }

    git rev-parse -q --verify "refs/tags/$tag" *> $null
    if ($LASTEXITCODE -eq 0) {
        throw "Tag $tag already exists. Increase APP_VERSION before releasing again."
    }

    git tag -a $tag -m "Release $tag"
    Write-Host "Created annotated tag $tag" -ForegroundColor Green

    if ($Push) {
        git push origin $tag
        if ($LASTEXITCODE -ne 0) {
            throw "Failed to push $tag to origin."
        }
        Write-Host "Pushed $tag to origin" -ForegroundColor Green
    }
    else {
        Write-Host "Tag was created locally only. Push it with:" -ForegroundColor Yellow
        Write-Host "  git push origin $tag"
    }
}
finally {
    Pop-Location
}

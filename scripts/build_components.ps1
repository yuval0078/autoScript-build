param(
    [ValidateSet("interface", "builder", "runner", "analyzer")]
    [string[]]$Components = @("interface", "builder", "runner", "analyzer"),
    [string]$OutputDirectory = "component-artifacts"
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$projectRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$versionsPath = Join-Path $projectRoot "component_versions.json"
$specRoot = Join-Path $projectRoot "packaging\pyinstaller"
$buildRoot = Join-Path $projectRoot "build\components"
$outputRoot = if ([System.IO.Path]::IsPathRooted($OutputDirectory)) {
    $OutputDirectory
} else {
    Join-Path $projectRoot $OutputDirectory
}
$bootstrapArtifact = $null

function Write-Utf8NoBom {
    param(
        [Parameter(Mandatory = $true)][string]$LiteralPath,
        [Parameter(Mandatory = $true)][string]$Content
    )
    $encoding = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($LiteralPath, $Content, $encoding)
}

$componentConfig = @{
    interface = @{ Spec = "Interface.spec"; Bundle = "AutoScriptInterface"; Entrypoint = "AutoScriptInterface.exe" }
    builder   = @{ Spec = "Builder.spec";   Bundle = "Builder";             Entrypoint = "Builder.exe" }
    runner    = @{ Spec = "Runner.spec";    Bundle = "ExperimentRunner";    Entrypoint = "ExperimentRunner.exe" }
    analyzer  = @{ Spec = "Analyzer.spec";  Bundle = "Analyzer";            Entrypoint = "Analyzer.exe" }
}

if (-not (Test-Path -LiteralPath $versionsPath -PathType Leaf)) {
    throw "Missing component version registry: $versionsPath"
}
$versionDocument = Get-Content -LiteralPath $versionsPath -Raw -Encoding UTF8 | ConvertFrom-Json
if ($versionDocument.schema_version -ne 1) {
    throw "Unsupported component_versions.json schema."
}
$definedComponents = @($versionDocument.components.PSObject.Properties.Name | Sort-Object)
$expectedComponents = @($componentConfig.Keys | Sort-Object)
if (($definedComponents -join ",") -ne ($expectedComponents -join ",")) {
    throw "component_versions.json must define exactly: $($expectedComponents -join ', ')."
}

if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
    throw "Python is required to build AutoScript components."
}
python -c "from component_versions import load_component_versions; load_component_versions()"
if ($LASTEXITCODE -ne 0) {
    throw "component_versions.json failed strict validation."
}

$sourceCommit = $null
if (Get-Command git -ErrorAction SilentlyContinue) {
    $candidateCommit = (git -C $projectRoot rev-parse HEAD 2>$null)
    if ($LASTEXITCODE -eq 0 -and $candidateCommit -match "^[0-9a-f]{40}$") {
        $sourceCommit = $candidateCommit
    }
}
if (-not $sourceCommit) {
    throw "A Git checkout with a valid HEAD commit is required for component builds."
}

New-Item -ItemType Directory -Force -Path $buildRoot | Out-Null
New-Item -ItemType Directory -Force -Path $outputRoot | Out-Null
$artifacts = [ordered]@{}

foreach ($component in $Components) {
    $config = $componentConfig[$component]
    $version = [string]$versionDocument.components.$component
    if ($version -notmatch "^(0|[1-9][0-9]*)(\.(0|[1-9][0-9]*)){1,3}$") {
        throw "Invalid version '$version' for component '$component'."
    }

    $componentBuildRoot = Join-Path $buildRoot $component
    $workPath = Join-Path $componentBuildRoot "work"
    $distPath = Join-Path $componentBuildRoot "dist"
    if (Test-Path -LiteralPath $componentBuildRoot) {
        Remove-Item -LiteralPath $componentBuildRoot -Recurse -Force
    }
    New-Item -ItemType Directory -Force -Path $workPath, $distPath | Out-Null

    $specPath = Join-Path $specRoot $config.Spec
    Write-Host "Building $component $version from $($config.Spec)..." -ForegroundColor Cyan
    python -m PyInstaller --noconfirm --clean --workpath $workPath --distpath $distPath $specPath
    if ($LASTEXITCODE -ne 0) {
        throw "PyInstaller failed for component '$component'."
    }

    $bundlePath = Join-Path $distPath $config.Bundle
    $entrypointPath = Join-Path $bundlePath $config.Entrypoint
    if (-not (Test-Path -LiteralPath $entrypointPath -PathType Leaf)) {
        throw "Component '$component' did not produce $($config.Entrypoint)."
    }

    $descriptor = [ordered]@{
        format_version = 1
        id = $component
        version = $version
        platform = "windows"
        arch = "x86_64"
        entrypoint = $config.Entrypoint
        protocol_version = 1
        source_commit = $sourceCommit
    }
    $descriptorPath = Join-Path $bundlePath "component.json"
    Write-Utf8NoBom -LiteralPath $descriptorPath `
        -Content ($descriptor | ConvertTo-Json -Depth 4)

    $archiveName = "AutoScript-$component-$version-windows-x86_64.zip"
    $archivePath = Join-Path $outputRoot $archiveName
    if (Test-Path -LiteralPath $archivePath) {
        Remove-Item -LiteralPath $archivePath -Force
    }
    Compress-Archive -Path (Join-Path $bundlePath "*") -DestinationPath $archivePath -CompressionLevel Optimal

    $archiveHash = (Get-FileHash -LiteralPath $archivePath -Algorithm SHA256).Hash.ToLowerInvariant()
    $archiveSize = (Get-Item -LiteralPath $archivePath).Length
    $checksumPath = "$archivePath.sha256"
    "$archiveHash  $archiveName" | Set-Content -LiteralPath $checksumPath -Encoding ascii

    $artifacts[$component] = [ordered]@{
        version = $version
        filename = $archiveName
        sha256 = $archiveHash
        size = $archiveSize
        entrypoint = $config.Entrypoint
        protocol = 1
    }
    Write-Host "Created $archiveName ($archiveSize bytes)" -ForegroundColor Green
}

if ($Components -contains "interface") {
    $interfaceVersion = [string]$versionDocument.components.interface
    $launcherBuildRoot = Join-Path $buildRoot "launcher"
    $launcherWorkPath = Join-Path $launcherBuildRoot "work"
    $launcherDistPath = Join-Path $launcherBuildRoot "dist"
    if (Test-Path -LiteralPath $launcherBuildRoot) {
        Remove-Item -LiteralPath $launcherBuildRoot -Recurse -Force
    }
    New-Item -ItemType Directory -Force -Path $launcherWorkPath, $launcherDistPath | Out-Null
    Write-Host "Building stable external bootstrap..." -ForegroundColor Cyan
    python -m PyInstaller --noconfirm --clean `
        --workpath $launcherWorkPath `
        --distpath $launcherDistPath `
        (Join-Path $specRoot "Launcher.spec")
    if ($LASTEXITCODE -ne 0) {
        throw "PyInstaller failed for the stable bootstrap."
    }

    $launcherBundle = Join-Path $launcherDistPath "AutoScriptLauncher"
    $interfaceBundle = Join-Path $buildRoot "interface\dist\AutoScriptInterface"
    $bootstrapStage = Join-Path $buildRoot "bootstrap-stage"
    if (Test-Path -LiteralPath $bootstrapStage) {
        Remove-Item -LiteralPath $bootstrapStage -Recurse -Force
    }
    New-Item -ItemType Directory -Force -Path $bootstrapStage | Out-Null

    $launcherNames = @(Get-ChildItem -LiteralPath $launcherBundle | ForEach-Object Name)
    $interfaceNames = @(Get-ChildItem -LiteralPath $interfaceBundle | ForEach-Object Name)
    $collisions = @($launcherNames | Where-Object { $_ -in $interfaceNames })
    if ($collisions.Count -gt 0) {
        throw "Bootstrap bundle collision: $($collisions -join ', ')."
    }
    Get-ChildItem -LiteralPath $launcherBundle | Copy-Item -Destination $bootstrapStage -Recurse
    Get-ChildItem -LiteralPath $interfaceBundle | Copy-Item -Destination $bootstrapStage -Recurse

    $bootstrapDescriptor = [ordered]@{
        format_version = 1
        bootstrap_version = $interfaceVersion
        platform = "windows"
        arch = "x86_64"
        entrypoint = "AutoScriptLauncher.exe"
        bundled_interface = "AutoScriptInterface.exe"
        protocol_version = 1
        source_commit = $sourceCommit
    }
    Write-Utf8NoBom -LiteralPath (Join-Path $bootstrapStage "bootstrap.json") `
        -Content ($bootstrapDescriptor | ConvertTo-Json -Depth 4)

    $bootstrapName = "AutoScript-bootstrap-$interfaceVersion-windows-x86_64.zip"
    $bootstrapPath = Join-Path $outputRoot $bootstrapName
    if (Test-Path -LiteralPath $bootstrapPath) {
        Remove-Item -LiteralPath $bootstrapPath -Force
    }
    Compress-Archive -Path (Join-Path $bootstrapStage "*") `
        -DestinationPath $bootstrapPath -CompressionLevel Optimal
    $bootstrapHash = (Get-FileHash -LiteralPath $bootstrapPath -Algorithm SHA256).Hash.ToLowerInvariant()
    $bootstrapSize = (Get-Item -LiteralPath $bootstrapPath).Length
    "$bootstrapHash  $bootstrapName" |
        Set-Content -LiteralPath "$bootstrapPath.sha256" -Encoding ascii
    $bootstrapArtifact = [ordered]@{
        version = $interfaceVersion
        filename = $bootstrapName
        sha256 = $bootstrapHash
        size = $bootstrapSize
        entrypoint = "AutoScriptLauncher.exe"
        bundled_interface = "AutoScriptInterface.exe"
        protocol = 1
    }
    Write-Host "Created $bootstrapName ($bootstrapSize bytes)" -ForegroundColor Green
}

$artifactManifest = [ordered]@{
    schema_version = 1
    platform = "windows"
    architecture = "x86_64"
    source_commit = $sourceCommit
    components = $artifacts
}
if ($null -ne $bootstrapArtifact) {
    $artifactManifest["bootstrap"] = $bootstrapArtifact
}
$manifestPath = Join-Path $outputRoot "component-artifacts.json"
Write-Utf8NoBom -LiteralPath $manifestPath `
    -Content ($artifactManifest | ConvertTo-Json -Depth 8)
Write-Host "Component artifacts: $outputRoot" -ForegroundColor Green

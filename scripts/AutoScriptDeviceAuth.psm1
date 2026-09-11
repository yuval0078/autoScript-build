$script:DefaultApiUrl = 'https://api.autoscript-lab.org'
$script:DefaultTokenPath = Join-Path $env:LOCALAPPDATA 'AutoScript\credentials\powershell-device-token.json'
$script:ModulePath = Join-Path $PSScriptRoot 'AutoScriptDeviceAuth.psm1'

function Assert-AutoScriptApiUrl {
    param([Parameter(Mandatory)][string]$ApiUrl)

    try {
        $uri = [System.Uri]::new($ApiUrl)
    }
    catch {
        throw 'ApiUrl must be a valid absolute HTTP or HTTPS URL.'
    }
    $isLocal = $uri.Host -in @('localhost', '127.0.0.1', '::1')
    if ($uri.Scheme -ne 'https' -and -not ($uri.Scheme -eq 'http' -and $isLocal)) {
        throw 'Device-token enrollment requires HTTPS, except for a local development API.'
    }
    return $uri.AbsoluteUri.TrimEnd('/')
}

function ConvertFrom-AutoScriptSecureString {
    param([Parameter(Mandatory)][Security.SecureString]$SecureValue)

    $pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($SecureValue)
    try {
        return [Runtime.InteropServices.Marshal]::PtrToStringBSTR($pointer)
    }
    finally {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($pointer)
    }
}

function Read-AutoScriptDeviceTokenRecord {
    param([Parameter(Mandatory)][string]$TokenPath)

    if (-not (Test-Path -LiteralPath $TokenPath -PathType Leaf)) {
        throw "No saved AutoScript device token was found at $TokenPath."
    }
    $record = Get-Content -LiteralPath $TokenPath -Raw -Encoding utf8 | ConvertFrom-Json
    if (
        $record.schema_version -ne 1 -or
        -not $record.api_url -or
        -not $record.token_id -or
        -not $record.protected_token
    ) {
        throw 'The saved AutoScript device-token record is invalid.'
    }
    return $record
}

function Register-AutoScriptDeviceToken {
    [CmdletBinding()]
    param(
        [string]$ApiUrl = $script:DefaultApiUrl,
        [string]$Username = 'admin',
        [string]$Label = "$env:COMPUTERNAME/$env:USERNAME PowerShell",
        [string]$TokenPath = $script:DefaultTokenPath
    )

    $ErrorActionPreference = 'Stop'
    $ApiUrl = Assert-AutoScriptApiUrl $ApiUrl
    if (Test-Path -LiteralPath $TokenPath) {
        throw "A device token is already saved at $TokenPath. Revoke it before enrolling another."
    }
    $credential = Get-Credential -UserName $Username -Message 'Authenticate to enroll this Windows device'
    if ($null -eq $credential) {
        throw 'Device-token enrollment was cancelled.'
    }

    $passwordText = ConvertFrom-AutoScriptSecureString $credential.Password
    $loginToken = $null
    $deviceToken = $null
    try {
        $loginBody = @{
            username = $credential.UserName
            password = $passwordText
        } | ConvertTo-Json -Compress
        $loginRequest = @{
            Method = 'Post'
            Uri = "$ApiUrl/api/v1/auth/login"
            ContentType = 'application/json'
            Body = $loginBody
        }
        $login = Invoke-RestMethod @loginRequest
        $loginToken = [string]$login.access_token

        $deviceBody = @{
            label = $Label
            password = $passwordText
        } | ConvertTo-Json -Compress
        $headers = @{ Authorization = "Bearer $loginToken" }
        $deviceRequest = @{
            Method = 'Post'
            Uri = "$ApiUrl/api/v1/auth/device-tokens"
            Headers = $headers
            ContentType = 'application/json'
            Body = $deviceBody
        }
        try {
            $issued = Invoke-RestMethod @deviceRequest
        }
        catch {
            $statusCode = $null
            if ($null -ne $_.Exception.Response) {
                $statusCode = [int]$_.Exception.Response.StatusCode
            }
            if ($statusCode -eq 404) {
                throw 'This AutoScript server does not support device tokens yet. Deploy migration 20260911_0016 and the matching API version, then retry enrollment.'
            }
            throw
        }
        $deviceToken = [string]$issued.device_token
        if (-not $deviceToken.StartsWith('asd_')) {
            throw 'The AutoScript API returned an invalid device token.'
        }

        $secureToken = ConvertTo-SecureString -String $deviceToken -AsPlainText -Force
        $record = [ordered]@{
            schema_version = 1
            api_url = $ApiUrl
            token_id = [string]$issued.id
            label = [string]$issued.label
            protected_token = ConvertFrom-SecureString $secureToken
            created_at = [string]$issued.created_at
        }
        $directory = Split-Path -Parent ([System.IO.Path]::GetFullPath($TokenPath))
        New-Item -ItemType Directory -Path $directory -Force | Out-Null
        $record | ConvertTo-Json | Set-Content -LiteralPath $TokenPath -Encoding utf8

        $env:AUTOSCRIPT_API_URL = $ApiUrl
        $env:AUTOSCRIPT_API_TOKEN = $deviceToken
        [pscustomobject]@{
            ApiUrl = $ApiUrl
            Username = $credential.UserName
            Label = $issued.label
            TokenId = $issued.id
            SavedAt = [System.IO.Path]::GetFullPath($TokenPath)
        }
    }
    finally {
        if ($loginToken) {
            try {
                $logoutRequest = @{
                    Method = 'Post'
                    Uri = "$ApiUrl/api/v1/auth/logout"
                    Headers = @{ Authorization = "Bearer $loginToken" }
                }
                Invoke-RestMethod @logoutRequest | Out-Null
            }
            catch {
                Write-Warning 'The temporary enrollment session could not be logged out.'
            }
        }
        $passwordText = $null
        $loginBody = $null
        $deviceBody = $null
        $loginToken = $null
        $deviceToken = $null
    }
}

function Connect-AutoScriptDeviceToken {
    [CmdletBinding()]
    param([string]$TokenPath = $script:DefaultTokenPath)

    $ErrorActionPreference = 'Stop'
    $record = Read-AutoScriptDeviceTokenRecord $TokenPath
    $ApiUrl = Assert-AutoScriptApiUrl ([string]$record.api_url)
    try {
        $secureToken = ConvertTo-SecureString ([string]$record.protected_token)
        $deviceToken = ConvertFrom-AutoScriptSecureString $secureToken
    }
    catch {
        throw 'Windows could not decrypt this device token. Use the same Windows account and computer that enrolled it.'
    }

    $env:AUTOSCRIPT_API_URL = $ApiUrl
    $env:AUTOSCRIPT_API_TOKEN = $deviceToken
    try {
        $meRequest = @{
            Method = 'Get'
            Uri = "$ApiUrl/api/v1/auth/me"
            Headers = @{ Authorization = "Bearer $deviceToken" }
        }
        $user = Invoke-RestMethod @meRequest
    }
    catch {
        $env:AUTOSCRIPT_API_TOKEN = $null
        throw 'The saved AutoScript device token is invalid or revoked. Enroll this device again.'
    }
    finally {
        $deviceToken = $null
    }
    return $user
}

function Unregister-AutoScriptDeviceToken {
    [CmdletBinding()]
    param(
        [string]$TokenPath = $script:DefaultTokenPath,
        [switch]$KeepAutoConnect
    )

    $ErrorActionPreference = 'Stop'
    $record = Read-AutoScriptDeviceTokenRecord $TokenPath
    $ApiUrl = Assert-AutoScriptApiUrl ([string]$record.api_url)
    $secureToken = ConvertTo-SecureString ([string]$record.protected_token)
    $deviceToken = ConvertFrom-AutoScriptSecureString $secureToken
    try {
        $revokeRequest = @{
            Method = 'Delete'
            Uri = "$ApiUrl/api/v1/auth/device-tokens/$($record.token_id)"
            Headers = @{ Authorization = "Bearer $deviceToken" }
        }
        Invoke-RestMethod @revokeRequest | Out-Null
        Remove-Item -LiteralPath $TokenPath -Force
        if ($env:AUTOSCRIPT_API_TOKEN -eq $deviceToken) {
            $env:AUTOSCRIPT_API_TOKEN = $null
        }
        if (-not $KeepAutoConnect) {
            Disable-AutoScriptDeviceTokenAutoConnect | Out-Null
        }
    }
    finally {
        $deviceToken = $null
    }
}

function Enable-AutoScriptDeviceTokenAutoConnect {
    [CmdletBinding()]
    param([string]$ProfilePath = $PROFILE.CurrentUserAllHosts)

    $ErrorActionPreference = 'Stop'
    $startMarker = '# >>> AutoScript device authentication >>>'
    $endMarker = '# <<< AutoScript device authentication <<<'
    $existing = ''
    if (Test-Path -LiteralPath $ProfilePath) {
        $loadedProfile = Get-Content -LiteralPath $ProfilePath -Raw
        if ($null -ne $loadedProfile) {
            $existing = [string]$loadedProfile
        }
    }
    if ($existing.Contains($startMarker)) {
        return [System.IO.Path]::GetFullPath($ProfilePath)
    }

    $profileDirectory = Split-Path -Parent ([System.IO.Path]::GetFullPath($ProfilePath))
    New-Item -ItemType Directory -Path $profileDirectory -Force | Out-Null
    $escapedModulePath = $script:ModulePath.Replace("'", "''")
    $lines = @(
        $startMarker,
        "Import-Module '$escapedModulePath' -Force",
        'try { Connect-AutoScriptDeviceToken | Out-Null } catch { Write-Warning (''AutoScript device authentication: '' + $_.Exception.Message) }',
        $endMarker
    )
    if ($existing.Length -gt 0 -and -not $existing.EndsWith([Environment]::NewLine)) {
        Add-Content -LiteralPath $ProfilePath -Value ''
    }
    Add-Content -LiteralPath $ProfilePath -Value ($lines -join [Environment]::NewLine)
    return [System.IO.Path]::GetFullPath($ProfilePath)
}

function Disable-AutoScriptDeviceTokenAutoConnect {
    [CmdletBinding()]
    param([string]$ProfilePath = $PROFILE.CurrentUserAllHosts)

    if (-not (Test-Path -LiteralPath $ProfilePath -PathType Leaf)) {
        return
    }
    $startMarker = '# >>> AutoScript device authentication >>>'
    $endMarker = '# <<< AutoScript device authentication <<<'
    $content = Get-Content -LiteralPath $ProfilePath -Raw
    $pattern = '(?ms)^' + [regex]::Escape($startMarker) + '.*?^' + [regex]::Escape($endMarker) + '(?:\r?\n)?'
    $updated = [regex]::Replace($content, $pattern, '')
    if ($updated -ne $content) {
        Set-Content -LiteralPath $ProfilePath -Value $updated -NoNewline -Encoding utf8
    }
}

Export-ModuleMember -Function @(
    'Register-AutoScriptDeviceToken',
    'Connect-AutoScriptDeviceToken',
    'Unregister-AutoScriptDeviceToken',
    'Enable-AutoScriptDeviceTokenAutoConnect',
    'Disable-AutoScriptDeviceTokenAutoConnect'
)

# PowerShell device authentication

AutoScript supports revocable, non-expiring device tokens for administrator
PowerShell sessions. A device token is separate from the 24-hour login token:

- its plaintext value is returned by the API only once;
- the server stores only its SHA-256 hash;
- the local PowerShell module protects it with Windows DPAPI, bound to the
  current Windows user on the current computer;
- it remains valid until it is explicitly revoked, the administrator password
  changes, or the administrator account is disabled;
- creating one requires both a valid administrator session and the current
  administrator password.

Never copy the protected token file to another computer or Windows account. It
will not decrypt there. Do not put the plaintext token in a PowerShell profile,
repository, command history, or user-level environment variable.

## Server deployment

Deploy the API version containing migration `20260911_0016`, then apply the
normal Alembic migration before enrollment:

```powershell
Set-Location backend
alembic upgrade head
```

## Enroll this Windows device once

Open PowerShell from the AutoScript project and run:

```powershell
Import-Module .\scripts\AutoScriptDeviceAuth.psm1 -Force
Register-AutoScriptDeviceToken
Enable-AutoScriptDeviceTokenAutoConnect
```

The credential window defaults to `admin` and accepts the current AutoScript
administrator password. The command does not print the token. By default, the
DPAPI-protected record is stored at:

```text
%LOCALAPPDATA%\AutoScript\credentials\powershell-device-token.json
```

`Enable-AutoScriptDeviceTokenAutoConnect` adds a small marked block to the
current user's all-hosts PowerShell profile. New PowerShell sessions then load
and validate the encrypted token automatically. It writes no plaintext token to
the profile.

For a different API deployment, pass its HTTPS URL:

```powershell
Register-AutoScriptDeviceToken -ApiUrl 'https://api.example.org'
```

## Connect in a new PowerShell session

```powershell
Import-Module .\scripts\AutoScriptDeviceAuth.psm1 -Force
Connect-AutoScriptDeviceToken
```

After validation, the module sets `AUTOSCRIPT_API_URL` and
`AUTOSCRIPT_API_TOKEN` only in the current PowerShell process. Programs launched
from that session inherit them.

## Revoke this device

```powershell
Unregister-AutoScriptDeviceToken
```

This revokes the server-side credential before removing the local encrypted
record and removes the marked auto-connect block from the PowerShell profile.
If a device is lost, revoke its token through the API from another
authenticated administrator session using the token ID returned during
enrollment or listed by `GET /api/v1/auth/device-tokens`.

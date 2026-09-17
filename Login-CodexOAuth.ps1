param(
    [Parameter(Mandatory = $true)][string]$HarPath,
    [string]$Codex = 'codex',
    [string]$Python = 'python',
    [switch]$DeviceAuth,
    [switch]$AcquireOnly
)
$ErrorActionPreference = 'Stop'
# Use the official CLI's PKCE/device flow, not a copied OAuth client implementation.
# This separate home never overwrites the user's normal Codex login.
$authHome = Join-Path $PSScriptRoot '.local\codex-oauth'
New-Item -ItemType Directory -Path $authHome -Force | Out-Null
$previousHome = $env:CODEX_HOME
try {
    $env:CODEX_HOME = $authHome
    $arguments = @('login', '-c', 'cli_auth_credentials_store="file"')
    if ($DeviceAuth) { $arguments += '--device-auth' }
    & $Codex @arguments
    if ($LASTEXITCODE -ne 0) { throw 'Codex OAuth login did not complete.' }
} finally {
    if ($null -eq $previousHome) { Remove-Item Env:CODEX_HOME -ErrorAction SilentlyContinue }
    else { $env:CODEX_HOME = $previousHome }
}
if (-not $AcquireOnly) {
    $authFile = Join-Path $authHome 'auth.json'
    if (-not (Test-Path -LiteralPath $authFile -PathType Leaf)) { throw 'Codex did not create the expected isolated auth.json.' }
    Push-Location $PSScriptRoot
    try {
        # Import only access_token and require a real Prism session probe. A Codex
        # refresh_token is never mislabeled as a Prism refresh cookie.
        & $Python -m prism_bridge auth-import --har $HarPath --auth-file $authFile --auth-state '.local\codex-import-prism-auth.json'
        if ($LASTEXITCODE -ne 0) { throw 'OAuth login succeeded, but Prism did not validate this imported credential. No existing Prism auth state was replaced.' }
        Write-Host 'Prism accepted the access token. Use --auth-state .local\codex-import-prism-auth.json when starting the bridge.'
    } finally {
        Pop-Location
    }
}

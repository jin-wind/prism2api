param(
    [Parameter(Mandatory = $true)][string]$HarPath,
    [string]$CookieFile = (Join-Path $PSScriptRoot '.local\prism-cookie.txt'),
    [string]$AuthState = (Join-Path $PSScriptRoot '.local\prism-auth.json'),
    [string]$Python = 'python',
    [int]$Port = 8765
)
$ErrorActionPreference = 'Stop'
if (-not (Test-Path -LiteralPath $HarPath -PathType Leaf)) { throw 'HAR file not found.' }
if (-not (Test-Path -LiteralPath $CookieFile -PathType Leaf) -and -not (Test-Path -LiteralPath $AuthState -PathType Leaf)) {
    Write-Warning 'No cookie file or auth-state yet - start anyway, then log in from the web UI (/ui).'
}
$localDir = Join-Path $PSScriptRoot '.local'
New-Item -ItemType Directory -Path $localDir -Force | Out-Null
$keyFile = Join-Path $localDir 'bridge-key.txt'
if (-not (Test-Path -LiteralPath $keyFile)) {
    $key = [Guid]::NewGuid().ToString('N') + [Guid]::NewGuid().ToString('N')
    [System.IO.File]::WriteAllText($keyFile, $key)
}
$env:PRISM_BRIDGE_API_KEY = [System.IO.File]::ReadAllText($keyFile).Trim()
Push-Location $PSScriptRoot
try {
    Write-Host "Starting the experimental bridge on http://127.0.0.1:$Port/v1"
    Write-Host "Web UI: http://127.0.0.1:$Port/ui#key=$($env:PRISM_BRIDGE_API_KEY)"
    $arguments = @('-m', 'prism_bridge', 'serve', '--har', $HarPath, '--auth-state', $AuthState, '--port', "$Port")
    if (Test-Path -LiteralPath $CookieFile -PathType Leaf) { $arguments += @('--cookie-file', $CookieFile) }
    & $Python @arguments
    if ($LASTEXITCODE -ne 0) { throw "Bridge exited with code $LASTEXITCODE" }
} finally {
    Pop-Location
}

param(
    [string]$Codex = 'codex',
    [switch]$InstallProfileOnly,
    [Parameter(ValueFromRemainingArguments = $true)][string[]]$CodexArguments
)
$ErrorActionPreference = 'Stop'
$keyFile = Join-Path $PSScriptRoot '.local\bridge-key.txt'
if (-not (Test-Path -LiteralPath $keyFile -PathType Leaf)) { throw 'Run Start-Bridge.ps1 first.' }
$env:PRISM_BRIDGE_API_KEY = [System.IO.File]::ReadAllText($keyFile).Trim()
$codexHome = if ($env:CODEX_HOME) { $env:CODEX_HOME } else { Join-Path $env:USERPROFILE '.codex' }
New-Item -ItemType Directory -Path $codexHome -Force | Out-Null
$source = Join-Path $PSScriptRoot 'prism_bridge.config.toml'
$target = Join-Path $codexHome 'prism_bridge.config.toml'
if (Test-Path -LiteralPath $target) {
    if ([System.IO.File]::ReadAllText($target) -ne [System.IO.File]::ReadAllText($source)) {
        throw "An existing, different profile was preserved: $target"
    }
} else {
    Copy-Item -LiteralPath $source -Destination $target
}
Write-Host "Profile ready: $target (main config and auth files unchanged)"
if (-not $InstallProfileOnly) { & $Codex --profile prism_bridge @CodexArguments }


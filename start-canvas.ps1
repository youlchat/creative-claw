param(
    [string]$Database = ".creative-claw/demo.db",
    [int]$Port = 8766,
    [switch]$NoBrowser
)

# Backwards-compatible wrapper. New users should run start.ps1 or start.bat.
& (Join-Path $PSScriptRoot "start.ps1") -Database $Database -Port $Port -NoBrowser:$NoBrowser

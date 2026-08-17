param(
    [string]$Database = ".creative-claw/demo.db",
    [string]$ProjectRoot = ".creative-claw/projects/demo",
    [int]$Port = 8766,
    [string]$HostAddress = "127.0.0.1",
    [switch]$NoBrowser
)

$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot
$venvPython = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"

if (-not (Test-Path -LiteralPath $venvPython)) {
    $python = (Get-Command python -ErrorAction Stop).Source
    Write-Host "[1/4] Creating Python virtual environment .venv"
    & $python -m venv .venv
}

Write-Host "[2/4] Installing Creative Claw and dependencies"
& $venvPython -m pip install --disable-pip-version-check -e .
Write-Host "[3/4] Bootstrapping the idempotent demo project"
& $venvPython examples/bootstrap_demo.py --db $Database --project-root $ProjectRoot --project-id demo

$url = "http://127.0.0.1:$Port/"
if (-not $NoBrowser) { Start-Process $url }
Write-Host "[4/4] Creative Claw is running at $url"
Write-Host "Press Ctrl+C to stop. Configure an optional model in the web UI."
& $venvPython -m creative_claw --db $Database serve --host $HostAddress --port $Port

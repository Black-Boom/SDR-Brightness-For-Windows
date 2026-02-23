$ErrorActionPreference = 'Stop'

Set-Location $PSScriptRoot

if (!(Test-Path .venv)) {
    python -m venv .venv
}

.\.venv\Scripts\python -m pip install --upgrade pip
.\.venv\Scripts\python -m pip install -r requirements.txt pyinstaller

$addBinary = @()
if (Test-Path ".\tools\set_sdrwhite.exe") {
    $addBinary = @("--add-binary", "tools\set_sdrwhite.exe;.")
}

.\.venv\Scripts\pyinstaller `
    --noconfirm `
    --clean `
    --onefile `
    --windowed `
    --name HDR-SDR-Brightness `
    @addBinary `
    hdr_sdr_tray.py

Write-Host "Build done. Output: dist\HDR-SDR-Brightness.exe"

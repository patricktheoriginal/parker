# setup_venv312.ps1 — create a Python 3.12 virtual environment for Parker.
#
# Why: MediaPipe (used by gesture_control.py for hand-swipe play/pause)
# officially only supports Python 3.7-3.12. If your system Python is newer
# (e.g. 3.14), `pip install mediapipe` either fails or installs a broken
# build that raises "module 'mediapipe' has no attribute 'solutions'" at
# runtime. This script sets up an isolated Python 3.12 environment so
# MediaPipe (and everything else) works correctly, without touching your
# system Python.
#
# Usage (PowerShell):
#   powershell -ExecutionPolicy Bypass -File tools\setup_venv312.ps1
#
# After it finishes, run Parker with:
#   .\.venv312\Scripts\python.exe main.py
# (or activate the venv first: .\.venv312\Scripts\Activate.ps1)

$ErrorActionPreference = "Continue"

Write-Host "=== Parker: Python 3.12 environment setup ===" -ForegroundColor Cyan

# 1. Find an existing Python 3.12 install.
$py312 = $null
try {
    $candidates = & py -0p 2>$null | Select-String "3\.12"
    if ($candidates) {
        $py312 = "py"
        $pyArgs = "-3.12"
    }
} catch {}

if (-not $py312) {
    # Try the standard install path directly.
    $direct = "$env:LocalAppData\Programs\Python\Python312\python.exe"
    if (Test-Path $direct) {
        $py312 = $direct
        $pyArgs = ""
    }
}

if (-not $py312) {
    Write-Host "Python 3.12 not found. Installing via winget..." -ForegroundColor Yellow
    winget install --id Python.Python.3.12 -e --source winget
    if ($LASTEXITCODE -ne 0) {
        Write-Host "winget install failed. Install Python 3.12 manually from:" -ForegroundColor Red
        Write-Host "  https://www.python.org/downloads/release/python-3120/" -ForegroundColor Red
        Write-Host "Then re-run this script." -ForegroundColor Red
        exit 1
    }
    $direct = "$env:LocalAppData\Programs\Python\Python312\python.exe"
    if (Test-Path $direct) {
        $py312 = $direct
        $pyArgs = ""
    } else {
        Write-Host "Installed, but couldn't find python.exe automatically." -ForegroundColor Red
        Write-Host "Close and reopen PowerShell, then re-run this script." -ForegroundColor Red
        exit 1
    }
}

Write-Host "Using Python: $py312 $pyArgs" -ForegroundColor Green
if ($pyArgs) {
    & $py312 $pyArgs --version
} else {
    & $py312 --version
}

# 2. Create the venv (in the repo root, alongside main.py).
$venvPath = Join-Path (Get-Location) ".venv312"
if (Test-Path $venvPath) {
    Write-Host "$venvPath already exists — skipping creation." -ForegroundColor Yellow
} else {
    Write-Host "Creating virtual environment at $venvPath ..." -ForegroundColor Cyan
    if ($pyArgs) {
        & $py312 $pyArgs -m venv $venvPath
    } else {
        & $py312 -m venv $venvPath
    }
}

$venvPython = Join-Path $venvPath "Scripts\python.exe"
if (-not (Test-Path $venvPython)) {
    Write-Host "venv creation failed — $venvPython not found." -ForegroundColor Red
    exit 1
}

# 3. Install requirements into the venv.
Write-Host "Installing requirements (this can take a few minutes)..." -ForegroundColor Cyan
& $venvPython -m pip install --upgrade pip
& $venvPython -m pip install -r requirements.txt

Write-Host ""
Write-Host "=== Done ===" -ForegroundColor Green
Write-Host "Run Parker with the new environment:"
Write-Host "  .\.venv312\Scripts\python.exe main.py" -ForegroundColor Cyan
Write-Host "Or activate it first:"
Write-Host "  .\.venv312\Scripts\Activate.ps1" -ForegroundColor Cyan
Write-Host "  python main.py"

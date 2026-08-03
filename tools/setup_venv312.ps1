# setup_venv312.ps1 -- create a Python 3.12 virtual environment for Parker.
#
# Why: MediaPipe (used by gesture_control.py for hand-gesture play/pause)
# officially only supports Python 3.9-3.12. If your system Python is newer
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

$ErrorActionPreference = "Stop"

Write-Host "=== Parker: Python 3.12 environment setup ===" -ForegroundColor Cyan

function Get-ExePath($cmd) {
    $found = Get-Command $cmd -ErrorAction SilentlyContinue
    if ($found) { return $found.Source }
    return $null
}

# 1. Find an existing Python 3.12 install. Every path below either sets
#    $py312Exe to a real, verified python.exe, or leaves it $null -- never a
#    launcher name plus a separate args string, which is what broke the
#    earlier version of this script when the 'py' launcher wasn't present.
$py312Exe = $null

# 1a. The py launcher, if installed, can target 3.12 specifically.
$pyLauncher = Get-ExePath "py"
if ($pyLauncher) {
    try {
        $resolved = & $pyLauncher "-3.12" -c "import sys; print(sys.executable)" 2>$null
        if ($LASTEXITCODE -eq 0 -and $resolved -and (Test-Path $resolved.Trim())) {
            $py312Exe = $resolved.Trim()
        }
    } catch {
        # py launcher exists but has no 3.12 registered -- fall through.
    }
}

# 1b. Standard per-user install location.
if (-not $py312Exe) {
    $direct = Join-Path $env:LocalAppData "Programs\Python\Python312\python.exe"
    if (Test-Path $direct) { $py312Exe = $direct }
}

# 1c. Not found anywhere -- install it.
if (-not $py312Exe) {
    Write-Host "Python 3.12 not found. Installing via winget..." -ForegroundColor Yellow
    winget install --id Python.Python.3.12 -e --source winget
    if ($LASTEXITCODE -ne 0) {
        Write-Host "winget install failed. Install Python 3.12 manually from:" -ForegroundColor Red
        Write-Host "  https://www.python.org/downloads/release/python-3120/" -ForegroundColor Red
        Write-Host "Then re-run this script." -ForegroundColor Red
        exit 1
    }
    $direct = Join-Path $env:LocalAppData "Programs\Python\Python312\python.exe"
    if (Test-Path $direct) {
        $py312Exe = $direct
    } else {
        Write-Host "Installed, but couldn't find python.exe automatically." -ForegroundColor Red
        Write-Host "Close and reopen PowerShell, then re-run this script." -ForegroundColor Red
        exit 1
    }
}

Write-Host "Using Python: $py312Exe" -ForegroundColor Green
& $py312Exe --version
if ($LASTEXITCODE -ne 0) {
    Write-Host "That Python executable doesn't run -- aborting." -ForegroundColor Red
    exit 1
}

# 2. Create the venv (in the repo root, alongside main.py).
$venvPath = Join-Path (Get-Location) ".venv312"
$venvPython = Join-Path $venvPath "Scripts\python.exe"

if (Test-Path $venvPython) {
    Write-Host "$venvPath already exists -- skipping creation." -ForegroundColor Yellow
} else {
    Write-Host "Creating virtual environment at $venvPath ..." -ForegroundColor Cyan
    & $py312Exe -m venv $venvPath
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path $venvPython)) {
        Write-Host "venv creation failed -- $venvPython was not created." -ForegroundColor Red
        exit 1
    }
}

# 3. Install requirements into the venv.
Write-Host "Installing requirements (this can take a few minutes)..." -ForegroundColor Cyan
& $venvPython -m pip install --upgrade pip
& $venvPython -m pip install -r requirements.txt
if ($LASTEXITCODE -ne 0) {
    Write-Host "pip install reported errors -- check the output above." -ForegroundColor Yellow
}

Write-Host ""
Write-Host "=== Done ===" -ForegroundColor Green
Write-Host "Run Parker with the new environment:"
Write-Host "  .\.venv312\Scripts\python.exe main.py" -ForegroundColor Cyan
Write-Host "Or activate it first:"
Write-Host "  .\.venv312\Scripts\Activate.ps1" -ForegroundColor Cyan
Write-Host "  python main.py"

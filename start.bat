@echo off
REM start.bat -- shortcut to launch Parker with the Python 3.12 venv
REM (needed for MediaPipe/gesture control). Run from anywhere by double-
REM clicking, or from a terminal with:  start.bat
cd /d "%~dp0"
if exist ".venv312\Scripts\python.exe" (
    ".venv312\Scripts\python.exe" main.py
) else (
    echo .venv312 not found. Run this first:
    echo   powershell -ExecutionPolicy Bypass -File tools\setup_venv312.ps1
    pause
)

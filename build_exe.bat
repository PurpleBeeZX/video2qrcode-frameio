@echo off
REM ===========================================================================
REM  Frame.io QR Code Automation System — Executable Builder
REM ===========================================================================
REM  This script builds a standalone Windows executable using PyInstaller.
REM  The resulting .exe can be copied to any Windows machine without Python.
REM ===========================================================================

cd /d "%~dp0"

echo ========================================
echo  Building Frame.io QR Code Uploader
echo ========================================
echo.

REM --- Check if PyInstaller is installed ---
pip show pyinstaller >nul 2>&1
if %errorlevel% neq 0 (
    echo [BUILD] PyInstaller not found. Installing...
    pip install pyinstaller==6.4.0
    if %errorlevel% neq 0 (
        echo [ERROR] Failed to install PyInstaller.
        pause
        exit /b 1
    )
)

REM --- Launch the Python build script ---
python build_exe.py

if %errorlevel% neq 0 (
    echo [ERROR] Build failed. See output above.
    pause
    exit /b 1
)

pause

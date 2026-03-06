@echo off
REM ============================================================================
REM  JeevesBot Build Script
REM  Compiles Jeeves into a standalone executable using PyInstaller.
REM ============================================================================
REM
REM  Prerequisites:
REM    1. Python 3.10+ installed and on PATH
REM    2. Run once:  pip install -r requirements.txt
REM
REM  Usage:
REM    Double-click build.bat from the JeevesBot folder.
REM    Output:  dist\Jeeves\Jeeves.exe
REM
REM  After building:
REM    1. Copy the entire dist\Jeeves\ folder to your server
REM    2. Place config.env next to Jeeves.exe
REM    3. Run Jeeves.exe
REM ============================================================================

echo.
echo ============================================
echo   JeevesBot Build Script
echo ============================================
echo.

REM Check Python
python --version >nul 2>&1
if errorlevel 1 (
    echo ERROR: Python not found on PATH.
    echo Install Python 3.10+ from https://python.org
    pause
    exit /b 1
)

REM Install dependencies (includes PyInstaller)
echo Installing/verifying dependencies...
python -m pip install -r requirements.txt
if errorlevel 1 (
    echo ERROR: Failed to install dependencies.
    pause
    exit /b 1
)

echo.
echo Building Jeeves.exe...
echo.

REM Use "python -m PyInstaller" instead of bare "pyinstaller" command
REM to avoid PATH issues with Windows Store Python and user-level installs
python -m PyInstaller Jeeves.spec --noconfirm --clean

if errorlevel 1 (
    echo.
    echo ERROR: Build failed! Check output above.
    pause
    exit /b 1
)

REM Copy config to output
if exist "config.env.example" copy /y "config.env.example" "dist\Jeeves\config.env.example" >nul

echo.
echo ============================================
echo   Build complete!
echo   Output: dist\Jeeves\Jeeves.exe
echo ============================================
echo.
pause

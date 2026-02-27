@echo off
echo ============================================
echo  JeevesBot Build Script
echo ============================================
echo.

REM Check Python is available
python --version >nul 2>&1
if errorlevel 1 (
    echo ERROR: Python is not installed or not in PATH.
    echo Download Python from https://www.python.org/downloads/
    echo Make sure to check "Add Python to PATH" during installation.
    pause
    exit /b 1
)

echo [1/3] Installing dependencies...
echo Removing obsolete backport packages...
python -m pip uninstall typing pathlib configparser importlib-metadata functools32 enum34 -y >nul 2>&1
python -m pip install -r requirements.txt --quiet
if errorlevel 1 (
    echo ERROR: Failed to install dependencies.
    pause
    exit /b 1
)

echo [2/3] Building executable...
python -m PyInstaller Jeeves.spec --noconfirm --clean
if errorlevel 1 (
    echo ERROR: PyInstaller build failed.
    pause
    exit /b 1
)

echo [3/3] Packaging distribution...

REM Copy config example into the dist folder
copy /Y config.env.example dist\Jeeves\config.env.example >nul
copy /Y README.md dist\Jeeves\README.md >nul

REM Create empty config.env if it doesn't exist in dist
if not exist dist\Jeeves\config.env (
    copy /Y config.env.example dist\Jeeves\config.env >nul
)

echo.
echo ============================================
echo  BUILD COMPLETE
echo ============================================
echo.
echo Distribution folder: dist\Jeeves\
echo.
echo To distribute:
echo   1. Zip the dist\Jeeves\ folder
echo   2. Users extract, edit config.env, run Jeeves.exe
echo.
pause

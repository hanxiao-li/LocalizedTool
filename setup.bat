@echo off
setlocal
cd /d "%~dp0"
echo ==============================================
echo   LocalizedTool - Setup
echo ==============================================
echo.

echo [1/3] Checking Python ...
python --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python 3.10+ not found. Please install Python and add it to PATH.
    pause
    exit /b 1
)

echo [2/3] Creating virtual environment .venv ...
if not exist ".venv" (
    python -m venv .venv
    if errorlevel 1 (
        echo [ERROR] Failed to create virtual environment.
        pause
        exit /b 1
    )
)

echo [3/3] Installing dependencies (first run may take a few minutes) ...
".venv\Scripts\python.exe" -m pip install --upgrade pip
if errorlevel 1 (
    echo [ERROR] Failed to upgrade pip.
    pause
    exit /b 1
)
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 (
    echo [ERROR] Dependency installation failed. Check your network and retry.
    pause
    exit /b 1
)

echo.
echo ==============================================
echo   Setup complete! Double-click run.bat to start.
echo   Default admin account: admin / admin123
echo ==============================================
pause

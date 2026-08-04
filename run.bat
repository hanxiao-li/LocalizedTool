@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo Environment not ready. Please run setup.bat first.
    pause
    exit /b 1
)

rem Generate self-signed HTTPS cert if missing (encrypted LAN access)
if not exist "data\cert.pem" (
    echo Generating HTTPS certificate ...
    ".venv\Scripts\python.exe" gen_cert.py
    if errorlevel 1 (
        echo Certificate generation failed.
        pause
        exit /b 1
    )
)

echo Starting server (press Ctrl+C in the server window to stop) ...
start "LocalizedTool-Server" ".venv\Scripts\python.exe" app.py

echo Waiting for the server to be ready ...
:waitloop
timeout /t 1 /nobreak >nul
curl -k -s -o nul https://127.0.0.1:5000 2>nul
if errorlevel 1 goto waitloop

echo Server ready. Opening browser ...
start "" "https://127.0.0.1:5000/login"
if errorlevel 1 (
    echo Could not open browser automatically. Please open it manually:
    echo   https://127.0.0.1:5000/login
)
echo.
echo Note: to stop the server, press Ctrl+C in the server window.
echo Others on the LAN can access via: https://<your-LAN-IP>:5000/login
pause

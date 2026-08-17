@echo off
cd /d "%~dp0"
set "APP_URL=https://desktop.tail27cee7.ts.net/shocks_art/"
echo Starting Shocks Art Content System...
echo Opening %APP_URL%
start "" powershell -NoProfile -WindowStyle Hidden -Command "Start-Sleep -Seconds 3; Start-Process '%APP_URL%'"
py -3.13 -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
pause

@echo off
setlocal
cd /d "%~dp0.."

if not exist ".venv\Scripts\python.exe" (
  echo [ERROR] Python venv not found at .venv
  echo Create it with:
  echo     python -m venv .venv
  echo     .venv\Scripts\python -m pip install -r backend\requirements.txt
  exit /b 1
)

if not exist "frontend\node_modules" (
  echo [ERROR] frontend\node_modules not found. Run: cd frontend ^&^& npm install
  exit /b 1
)

echo Starting backend  -^> http://127.0.0.1:8000  (docs: /docs)
start "XHS backend" cmd /k ".venv\Scripts\python.exe -m uvicorn backend.main:app --host 127.0.0.1 --port 8000"

echo Starting frontend -^> http://127.0.0.1:3000
start "XHS frontend" cmd /k "cd frontend && npm run dev -- -H 127.0.0.1"

echo.
echo Both services launched in separate windows.
echo   Frontend : http://127.0.0.1:3000
echo   Backend  : http://127.0.0.1:8000/docs
echo   Login    : admin / RubyRain2026!
endlocal

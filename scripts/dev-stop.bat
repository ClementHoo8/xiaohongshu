@echo off
setlocal
echo Stopping services on ports 8000 (backend) and 3000 (frontend)...
for %%P in (8000 3000) do (
  for /f "tokens=5" %%A in ('netstat -ano ^| findstr ":%%P" ^| findstr "LISTENING"') do (
    echo   port %%P -^> killing PID %%A
    taskkill /F /PID %%A >nul 2>&1
  )
)
echo Done.
endlocal

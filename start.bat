@echo off
title HM Data Extractor
echo ========================================
echo   HM Data Extractor - Starting...
echo ========================================
echo.

echo [1/2] Starting FastAPI backend on port 8000...
start "HM Backend" cmd /c "cd /d %~dp0 && python api.py"

echo [2/2] Starting Next.js frontend on port 3000...
start "HM Frontend" cmd /c "cd /d %~dp0\frontend && npm run dev"

timeout /t 4 /nobreak >nul
echo.
echo ========================================
echo   Backend:  http://localhost:8000
echo   Frontend: http://localhost:3000
echo ========================================
echo.
echo Opening browser...
start http://localhost:3000
echo.
echo Press any key to stop both servers...
pause >nul

echo Stopping servers...
taskkill /FI "WINDOWTITLE eq HM Backend*" /F >nul 2>&1
taskkill /FI "WINDOWTITLE eq HM Frontend*" /F >nul 2>&1
echo Done.

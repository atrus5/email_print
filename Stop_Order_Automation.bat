@echo off
title PuzzleMaster — Stop Automation

echo ======================================================
echo   PuzzleMaster Order Automation — Stopping
echo ======================================================
echo.

:: Stop the Python script
echo Stopping Python automation script...
taskkill /F /IM python.exe /T >nul 2>&1
if %errorlevel% == 0 (
    echo   [OK] Python process stopped.
) else (
    echo   [--] No Python process was running.
)

:: Ask if Chrome should also be closed
echo.
set /P CLOSE_CHROME=Close Chrome debug session too? (y/n): 
if /i "%CLOSE_CHROME%"=="y" (
    taskkill /F /IM chrome.exe /T >nul 2>&1
    echo   [OK] Chrome closed.
)

echo.
echo Automation has been stopped.
echo Check system_log.txt and success_log.txt for a record of activity.
echo.
pause

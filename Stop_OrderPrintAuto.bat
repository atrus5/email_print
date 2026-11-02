@echo off
REM ============================================================
REM  Graceful stop for OrderPrintAuto (used by EmailPrint_Stop)
REM
REM  1. Write stop_trigger.txt - the control panel polls for this
REM     file every 2s and shuts itself down cleanly (stops every
REM     worker, closes every automation Chrome session).
REM  2. Wait up to 60 seconds for OrderPrintAuto.exe to exit.
REM  3. If it is still alive: normal taskkill (WM_CLOSE), then
REM     /F as a last resort.
REM  4. Sweep any leftover workers / automation Chrome sessions
REM     (the panel's own cleanup covers these when it is alive;
REM     this only matters if it crashed earlier).
REM
REM  No prompts, no pause - it must run unattended.
REM  Uses findstr (not find) so a Unix find.exe on PATH can never
REM  silently break the wait loop.
REM ============================================================
setlocal
set "APPDIR=%~dp0"
set "TRIGGER=%APPDIR%stop_trigger.txt"

echo stop> "%TRIGGER%"

set /a waited=0
:waitloop
ping -n 3 127.0.0.1 >nul
set /a waited+=2
tasklist /FI "IMAGENAME eq OrderPrintAuto.exe" 2>nul | findstr /I /C:"OrderPrintAuto.exe" >nul
if errorlevel 1 goto :panelgone
if %waited% lss 60 goto :waitloop

REM Panel ignored the trigger or is hung: ask nicely, then force.
taskkill /IM OrderPrintAuto.exe >nul 2>&1
ping -n 6 127.0.0.1 >nul
taskkill /F /IM OrderPrintAuto.exe >nul 2>&1

:panelgone
del /q "%TRIGGER%" >nul 2>&1

REM Leftover sweep: the three worker scripts and the automation Chrome
REM debug sessions (ports 9222-9225 only - personal Chrome untouched).
powershell -NoProfile -Command "Get-CimInstance Win32_Process | Where-Object { $_.ProcessId -ne $PID -and $_.CommandLine -match 'order_print\.py|etsy_print\.py|shopify_print\.py|remote-debugging-port=922[2-5]' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }" >nul 2>&1

exit /b 0

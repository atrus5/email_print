# PuzzleMaster - Setup and Start
$Host.UI.RawUI.WindowTitle = "PuzzleMaster - Setup and Start"

# ── Configuration ─────────────────────────────────────────────
$ScriptDir = $PSScriptRoot
$ChromePath = "C:\Program Files\Google\Chrome\Application\chrome.exe"
$DashboardPort = 8080
$LoginWait = 30
# ─────────────────────────────────────────────────────────────

Write-Host "======================================================" -ForegroundColor Cyan
Write-Host "  PuzzleMaster Order Automation - First Time Setup" -ForegroundColor Cyan
Write-Host "======================================================" -ForegroundColor Cyan
Write-Host ""

# ── STEP 1: Check Python ─────────────────────────────────────
Write-Host "[1/7] Checking Python installation..." -ForegroundColor Yellow
try {
    $pythonVer = python --version 2>&1
    Write-Host "  Found: $pythonVer"
} catch {
    Write-Host "`nERROR: Python not found." -ForegroundColor Red
    Write-Host "Please install Python from https://www.python.org/downloads/"
    Write-Host "Make sure to check 'Add Python to PATH' during installation."
    Pause
    exit
}

# ── STEP 2: Install pip packages ─────────────────────────────
Write-Host "`n[2/7] Installing required Python packages..." -ForegroundColor Yellow
python -m pip install python-dotenv beautifulsoup4 playwright --quiet
Write-Host "  Done."

# ── STEP 3: Install Playwright browser ───────────────────────
Write-Host "`n[3/7] Installing Playwright Chromium browser..." -ForegroundColor Yellow
Write-Host "  This may take a minute..."
playwright install chromium
Write-Host "  Done."

# ── STEP 4: Check .env file ───────────────────────────────────
Write-Host "`n[4/7] Checking configuration..." -ForegroundColor Yellow
if (-not (Test-Path "$ScriptDir\.env")) {
    Write-Host "`nWARNING: .env file not found." -ForegroundColor Red
    Write-Host "Please copy .env.template to .env and fill in your credentials."
    Pause
    exit
}
Write-Host "  .env file found."

# ── STEP 5: Start Chrome ──────────────────────────────────────
Write-Host "`n[5/7] Starting Chrome with remote debugging..." -ForegroundColor Yellow
$chromeArgs = @(
    "--remote-debugging-port=9222",
    "--kiosk-printing",
    "--user-data-dir=C:\Temp\ChromeDebugProfile"
)
Start-Process $ChromePath -ArgumentList $chromeArgs
Write-Host "  Chrome started."

# ── STEP 6: Login prompt ──────────────────────────────────────
Write-Host "`n======================================================" -ForegroundColor Cyan
Write-Host "  ACTION REQUIRED:" -ForegroundColor White
Write-Host "  Chrome has just opened. Please:"
Write-Host "  1. Go to https://www.puzzlemaster.ca/admin/"
Write-Host "  2. Log in to the admin panel"
Write-Host ""
Write-Host "  The automation will start automatically in $LoginWait seconds."
Write-Host "======================================================`n" -ForegroundColor Cyan

# Countdown
for ($i = $LoginWait; $i -gt 0; $i--) {
    Write-Progress -Activity "Waiting for User Login" -Status "$i seconds remaining..." -PercentComplete (($i / $LoginWait) * 100)
    Start-Sleep -Seconds 1
}

# ── STEP 7: Start dashboard + automation ─────────────────────
Write-Host "[6/7] Starting dashboard server..." -ForegroundColor Yellow
Set-Location $ScriptDir
Start-Process python -ArgumentList "-m http.server --cgi $DashboardPort" -WindowStyle Minimized
Write-Host "  Dashboard: http://localhost:$DashboardPort/dashboard.html"

Write-Host "`n[7/7] Starting automation script..." -ForegroundColor Yellow
Write-Host "`n======================================================" -ForegroundColor Green
Write-Host "  Setup complete! Automation is now running." -ForegroundColor Green
Write-Host "  Do not close this window."
Write-Host "======================================================`n" -ForegroundColor Green

# Run the Python script
python "order_print.py"

Write-Host "`n======================================================" -ForegroundColor Red
Write-Host "  Script exited. Check system_log.txt for details."
Write-Host "======================================================"
Pause
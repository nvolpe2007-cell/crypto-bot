# ── MTSM Nick's Crypto Bot Launcher ──────────────────────────────────────────
Write-Host ""
Write-Host "  Starting crypto-bot..." -ForegroundColor Cyan
Write-Host "  Dashboard will open at http://localhost:8080" -ForegroundColor DarkGray
Write-Host ""

Set-Location $PSScriptRoot

# Install dependencies if needed
if (-not (Test-Path ".venv") -and -not (Get-Command python -ErrorAction SilentlyContinue | Out-Null)) {
    Write-Host "  Installing Python dependencies..." -ForegroundColor Yellow
    pip install -r requirements.txt
}

# Open the dashboard in browser after 3 seconds
Start-Job -ScriptBlock {
    Start-Sleep -Seconds 3
    Start-Process "http://localhost:8080"
} | Out-Null

# Run the bots (dashboard auto-starts on port 8080)
py -3.12 run_all_bots.py

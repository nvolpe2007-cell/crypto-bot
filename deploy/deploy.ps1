# deploy.ps1 - Upload and set up crypto bot on VPS
# Usage: .\deploy\deploy.ps1 -IP "1.2.3.4"

param(
    [Parameter(Mandatory=$true)]
    [string]$IP
)

$BOT_DIR = "D:\crypto-bot"
$REMOTE = "root@$IP"
$REMOTE_PATH = "/opt/crypto-bot"

Write-Host "`n=== Deploying Crypto Bot to $IP ===" -ForegroundColor Cyan

# Check sshpass equivalent - use plink if available, otherwise prompt
$sshCmd = "ssh"
$scpCmd = "scp"

# Ensure remote directory exists before upload
Write-Host "`n[0/3] Creating remote directory..." -ForegroundColor Yellow
ssh -o StrictHostKeyChecking=no $REMOTE "mkdir -p $REMOTE_PATH"

# Upload files
Write-Host "`n[1/3] Uploading files..." -ForegroundColor Yellow
scp -o StrictHostKeyChecking=no -r `
    "$BOT_DIR\src" `
    "$BOT_DIR\arbitrage" `
    "$BOT_DIR\deploy" `
    "$BOT_DIR\config.yaml" `
    "$BOT_DIR\requirements.txt" `
    "$BOT_DIR\run_all_bots.py" `
    "$BOT_DIR\.env" `
    "${REMOTE}:${REMOTE_PATH}/"

Write-Host "[2/3] Running setup script..." -ForegroundColor Yellow
ssh -o StrictHostKeyChecking=no $REMOTE "cd $REMOTE_PATH && bash deploy/setup_vps.sh"

Write-Host "[3/3] Verifying bot is running..." -ForegroundColor Yellow
ssh -o StrictHostKeyChecking=no $REMOTE "systemctl status crypto-bot --no-pager"

Write-Host "`n=== Deploy complete! ===" -ForegroundColor Green
Write-Host "SSH in anytime: ssh root@$IP" -ForegroundColor White
Write-Host "View live logs: ssh root@$IP 'journalctl -u crypto-bot -f'" -ForegroundColor White

#!/bin/bash
# Run this on the VPS once: bash setup_vps.sh
set -e

echo "=== Setting up Crypto Bot VPS ==="

# Update system
apt-get update -qq
apt-get install -y python3 python3-pip python3-venv git curl -qq

# Create bot directory
mkdir -p /opt/crypto-bot
cd /opt/crypto-bot

# Create virtual environment
python3 -m venv venv
source venv/bin/activate

# Install dependencies
pip install --upgrade pip -q
pip install -r requirements.txt -q

# Create logs directory
mkdir -p logs

# Set permissions
chmod 600 .env

# Install systemd service
cp deploy/crypto-bot.service /etc/systemd/system/crypto-bot.service
systemctl daemon-reload
systemctl enable crypto-bot
systemctl start crypto-bot

# Install swing-strategy forward-test cron (idempotent: only adds if missing).
# Canonical line lives in deploy/swing_cron.txt so a VPS rebuild restores the
# 1h+4h+daily bands instead of silently reverting to 4h/daily.
SWING_CRON_LINE="$(grep -v '^#' deploy/swing_cron.txt | grep swing_paper.py)"
if ! crontab -l 2>/dev/null | grep -qF 'swing_paper.py'; then
  ( crontab -l 2>/dev/null; echo "$SWING_CRON_LINE" ) | crontab -
  echo "Installed swing cron: $SWING_CRON_LINE"
else
  echo "Swing cron already present; leaving as-is. Canonical line:"
  echo "  $SWING_CRON_LINE"
fi

echo ""
echo "=== Setup complete ==="
echo "Bot status: $(systemctl is-active crypto-bot)"
echo "View logs:  journalctl -u crypto-bot -f"
echo "Stop bot:   systemctl stop crypto-bot"
echo "Start bot:  systemctl start crypto-bot"

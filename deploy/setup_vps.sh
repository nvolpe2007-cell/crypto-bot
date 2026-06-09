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

# Install forward-test crons (idempotent: only add if missing). Canonical lines
# live in deploy/*_cron.txt so a VPS rebuild restores the exact schedules.
#   swing  = 4h-majors directional forward test (note: one-year backtest is
#            negative — see memory swing-one-year-backtest-negative).
#   tsmom  = trend-following allocation (BTC/ETH/SOL, long>SMA200+band) — the
#            low-turnover candidate that survives the cost wall.
install_cron() {  # $1 = cron file, $2 = unique grep token, $3 = label
  local line; line="$(grep -v '^#' "$1" | grep "$2")"
  if ! crontab -l 2>/dev/null | grep -qF "$2"; then
    ( crontab -l 2>/dev/null; echo "$line" ) | crontab -
    echo "Installed $3 cron: $line"
  else
    echo "$3 cron already present; leaving as-is. Canonical line:"
    echo "  $line"
  fi
}
install_cron deploy/swing_cron.txt swing_paper.py swing
install_cron deploy/tsmom_cron.txt tsmom_paper.py tsmom

echo ""
echo "=== Setup complete ==="
echo "Bot status: $(systemctl is-active crypto-bot)"
echo "View logs:  journalctl -u crypto-bot -f"
echo "Stop bot:   systemctl stop crypto-bot"
echo "Start bot:  systemctl start crypto-bot"

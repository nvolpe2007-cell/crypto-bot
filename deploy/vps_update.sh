#!/bin/bash
# Run on the VPS to pull latest code and (re)start the bot.
# Usage:  bash /opt/crypto-bot/deploy/vps_update.sh
#
# Handles:
#  - git pull (or first-time clone)
#  - .env BOM strip (known systemd issue)
#  - python venv + pandas-ta install fallback
#  - systemd service install + restart
set -e

REPO_URL="https://github.com/nvolpe2007-cell/crypto-bot.git"
BOT_DIR="/opt/crypto-bot"
SERVICE_FILE="/etc/systemd/system/crypto-bot.service"

echo "=== Crypto Bot VPS Update ==="
echo "Starting at $(date -u +'%Y-%m-%d %H:%M:%S UTC')"
echo

# ── 1. Ensure system deps ──────────────────────────────────────────────────────
echo "[1/7] Checking system packages..."
apt-get update -qq
apt-get install -y python3 python3-pip python3-venv git curl >/dev/null 2>&1

# ── 2. Clone or pull latest code ───────────────────────────────────────────────
if [ -d "$BOT_DIR/.git" ]; then
    echo "[2/7] Pulling latest code..."
    cd "$BOT_DIR"
    git fetch origin master
    git reset --hard origin/master
else
    echo "[2/7] First-time clone (preserving existing .env if present)..."
    if [ -f "$BOT_DIR/.env" ]; then
        cp "$BOT_DIR/.env" /tmp/crypto-bot.env.bak
    fi
    rm -rf "$BOT_DIR"
    git clone "$REPO_URL" "$BOT_DIR"
    cd "$BOT_DIR"
    if [ -f /tmp/crypto-bot.env.bak ]; then
        cp /tmp/crypto-bot.env.bak "$BOT_DIR/.env"
    fi
fi

# ── 3. Strip UTF-8 BOM from .env (systemd EnvironmentFile chokes on it) ──────
if [ -f "$BOT_DIR/.env" ]; then
    echo "[3/7] Sanitizing .env (strip BOM)..."
    python3 -c "
f='$BOT_DIR/.env'
import sys
with open(f,'rb') as fh: d=fh.read()
if d.startswith(b'\xef\xbb\xbf'):
    d = d[3:]
    with open(f,'wb') as fh: fh.write(d)
    print('  BOM stripped')
else:
    print('  no BOM (clean)')
"
    chmod 600 "$BOT_DIR/.env"
else
    echo "[3/7] WARNING: no .env file present at $BOT_DIR/.env"
    echo "       You must create one with KRAKEN_API_KEY/SECRET + TELEGRAM_* before the bot will run."
fi

# ── 4. Create venv + install deps ──────────────────────────────────────────────
echo "[4/7] Setting up Python venv..."
cd "$BOT_DIR"
if [ ! -d venv ]; then
    python3 -m venv venv
fi
./venv/bin/pip install --upgrade pip --quiet

echo "[5/7] Installing requirements..."
# pandas-ta has known PyPI install issues on some Pythons; try pinned first, fallback to GitHub
./venv/bin/pip install --quiet -r requirements.txt 2>/tmp/pip-err.log || {
    echo "  requirements.txt install partially failed; trying pandas-ta from GitHub..."
    ./venv/bin/pip install --quiet 'pandas-ta @ git+https://github.com/twopirllc/pandas-ta.git' || true
    ./venv/bin/pip install --quiet -r requirements.txt || {
        echo "  ERROR: dependency install failed. See /tmp/pip-err.log"
        cat /tmp/pip-err.log
        exit 1
    }
}

# ── 5. Ensure data/ and logs/ dirs exist ──────────────────────────────────────
mkdir -p "$BOT_DIR/logs" "$BOT_DIR/data"

# ── 6. Install or refresh systemd service ─────────────────────────────────────
echo "[6/7] Installing systemd service..."
cp "$BOT_DIR/deploy/crypto-bot.service" "$SERVICE_FILE"
systemctl daemon-reload
systemctl enable crypto-bot >/dev/null 2>&1

# ── 7. (Re)start the bot ──────────────────────────────────────────────────────
echo "[7/7] Restarting service..."
systemctl restart crypto-bot
sleep 3

echo
echo "=== Status ==="
systemctl status crypto-bot --no-pager -l | head -15
echo
echo "Stream live logs:    journalctl -u crypto-bot -f"
echo "Recent bot log:      tail -f $BOT_DIR/logs/bot.log"
echo "Trade report:        cd $BOT_DIR && ./venv/bin/python trade_report.py"
echo "Stop:                systemctl stop crypto-bot"
echo
echo "Done at $(date -u +'%Y-%m-%d %H:%M:%S UTC')"

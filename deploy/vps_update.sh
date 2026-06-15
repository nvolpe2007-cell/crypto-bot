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
# RESILIENT BY DESIGN: pandas-ta is NOT in requirements.txt (not on PyPI; its git
# source refuses an unauthenticated clone from the VPS). A pip hiccup must NOT abort
# the deploy before the unit-refresh/restart steps — that was the root cause of the
# 2026-06-15 systemd unit drift. So: best-effort install, bootstrap pandas-ta ONLY if
# missing (multi-source, non-fatal), then VERIFY the critical deps actually import and
# fail ONLY on a genuinely broken env.
./venv/bin/pip install --quiet -r requirements.txt 2>/tmp/pip-err.log || \
    echo "  note: 'pip install -r' returned non-zero (tolerated; verifying imports below)"

if ! ./venv/bin/python -c 'import pandas_ta' 2>/dev/null; then
    echo "  pandas_ta missing; bootstrapping (only-if-missing, non-fatal)..."
    ./venv/bin/pip install --quiet pandas-ta 2>/dev/null \
        || ./venv/bin/pip install --quiet 'pandas-ta @ git+https://github.com/twopirllc/pandas-ta.git' 2>/dev/null \
        || echo "  WARNING: could not install pandas_ta from any source"
fi

echo "  verifying critical imports..."
if ! ./venv/bin/python -c 'import ccxt, pandas, numpy, yaml, pandas_ta, sklearn, anthropic' 2>>/tmp/pip-err.log; then
    echo "  ERROR: a critical dependency is not importable. See /tmp/pip-err.log"
    tail -20 /tmp/pip-err.log
    exit 1
fi
echo "  critical deps OK"

# ── 5. Ensure data/ and logs/ dirs exist ──────────────────────────────────────
mkdir -p "$BOT_DIR/logs" "$BOT_DIR/data"

# ── 6. Install or refresh systemd service ─────────────────────────────────────
echo "[6/7] Installing systemd service..."
cp "$BOT_DIR/deploy/crypto-bot.service" "$SERVICE_FILE"
systemctl daemon-reload
systemctl enable crypto-bot >/dev/null 2>&1

# ── 7. (Re)start the bot + ASSERT it actually came up ─────────────────────────
echo "[7/7] Restarting service..."
systemctl restart crypto-bot
sleep 8

# Fail loudly if the service did not reach 'active' — a broken unit/env crash-loops
# (status stays 'activating'/'failed'), and a silent "status printed, exit 0" let the
# 2026-06-15 drift hide. The caller (auto_deploy.ps1) also health-checks, but make the
# VPS script itself the first line of defense.
ACTIVE="$(systemctl is-active crypto-bot || true)"
if [ "$ACTIVE" != "active" ]; then
    echo "  ERROR: service is '$ACTIVE' after restart (expected 'active')."
    journalctl -u crypto-bot --no-pager -n 25
    exit 1
fi
echo "  service is active."

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

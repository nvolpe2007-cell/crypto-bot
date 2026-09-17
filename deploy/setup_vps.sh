#!/bin/bash
# One-time VPS bootstrap.  Run on the VPS:  bash /opt/crypto-bot/deploy/setup_vps.sh
#
# This is the FIRST-BOOT wrapper. The heavy lifting (clone/pull, .env BOM strip,
# venv, dependency verification, systemd unit, restart + active assertion) lives in
# vps_update.sh and is NOT duplicated here — that duplication is what let the two
# scripts drift apart. This script adds only what vps_update.sh does not do:
# the cron schedules and the weekly-report timer.
set -e

BOT_DIR="/opt/crypto-bot"
REPO_URL="https://github.com/nvolpe2007-cell/crypto-bot.git"

echo "=== Crypto Bot VPS bootstrap ==="

# ── 0. Bare box: we may not even have git/python yet, nor the repo. ───────────
apt-get update -qq
apt-get install -y python3 python3-pip python3-venv git curl cron >/dev/null 2>&1
systemctl enable --now cron >/dev/null 2>&1 || true

if [ ! -d "$BOT_DIR/.git" ]; then
    echo "[bootstrap] No repo at $BOT_DIR — cloning."
    mkdir -p "$BOT_DIR"
    if [ -f "$BOT_DIR/.env" ]; then cp "$BOT_DIR/.env" /tmp/crypto-bot.env.bak; fi
    rm -rf "$BOT_DIR"
    git clone "$REPO_URL" "$BOT_DIR"
    if [ -f /tmp/crypto-bot.env.bak ]; then cp /tmp/crypto-bot.env.bak "$BOT_DIR/.env"; fi
fi

cd "$BOT_DIR"

# ── 1. Delegate code/venv/systemd/restart to the single source of truth. ─────
echo "[bootstrap] Running vps_update.sh (code, venv, deps, systemd, start)..."
bash "$BOT_DIR/deploy/vps_update.sh"

# ── 2. Cron schedules. ────────────────────────────────────────────────────────
# Canonical lines live in deploy/*_cron.txt so a VPS rebuild restores the exact
# schedules. Idempotent: a line is added only if its token is absent.
#
# NOTE: the arbitrage arms (flash_arb, stablecoin_arb, dex_arb, dex_flash_arb,
# pattern_flow) were REMOVED after the no-edge verdict (0/34 wins, fees 12x gross).
# Their cron files and scripts no longer exist; this script used to reference them
# and would abort here under `set -e`. Do not re-add them without new evidence.
install_cron() {  # $1 = cron file (repo-relative), $2 = unique grep token, $3 = label
    local file="$BOT_DIR/$1"
    if [ ! -f "$file" ]; then
        echo "  SKIP $3: $1 not present in repo"
        return 0
    fi
    local lines
    lines="$(grep -v '^[[:space:]]*#' "$file" | grep -v '^[[:space:]]*$' || true)"
    if [ -z "$lines" ]; then
        echo "  SKIP $3: $1 has no cron lines"
        return 0
    fi
    if crontab -l 2>/dev/null | grep -qF "$2"; then
        echo "  OK   $3: already present; leaving as-is"
        return 0
    fi
    ( crontab -l 2>/dev/null; echo "$lines" ) | crontab -
    echo "  ADD  $3: $(echo "$lines" | wc -l) line(s)"
}

echo "[bootstrap] Installing cron schedules..."
install_cron deploy/swing_cron.txt                 swing_paper.py           swing
install_cron deploy/tsmom_cron.txt                 tsmom_paper.py           tsmom
install_cron deploy/trend_ensemble_cron.txt        trend_ensemble_paper.py  trend_ensemble
install_cron deploy/lev_perp_arms_cron.txt         lev_perp_paper.py        lev_perp_arms
install_cron deploy/meme_cohort_cron.txt           meme_cohort.py           meme_cohort
install_cron deploy/meme_radar_cron.txt            meme_radar.py            meme_radar
install_cron deploy/trade_close_notifier_cron.txt  trade_close_notifier.py  trade_close_notifier

# ── 3. Weekly report timer. ───────────────────────────────────────────────────
echo "[bootstrap] Installing weekly-report timer..."
cp "$BOT_DIR/deploy/weekly_report.service" /etc/systemd/system/weekly_report.service
cp "$BOT_DIR/deploy/weekly_report.timer"   /etc/systemd/system/weekly_report.timer
systemctl daemon-reload
systemctl enable --now weekly_report.timer >/dev/null 2>&1
echo "  weekly_report.timer: $(systemctl is-enabled weekly_report.timer 2>/dev/null || echo unknown)"

echo
echo "=== Bootstrap complete ==="
echo "Bot status:   $(systemctl is-active crypto-bot)"
echo "Crons:"; crontab -l 2>/dev/null | grep -v '^#' | sed 's/^/  /'
echo
echo "View logs:  journalctl -u crypto-bot -f"

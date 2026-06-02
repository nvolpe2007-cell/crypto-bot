#!/usr/bin/env bash
# Read-only bot status reporter.
#
# Designed to be the FORCED COMMAND for a restricted SSH key (see
# ~/.ssh/authorized_keys: command="/opt/crypto-bot/deploy/bot_status.sh"...).
# Whatever a client asks for, this is all that key can ever run — it prints a
# status snapshot and exits. No arguments are honoured ($SSH_ORIGINAL_COMMAND
# is intentionally ignored), nothing is mutated.
set -euo pipefail

APP_DIR="/opt/crypto-bot"
cd "$APP_DIR" 2>/dev/null || true

echo "=== crypto-bot status @ $(date -u +%Y-%m-%dT%H:%M:%SZ) ==="

# Service state + uptime
echo "--- service ---"
systemctl is-active crypto-bot 2>/dev/null || true
systemctl show crypto-bot -p ActiveEnterTimestamp --value 2>/dev/null || true

# Last heartbeat + funnel (the combined P&L line now reads real arm rollups)
echo "--- last heartbeat / funnel ---"
journalctl -u crypto-bot --no-pager -n 4000 2>/dev/null \
  | grep -E '\[HEARTBEAT\]|\[FUNNEL\]' | tail -2 || echo "(no heartbeat found)"

# Funding-arb arms: net rollup + open/closed counts, straight from state files
echo "--- funding arms (net rollup) ---"
for f in funding_arb_state funding_arb_majors_state funding_arb_kraken_state; do
  python3 - "$f" <<'PY' 2>/dev/null || true
import json, sys
name = sys.argv[1]
try:
    d = json.load(open(f"data/{name}.json"))
except Exception as e:
    print(f"{name:28} (unreadable: {e})"); sys.exit(0)
print(f"{name:28} open={len(d.get('open',{}))} closed={len(d.get('closed',[]))} rollup=${float(d.get('last_rollup_total',0)):+.2f}")
PY
done

# Kraken arm detail — did the aggressive all-in config open/close anything?
echo "--- kraken arm: open positions ---"
python3 - <<'PY' 2>/dev/null || true
import json
try:
    d = json.load(open("data/funding_arb_kraken_state.json"))
except Exception as e:
    print(f"(unreadable: {e})"); raise SystemExit
op = d.get("open", {})
if not op:
    print("(none open)")
for k, p in op.items():
    print(f"  {p.get('symbol'):14} apy={p.get('entry_apy',0):7.1f} size=${p.get('size_usd',0):6.0f} "
          f"funding=${p.get('funding_collected',0):+.3f} cost=${p.get('entry_cost',0):.3f} "
          f"cyc={p.get('cycles_collected',0)} since={p.get('entry_time_iso','')[:16]}")
cl = d.get("closed", [])
print("--- kraken arm: last 5 closed ---")
for p in cl[-5:]:
    net = p.get('funding_collected',0) - p.get('entry_cost',0)
    print(f"  {p.get('close_time_iso','')[:16]} {p.get('symbol'):14} net=${net:+.3f} "
          f"cyc={p.get('cycles_collected',0)} {p.get('close_reason','')}")
PY

echo "=== end ==="

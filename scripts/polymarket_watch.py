#!/usr/bin/env python3
"""
Watch the Polymarket PAPER arm live: every decision (traded or not), open
positions, settled trades and P&L. Read-only — pulls the arm's two data files
from the VPS over SSH (or reads them locally with --local).

    python scripts/polymarket_watch.py            # refresh every 15s
    python scripts/polymarket_watch.py --once     # one snapshot
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import time
from datetime import datetime, timezone

HOST = "crypto-bot-vps"
REMOTE = "/opt/crypto-bot/data"
GREEN, RED, DIM, BOLD, RESET = "\033[32m", "\033[31m", "\033[2m", "\033[1m", "\033[0m"


def pull(local: bool):
    if local:
        state = open("data/polymarket_paper_state.json").read() if os.path.exists(
            "data/polymarket_paper_state.json") else "{}"
        dec = open("data/polymarket_decisions.jsonl").read().splitlines()[-12:] if os.path.exists(
            "data/polymarket_decisions.jsonl") else []
        return json.loads(state or "{}"), [json.loads(x) for x in dec]
    out = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", HOST,
         f"cat {REMOTE}/polymarket_paper_state.json 2>/dev/null || echo '{{}}'; echo '@@@';"
         f" tail -n 12 {REMOTE}/polymarket_decisions.jsonl 2>/dev/null"],
        capture_output=True, text=True, timeout=30).stdout
    state_txt, _, dec_txt = out.partition("@@@")
    return json.loads(state_txt.strip() or "{}"), [json.loads(x) for x in dec_txt.split("\n") if x.strip()]


def hhmm(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%H:%M:%S")


def render(state, decisions):
    lines = [f"{BOLD}POLYMARKET PAPER BOT — 15m BTC Up/Down  (fake money, no real orders){RESET}",
             f"{DIM}now {datetime.now(timezone.utc):%H:%M:%S} UTC{RESET}", ""]
    eq, start = state.get("equity", 1000.0), state.get("start_equity", 1000.0)
    closed, open_ = state.get("closed", []), state.get("open", [])
    wins = sum(1 for c in closed if c.get("pnl", 0) > 0)
    col = GREEN if eq >= start else RED
    lines.append(f"Balance {col}${eq:,.2f}{RESET} (started ${start:,.0f})   "
                 f"trades settled {len(closed)}  won {wins}  lost {len(closed) - wins}")
    lines.append("")
    lines.append(f"{BOLD}Latest decisions{RESET}  (BTC vs target → bot's odds vs market's price)")
    if not decisions:
        lines.append(f"  {DIM}none yet — it decides in minutes 5-13 of each 15-minute window{RESET}")
    for d in decisions:
        diff = d["spot"] - d["start_price"]
        left = 900 - d["elapsed"]
        act = d["action"]
        tag = f"{BOLD}{GREEN}{act}{RESET}" if act != "HOLD" else f"{DIM}HOLD{RESET}"
        lines.append(f"  {hhmm(d['t'])}  BTC {d['spot']:,.0f} ({diff:+,.0f} vs target {d['start_price']:,.0f}), "
                     f"{left // 60}m{left % 60:02d}s left | bot: Up {d['q_up']:.0%} | "
                     f"market: Up {d['ask_up']:.2f} / Down {d['ask_down']:.2f} → {tag}")
    lines.append("")
    lines.append(f"{BOLD}Open fake bets{RESET}")
    if not open_:
        lines.append(f"  {DIM}none{RESET}")
    for p in open_:
        side = "UP" if p["side"] == "YES" else "DOWN"
        lines.append(f"  {side} @ {p['entry_price']:.2f}  ${p['size_usd']:.0f} stake  "
                     f"(fee ${p.get('fee_usd') or 0:.2f})  — {p['question']}")
    lines.append("")
    lines.append(f"{BOLD}Settled fake bets{RESET}")
    if not closed:
        lines.append(f"  {DIM}none yet{RESET}")
    for c in closed[-10:]:
        side = "UP" if c["side"] == "YES" else "DOWN"
        pnl = c.get("pnl", 0)
        lines.append(f"  {(GREEN if pnl > 0 else RED)}{pnl:+7.2f}{RESET}  {side} @ {c['entry_price']:.2f}  — "
                     f"{c['question']}")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--local", action="store_true")
    args = ap.parse_args()
    os.system("")  # enable ANSI colours on Windows consoles
    while True:
        try:
            screen = render(*pull(args.local))
        except Exception as exc:
            screen = f"could not read the bot's data: {exc}"
        if args.once:
            print(screen)
            return
        print("\033[2J\033[H" + screen, flush=True)
        time.sleep(15)


if __name__ == "__main__":
    main()

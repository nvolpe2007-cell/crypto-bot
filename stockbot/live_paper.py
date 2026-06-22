#!/usr/bin/env python3
"""
Alpaca PAPER live runner for the ORB intraday strategy.

Same edge as the backtest (stockbot/strategy.py), now placing PAPER orders via
AlpacaPaper. Idempotent + state-driven like the crypto forward runners: run it on
a schedule during market hours (e.g. every 5 min). It will, per symbol:
  • after the opening-range window and before the cutoff, take AT MOST ONE trade/day
    on a breakout — a market entry with a SERVER-SIDE bracket (stop + target), so a
    missed poll can never strand the position;
  • flatten everything at the close (EOD), and post the day's P&L to Telegram.

FAIL-SAFE: no keys/SDK → AlpacaPaper.available() is False → the runner no-ops.
PAPER ONLY. Verify behaviour in your Alpaca paper dashboard.

  STOCKBOT_SYMBOLS=SPY,QQQ   STOCKBOT_NOTIONAL=2000   python -m stockbot.live_paper
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import List, Optional
from zoneinfo import ZoneInfo

import pandas as pd

from .alpaca import AlpacaPaper
from .strategy import ORBConfig, opening_range, _enter_side

ET = ZoneInfo("America/New_York")
STATE_FILE = Path(os.getenv("STOCKBOT_STATE_FILE", "data/stockbot_live_state.json"))


@dataclass
class OrderIntent:
    symbol: str
    side: str            # 'long' | 'short'
    qty: int
    entry_ref: float     # reference price (market fill will differ)
    stop: float
    target: float


def _minute_of_day(t) -> int:
    return t.hour * 60 + t.minute


def decide(today_bars: pd.DataFrame, cfg: ORBConfig, now_t, notional: float
           ) -> Optional[OrderIntent]:
    """Pure intraday ORB decision for ONE symbol given TODAY's bars and the current
    Eastern wall-clock time. Returns an OrderIntent on a fresh breakout inside the
    trading window, else None. Requires a target (bracket needs a TP)."""
    if today_bars is None or today_bars.empty or cfg.target_r is None:
        return None
    or_high, or_low = opening_range(today_bars, cfg)
    if or_high is None or or_high <= or_low:
        return None
    or_end = _minute_of_day(cfg.session_start) + cfg.or_minutes
    if _minute_of_day(now_t) < or_end:          # still inside the opening range
        return None
    if now_t > cfg.entry_cutoff or now_t >= cfg.session_end:
        return None                              # too late to start a new trade
    last = today_bars.iloc[-1]
    side, entry_ref = _enter_side(last, or_high, or_low, cfg.direction)
    if side is None:
        return None
    stop = or_low if side == "long" else or_high
    risk = abs(entry_ref - stop)
    if risk <= 0:
        return None
    target = entry_ref + cfg.target_r * risk * (1 if side == "long" else -1)
    qty = int(notional // entry_ref)
    if qty < 1:
        return None
    # symbol filled in by the caller (run_step) — decide() only sees bars
    return OrderIntent("", side, qty, float(entry_ref), float(stop), float(target))


def _today_key(now: datetime) -> str:
    return now.astimezone(ET).strftime("%Y-%m-%d")


def run_step(client: AlpacaPaper, symbols: List[str], cfg: ORBConfig, state: dict,
             now: datetime, notional: float) -> dict:
    """One scheduled tick: manage EOD, then for each flat, not-yet-traded symbol look
    for a breakout and submit a paper bracket. Returns a summary dict. Pure of I/O
    beyond the injected client → unit-testable with a fake client."""
    if not client.available():
        return {"status": "unavailable", "actions": []}
    now_et = now.astimezone(ET)
    day = _today_key(now)
    state.setdefault("traded", {}).setdefault(day, [])
    actions = []

    # EOD: flatten once at/after the close.
    if now_et.time() >= cfg.session_end:
        if state.get("eod_done") != day:
            if client.close_all():
                state["eod_done"] = day
                actions.append("eod_flat")
        return {"status": "eod", "actions": actions}

    for sym in symbols:
        if sym in state["traded"][day]:
            continue                              # one trade/symbol/day
        if client.get_position(sym) is not None:
            continue                              # already in a position
        bars = client.recent_bars(sym)
        if bars.empty:
            continue
        today = bars[[d.astimezone(ET).strftime("%Y-%m-%d") == day
                      for d in bars.index]] if bars.index.tz is not None else bars
        intent = decide(today, cfg, now_et.time(), notional)
        if intent is None:
            continue
        oid = client.submit_bracket(sym, intent.qty, intent.side,
                                    take_profit=intent.target, stop_loss=intent.stop)
        if oid:
            state["traded"][day].append(sym)
            actions.append(f"{sym} {intent.side} x{intent.qty} @~{intent.entry_ref:.2f} "
                           f"(tp {intent.target:.2f} / sl {intent.stop:.2f}) #{oid}")
    return {"status": "ok", "actions": actions}


def _load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {"traded": {}, "started_at": datetime.now(ET).isoformat()}


def _save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(exist_ok=True)
    tmp = STATE_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2))
    tmp.replace(STATE_FILE)


def main() -> int:
    symbols = [s.strip().upper() for s in os.getenv("STOCKBOT_SYMBOLS", "SPY,QQQ").split(",")
               if s.strip()]
    notional = float(os.getenv("STOCKBOT_NOTIONAL", "2000"))
    cfg = ORBConfig(or_minutes=int(os.getenv("STOCKBOT_OR_MIN", "15")),
                    direction=os.getenv("STOCKBOT_DIR", "long"),
                    target_r=float(os.getenv("STOCKBOT_TARGET_R", "2.0")),
                    cost_bps_per_side=0.0)        # real fills carry real cost; don't double-count
    client = AlpacaPaper()
    if not client.available():
        print("[stockbot.live] no ALPACA_API_KEY/SECRET (or alpaca-py) — idle, no orders.")
        return 0
    state = _load_state()
    now = datetime.now(ET)
    acct0 = client.account()
    state.setdefault("start_equity", acct0.equity if acct0 else None)
    res = run_step(client, symbols, cfg, state, now, notional)
    _save_state(state)

    acct = client.account()
    if acct:
        start = state.get("start_equity") or acct.equity
        msg = (f"📈 <b>stockbot (Alpaca paper)</b> {now:%Y-%m-%d %H:%M ET}\n"
               f"equity <b>${acct.equity:,.2f}</b> (start ${start:,.2f}, "
               f"{acct.equity-start:+,.2f})\n"
               f"status={res['status']} actions={res['actions'] or 'none'}")
        print(msg.replace("<b>", "").replace("</b>", ""))
        try:
            from .notify import post
            post(msg)
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

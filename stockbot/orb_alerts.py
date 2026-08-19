#!/usr/bin/env python3
"""
ORB long-only setup alerts — the interactive companion to live_paper.py.

Same ORB decision engine (decide(), reused from live_paper.py) and the same
one-alert-per-symbol-per-day discipline, but instead of auto-trading it posts
a chart + two buttons to Telegram and waits for a human decision:

    Trade for me (paper)  -> places the SAME Alpaca PAPER bracket order
                              live_paper.py would have (fail-safe no-op if
                              Alpaca isn't configured -- posts why instead)
    I'll take it myself    -> just acknowledges; you trade it yourself

Two halves, both safe to call every scheduled tick:
  scan_and_alert()    -- look for NEW breakouts, post an alert for each
  handle_responses()  -- poll Telegram for button presses on PENDING alerts

Env (in addition to live_paper.py's STOCKBOT_SYMBOLS/NOTIONAL/OR_MIN/TARGET_R/DIR):
  STOCKBOT_TELEGRAM=1  TELEGRAM_BOT_TOKEN=...  STOCKBOT_TELEGRAM_CHAT_ID=...
      (see notify.py) -- required for both alerting and button handling.
  STOCKBOT_ALERT_STATE_FILE   default data/stockbot_alert_state.json

FAIL-SAFE at every layer: no Telegram creds -> both halves no-op (still safe to
run on a schedule). No Alpaca keys -> "trade for me" posts a reply explaining
that instead of silently doing nothing. No matplotlib -> falls back to a
text-only alert (no chart) rather than dropping the alert entirely.

    python -m stockbot.orb_alerts        # one scheduled tick: scan + handle
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, List, Optional
from zoneinfo import ZoneInfo

import pandas as pd

from . import notify
from .alpaca import AlpacaPaper
from .live_paper import decide
from .strategy import ORBConfig

ET = ZoneInfo("America/New_York")
STATE_FILE = Path(os.getenv("STOCKBOT_ALERT_STATE_FILE", "data/stockbot_alert_state.json"))


@dataclass
class PendingAlert:
    symbol: str
    date: str
    side: str
    qty: int
    entry: float
    stop: float
    target: float
    message_id: Optional[int]
    status: str = "pending"   # 'pending' | 'traded' | 'manual' | 'trade_failed'


# ── data ─────────────────────────────────────────────────────────────────────

def _todays_bars(symbol: str, now_et: datetime) -> pd.DataFrame:
    """Today's intraday bars via yfinance (no Alpaca keys required to get
    alerted -- only 'trade for me' needs Alpaca). Empty df on any failure."""
    from .data import fetch_yfinance
    try:
        bars = fetch_yfinance(symbol, period="5d", interval="5m")
    except Exception:
        return pd.DataFrame()
    if bars.empty:
        return bars
    today = now_et.strftime("%Y-%m-%d")
    return bars[bars.index.strftime("%Y-%m-%d") == today]


# ── state ────────────────────────────────────────────────────────────────────

def _load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {"alerted": {}, "pending": {}, "next_offset": None}


def _save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2))
    tmp.replace(STATE_FILE)


# ── scan + alert ─────────────────────────────────────────────────────────────

def _keyboard(alert_id: str) -> dict:
    return {"inline_keyboard": [[
        {"text": "\U0001F916 Trade for me (paper)", "callback_data": f"t:{alert_id}"},
        {"text": "\U0001F464 I'll take it myself", "callback_data": f"m:{alert_id}"},
    ]]}


def _caption(symbol: str, side: str, qty: int, entry: float, stop: float, target: float) -> str:
    risk = abs(entry - stop)
    r_mult = abs(target - entry) / risk if risk else 0.0
    return (f"\U0001F4C8 <b>{symbol} ORB {side.upper()} setup</b>\n"
            f"entry <b>{entry:.2f}</b>  stop {stop:.2f}  target {target:.2f}  "
            f"({r_mult:.1f}R)\n"
            f"suggested size: {qty} sh (~${qty*entry:,.0f})\n\n"
            f"Trade it for you (Alpaca paper), or take it yourself?")


def scan_and_alert(symbols: List[str], cfg: ORBConfig, notional: float, now: datetime,
                   state: dict, fetch_bars: Callable[[str, datetime], pd.DataFrame] = _todays_bars
                   ) -> List[str]:
    """One tick: for each symbol not yet alerted today, look for a fresh ORB
    breakout and, if found, post an interactive alert. Returns action strings."""
    if not notify.enabled():
        return []
    now_et = now.astimezone(ET)
    day = now_et.strftime("%Y-%m-%d")
    state.setdefault("alerted", {}).setdefault(day, [])
    state.setdefault("pending", {})
    actions: List[str] = []

    for sym in symbols:
        if sym in state["alerted"][day]:
            continue
        bars = fetch_bars(sym, now_et)
        if bars is None or bars.empty:
            continue
        intent = decide(bars, cfg, now_et.time(), notional)
        if intent is None:
            continue

        alert_id = f"{sym}_{day}"
        caption = _caption(sym, intent.side, intent.qty, intent.entry_ref, intent.stop, intent.target)
        try:
            from .chart import render_orb_chart
            from .strategy import opening_range
            or_high, or_low = opening_range(bars, cfg)
            png = render_orb_chart(sym, bars, or_high, or_low, intent.entry_ref,
                                   intent.stop, intent.target, intent.side)
            message_id = notify.post_photo(caption, png, reply_markup=_keyboard(alert_id))
        except ImportError:
            message_id = None
        if message_id is None:
            # no chart (matplotlib missing) or sendPhoto failed -- text fallback
            notify.post(caption)
        state["alerted"][day].append(sym)
        state["pending"][alert_id] = asdict(PendingAlert(
            symbol=sym, date=day, side=intent.side, qty=intent.qty,
            entry=intent.entry_ref, stop=intent.stop, target=intent.target,
            message_id=message_id))
        actions.append(f"alerted {sym} {intent.side} @~{intent.entry_ref:.2f}")
    return actions


# ── handle button presses ───────────────────────────────────────────────────

def _execute_trade(pending: dict, client: AlpacaPaper) -> str:
    if not client.available():
        return ("no Alpaca paper keys configured -- set ALPACA_API_KEY/ALPACA_API_SECRET "
                "to enable 'trade for me'. Take it yourself for now.")
    oid = client.submit_bracket(pending["symbol"], pending["qty"], pending["side"],
                                take_profit=pending["target"], stop_loss=pending["stop"])
    if oid:
        return (f"placed: {pending['symbol']} {pending['side']} x{pending['qty']} "
                f"@~{pending['entry']:.2f} (tp {pending['target']:.2f} / "
                f"sl {pending['stop']:.2f}) #{oid}")
    return "order submission failed -- check the Alpaca paper dashboard / logs."


def handle_responses(state: dict, client: Optional[AlpacaPaper] = None) -> List[str]:
    """Poll Telegram for button presses on pending alerts and act on them.
    Idempotent: an already-resolved alert's buttons are just removed again."""
    if not notify.enabled():
        return []
    client = client or AlpacaPaper()
    state.setdefault("pending", {})
    actions: List[str] = []

    updates = notify.get_updates(offset=state.get("next_offset"))
    for upd in updates:
        state["next_offset"] = upd["update_id"] + 1
        cq = upd.get("callback_query")
        if not cq or "data" not in cq:
            continue
        data = cq["data"]
        if ":" not in data:
            continue
        action, alert_id = data.split(":", 1)
        pending = state["pending"].get(alert_id)
        chat_id = cq.get("message", {}).get("chat", {}).get("id")
        message_id = cq.get("message", {}).get("message_id")
        if pending is None:
            notify.answer_callback_query(cq["id"], "unknown/expired alert")
            continue
        if pending["status"] != "pending":
            notify.answer_callback_query(cq["id"], f"already handled ({pending['status']})")
            continue

        if action == "t":
            note = _execute_trade(pending, client)
            pending["status"] = "traded" if note.startswith("placed:") else "trade_failed"
        elif action == "m":
            note = "noted -- take it yourself."
            pending["status"] = "manual"
        else:
            continue

        notify.answer_callback_query(cq["id"], note[:200])
        if chat_id is not None and message_id is not None:
            notify.edit_message_reply_markup(chat_id, message_id, None)
        notify.post(f"{pending['symbol']} {pending['date']}: {note}")
        actions.append(f"{alert_id}: {note}")

    return actions


# ── entrypoint ───────────────────────────────────────────────────────────────

def main() -> int:
    symbols = [s.strip().upper() for s in os.getenv("STOCKBOT_SYMBOLS", "SPY,QQQ").split(",")
              if s.strip()]
    notional = float(os.getenv("STOCKBOT_NOTIONAL", "2000"))
    cfg = ORBConfig(or_minutes=int(os.getenv("STOCKBOT_OR_MIN", "15")),
                    direction=os.getenv("STOCKBOT_DIR", "long"),
                    target_r=float(os.getenv("STOCKBOT_TARGET_R", "2.0")),
                    cost_bps_per_side=0.0)
    if not notify.enabled():
        print("[stockbot.orb_alerts] STOCKBOT_TELEGRAM not enabled -- idle, no alerts.")
        return 0

    state = _load_state()
    now = datetime.now(ET)
    alerted = scan_and_alert(symbols, cfg, notional, now, state)
    handled = handle_responses(state)
    _save_state(state)
    print(f"[stockbot.orb_alerts] {now:%Y-%m-%d %H:%M ET} "
          f"alerted={alerted or 'none'} handled={handled or 'none'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

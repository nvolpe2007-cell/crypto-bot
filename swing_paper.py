#!/usr/bin/env python3
"""
Swing strategy — FORWARD paper runner (single-shot, cron-friendly).

This is the clock that earns real proof. Run it on a schedule (hourly cron is
fine — it only acts when a new 4h bar has CLOSED). On each new closed bar it
evaluates the strategy on every major, manages open paper positions
(stop / target / trend-break), opens new ones, logs every decision, and records
closed trades to data/swing_paper_state.json — which proof_scorecard.py reads.

FORWARD-ONLY by construction: on the very first run per symbol it just records
the current bar as the baseline and takes NO trade, so the live record is built
only from bars that close AFTER you start. No replaying history into the ledger
(that would just be the in-sample backtest wearing a disguise).

    python swing_paper.py        # process any newly-closed bars, then exit
"""
from __future__ import annotations

import json
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from src.swing_strategy import SwingStrategy, ROUND_TRIP_COST_FRAC
from src.decision_log import DecisionLog

KRAKEN_PAIRS = {"BTC": "XBTUSD", "ETH": "ETHUSD", "SOL": "SOLUSD"}
INTERVAL_MIN = 240          # 4h
SIZE_USD = 100.0
STATE_FILE = Path("data/swing_paper_state.json")


def fetch_closed_bars(pair: str) -> list[dict]:
    """Ascending OHLC with the in-progress final interval DROPPED, so we only
    ever act on fully-closed bars (no repainting / lookahead)."""
    url = f"https://api.kraken.com/0/public/OHLC?pair={pair}&interval={INTERVAL_MIN}"
    with urllib.request.urlopen(url, timeout=30) as r:
        data = json.loads(r.read())
    if data.get("error"):
        raise RuntimeError(f"Kraken error for {pair}: {data['error']}")
    series = next(v for k, v in data["result"].items() if k != "last")
    bars = [{"t": int(row[0]), "o": float(row[1]), "h": float(row[2]),
             "l": float(row[3]), "c": float(row[4])} for row in series]
    return bars[:-1]            # drop the still-forming current bar


def _load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"positions": {}, "closed": [], "last_bar_t": {},
            "started_at": datetime.now(timezone.utc).isoformat()}


def _save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(exist_ok=True)
    tmp = STATE_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2))
    tmp.replace(STATE_FILE)


def _step(base: str, window: list[dict], bar: dict, state: dict,
          strat: SwingStrategy, dlog: DecisionLog, notify=None) -> None:
    """Advance one closed bar: manage an open position or look for an entry."""
    for b in window:
        b["symbol"] = base
    pos = state["positions"].get(base)

    if pos:
        exit_price = exit_reason = None
        if bar["l"] <= pos["stop"]:
            exit_price, exit_reason = pos["stop"], "stop"
        elif bar["h"] >= pos["target"]:
            exit_price, exit_reason = pos["target"], "target"
        else:
            dec = strat.evaluate(window, position_open=True)
            dlog.evaluation(dec)
            if dec.action == "EXIT":
                exit_price, exit_reason = bar["c"], "trend_break"
        if exit_price is not None:
            ret = (exit_price - pos["entry"]) / pos["entry"]
            net = SIZE_USD * ret - SIZE_USD * ROUND_TRIP_COST_FRAC
            rec = {"symbol": base, "entry_ts": pos["entry_ts"],
                   "exit_ts": str(bar["t"]), "entry": pos["entry"],
                   "exit": exit_price, "size_usd": SIZE_USD, "pnl": net,
                   "pnl_pct": ret * 100, "reason": exit_reason, "won": net > 0}
            state["closed"].append(rec)
            del state["positions"][base]
            dlog.closed(base, pos["entry_ts"], str(bar["t"]), pos["entry"],
                        exit_price, SIZE_USD, net, ret * 100, exit_reason, 0)
            if notify:
                notify(f"SWING CLOSE {base}: {exit_reason} net=${net:+.2f} ({ret*100:+.1f}%)")
        return

    dec = strat.evaluate(window, position_open=False)
    dlog.evaluation(dec)
    if dec.is_enter:
        state["positions"][base] = {
            "entry": dec.price, "stop": dec.stop_price, "target": dec.target_price,
            "entry_ts": str(bar["t"]), "size_usd": SIZE_USD, "rr": dec.rr}
        dlog.opened(base, str(bar["t"]), dec.price, SIZE_USD,
                    dec.stop_price, dec.target_price, dec.rr, dec.reason)
        if notify:
            notify(f"SWING OPEN {base} @ {dec.price:.2f} stop {dec.stop_price:.2f} "
                   f"target {dec.target_price:.2f} (R:R {dec.rr:.1f}) — {dec.reason}")


def process_symbol(base: str, closed_bars: list[dict], state: dict,
                   strat: SwingStrategy, dlog: DecisionLog, notify=None) -> int:
    """Process every newly-closed bar since last run. Returns # bars processed.
    First-ever run only sets the baseline (forward-only, no replay)."""
    if not closed_bars:
        return 0
    last_t = state["last_bar_t"].get(base)
    if last_t is None:                       # baseline: start the clock from now
        state["last_bar_t"][base] = closed_bars[-1]["t"]
        return 0
    new = [b for b in closed_bars if b["t"] > last_t]
    for bar in new:
        idx = closed_bars.index(bar)
        _step(base, closed_bars[: idx + 1], bar, state, strat, dlog, notify)
        state["last_bar_t"][base] = bar["t"]
    return len(new)


def main():
    strat = SwingStrategy()
    dlog = DecisionLog(path=Path("data/swing_decisions.jsonl"))
    state = _load_state()
    total_new = 0
    for base, pair in KRAKEN_PAIRS.items():
        try:
            bars = fetch_closed_bars(pair)
        except Exception as e:
            print(f"{base}: fetch failed - {e}")
            continue
        total_new += process_symbol(base, bars, state, strat, dlog)
    _save_state(state)
    open_n = len(state["positions"])
    closed_n = len(state["closed"])
    print(f"[swing_paper] {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC  "
          f"new_bars={total_new} open={open_n} closed={closed_n} "
          f"open_syms={list(state['positions'])}")


if __name__ == "__main__":
    main()

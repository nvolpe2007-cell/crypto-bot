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
import os
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from src.swing_strategy import SwingStrategy, ROUND_TRIP_COST_FRAC
from src.decision_log import DecisionLog

# Validated liquid Kraken USD-spot majors (pair codes confirmed live against the
# OHLC endpoint). A WIDER universe is the fastest, cleanest way to reach the
# proof bar (n>=30, t>2): more uncorrelated symbols = more INDEPENDENT setups
# per unit time, with the SAME locked per-trade edge. This is data expansion,
# NOT a strategy change — swing_strategy.py is untouched. Subset the active set
# with SWING_SYMBOLS="BTC,ETH,..." (bases).
KRAKEN_PAIRS_ALL = {
    "BTC": "XBTUSD", "ETH": "ETHUSD", "SOL": "SOLUSD", "ADA": "ADAUSD",
    "DOT": "DOTUSD", "LINK": "LINKUSD", "AVAX": "AVAXUSD", "LTC": "LTCUSD",
    "XRP": "XRPUSD", "ATOM": "ATOMUSD", "UNI": "UNIUSD", "BCH": "BCHUSD",
    "DOGE": "XDGUSD", "AAVE": "AAVEUSD", "FIL": "FILUSD", "ALGO": "ALGOUSD",
}
_env_syms = os.getenv("SWING_SYMBOLS", "").strip()
if _env_syms:
    _want = {s.strip().upper() for s in _env_syms.split(",") if s.strip()}
    KRAKEN_PAIRS = {b: p for b, p in KRAKEN_PAIRS_ALL.items() if b in _want}
else:
    KRAKEN_PAIRS = dict(KRAKEN_PAIRS_ALL)

# Timeframes the (same, locked) strategy runs on. 4h is the original; daily
# (1440) adds longer-horizon, largely-independent samples. Each (symbol,
# timeframe) keeps its own clock + position under a namespaced state key
# ("BASE@INTERVAL"), so the two never collide. Caveat: same-symbol 4h vs daily
# trades are somewhat correlated — cross-symbol independence is the real driver
# of the t-stat, so the universe width matters more than the extra timeframe.
INTERVALS = [int(x) for x in os.getenv("SWING_INTERVALS", "240,1440").split(",")
             if x.strip()]
STATE_FILE = Path("data/swing_paper_state.json")

# ── Paper account ────────────────────────────────────────────────────────────
# A real $500 paper bankroll. Sizing is a FIXED fraction of the STARTING equity
# (not compounding) so every trade's $ P&L scales identically — that keeps the
# proof_scorecard t-stat clean (uniform scaling leaves it unchanged; this is
# capital allocation, NOT a strategy change, so the locked strategy stays locked).
# Per-trade size is a FIXED fraction of starting equity. With the universe now
# up to 16 symbols × 2 timeframes, 1/3 each would over-deploy, so the default
# drops to ~1/8 (≈8 concurrent positions ≈ full account). This is capital
# allocation only: per-trade expectancy and the proof t-stat are SCALE-INVARIANT
# to uniform sizing, so the locked strategy stays locked. Override with
# SWING_ALLOC_FRAC.
STARTING_EQUITY = float(os.getenv("SWING_START_EQUITY", "500"))
ALLOC_FRAC = float(os.getenv("SWING_ALLOC_FRAC", "0.125"))
TRADE_SIZE = round(STARTING_EQUITY * ALLOC_FRAC, 2)


def fetch_closed_bars(pair: str, interval_min: int) -> list[dict]:
    """Ascending OHLC with the in-progress final interval DROPPED, so we only
    ever act on fully-closed bars (no repainting / lookahead)."""
    url = f"https://api.kraken.com/0/public/OHLC?pair={pair}&interval={interval_min}"
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
            "starting_equity": STARTING_EQUITY, "equity": STARTING_EQUITY,
            "started_at": datetime.now(timezone.utc).isoformat()}


def _save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(exist_ok=True)
    tmp = STATE_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2))
    tmp.replace(STATE_FILE)


def _step(key: str, base: str, tf: int, window: list[dict], bar: dict, state: dict,
          strat: SwingStrategy, dlog: DecisionLog, notify=None) -> None:
    """Advance one closed bar for a (symbol, timeframe) slot. `key` namespaces
    state ("BASE@INTERVAL"); `base`/`tf` are for labels and the trade record."""
    label = f"{base} {tf}m"
    for b in window:
        b["symbol"] = base
    pos = state["positions"].get(key)

    if pos:
        exit_price = exit_reason = None
        if bar["l"] <= pos["stop"]:
            # A stop is a market order: if the bar GAPS open below the stop, you
            # fill at the (worse) open, not the stop price. Modeling the fill at
            # exactly the stop overstated P&L on gap-downs (esp. daily bars).
            exit_price, exit_reason = min(pos["stop"], bar["o"]), "stop"
        elif bar["h"] >= pos["target"]:
            # Target is a limit order → fills at the limit even on a gap up.
            exit_price, exit_reason = pos["target"], "target"
        else:
            dec = strat.evaluate(window, position_open=True)
            dlog.evaluation(dec)
            if dec.action == "EXIT":
                exit_price, exit_reason = bar["c"], "trend_break"
        if exit_price is not None:
            size = pos["size_usd"]
            ret = (exit_price - pos["entry"]) / pos["entry"]
            net = size * ret - size * ROUND_TRIP_COST_FRAC
            state["equity"] = state.get("equity", STARTING_EQUITY) + net
            rec = {"symbol": base, "tf": tf, "entry_ts": pos["entry_ts"],
                   "exit_ts": str(bar["t"]), "entry": pos["entry"],
                   "exit": exit_price, "size_usd": size, "pnl": net,
                   "pnl_pct": ret * 100, "reason": exit_reason, "won": net > 0,
                   "equity_after": round(state["equity"], 2)}
            state["closed"].append(rec)
            del state["positions"][key]
            dlog.closed(base, pos["entry_ts"], str(bar["t"]), pos["entry"],
                        exit_price, size, net, ret * 100, exit_reason, 0)
            if notify:
                notify(f"SWING CLOSE {label}: {exit_reason} net=${net:+.2f} ({ret*100:+.1f}%)")
        return

    dec = strat.evaluate(window, position_open=False)
    dlog.evaluation(dec)
    if dec.is_enter:
        state["positions"][key] = {
            "symbol": base, "tf": tf,
            "entry": dec.price, "stop": dec.stop_price, "target": dec.target_price,
            "entry_ts": str(bar["t"]), "size_usd": TRADE_SIZE, "rr": dec.rr}
        dlog.opened(base, str(bar["t"]), dec.price, TRADE_SIZE,
                    dec.stop_price, dec.target_price, dec.rr, dec.reason)
        if notify:
            notify(f"SWING OPEN {label} @ {dec.price:.2f} stop {dec.stop_price:.2f} "
                   f"target {dec.target_price:.2f} (R:R {dec.rr:.1f}) — {dec.reason}")


def process_symbol(key: str, base: str, tf: int, closed_bars: list[dict],
                   state: dict, strat: SwingStrategy, dlog: DecisionLog,
                   notify=None) -> int:
    """Process every newly-closed bar since last run for one (symbol, timeframe)
    slot. Returns # bars processed. First-ever run per slot only sets the
    baseline (forward-only, no replay)."""
    if not closed_bars:
        return 0
    last_t = state["last_bar_t"].get(key)
    if last_t is None:                       # baseline: start the clock from now
        state["last_bar_t"][key] = closed_bars[-1]["t"]
        return 0
    new = [b for b in closed_bars if b["t"] > last_t]
    for bar in new:
        idx = closed_bars.index(bar)
        _step(key, base, tf, closed_bars[: idx + 1], bar, state, strat, dlog, notify)
        state["last_bar_t"][key] = bar["t"]
    return len(new)


def main():
    strat = SwingStrategy()
    dlog = DecisionLog(path=Path("data/swing_decisions.jsonl"))
    state = _load_state()
    total_new = 0
    for base, pair in KRAKEN_PAIRS.items():
        for tf in INTERVALS:
            key = f"{base}@{tf}"
            try:
                bars = fetch_closed_bars(pair, tf)
            except Exception as e:
                print(f"{key}: fetch failed - {e}")
                continue
            total_new += process_symbol(key, base, tf, bars, state, strat, dlog)
    _save_state(state)
    open_n = len(state["positions"])
    closed_n = len(state["closed"])
    eq = state.get("equity", STARTING_EQUITY)
    start = state.get("starting_equity", STARTING_EQUITY)
    print(f"[swing_paper] {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC  "
          f"equity=${eq:.2f} (start ${start:.0f}, {eq-start:+.2f}) "
          f"trade_size=${TRADE_SIZE:.0f} universe={len(KRAKEN_PAIRS)}x{len(INTERVALS)}tf "
          f"new_bars={total_new} open={open_n} closed={closed_n} "
          f"open_slots={list(state['positions'])}")


if __name__ == "__main__":
    main()

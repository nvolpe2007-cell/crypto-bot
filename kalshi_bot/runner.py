"""
Live PAPER runner for Kalshi KXBTC15M ("BTC price up in next 15 mins?").

Prices the open window with the fair-value model against a FRESH Kraken BTC
price and Kalshi's live best ask at the same instant — once at each of minutes
5, 7, 9, 11, 13, as in the backtest — and paper-buys at that ask
(Kalshi taker fee charged) when the net edge clears MIN_EDGE. Holds to
settlement; settles from Kalshi's own `result`. At most one position per window.

The config is the pre-registered one (registry kalshi-btc-15m-updown) — the same
the backtest judged. Do not tune it on live P&L.

Every decision point, traded or not, is appended to data/kalshi_decisions.jsonl.

No account, no key, no order placement: paper only.

    python -m kalshi_bot.runner            # loop forever
    python -m kalshi_bot.runner --once     # one tick (smoke test)
"""

from __future__ import annotations

import argparse
import json
import logging
import time
from pathlib import Path
from typing import Optional, Tuple

import requests

from kalshi_bot.client import get_market, iso_ts, open_market, quote
from kalshi_bot.fair_value import WINDOW_S, edge_per_contract, prob_up, sigma_per_second, taker_fee
from kalshi_bot.paper_sim import KalshiPaperState, open_position, settle

logger = logging.getLogger("kalshi_runner")

DECISIONS_FILE = Path("data/kalshi_decisions.jsonl")
KRAKEN = "https://api.kraken.com/0/public"

# Pre-registered — identical to scripts/kalshi_backtest.py.
MIN_EDGE = 0.03
PRICE_BAND = (0.10, 0.90)
DECISION_MINUTES = (5, 7, 9, 11, 13)   # same decision points the backtest judged
STAKE_USD = 10.0
VOL_LOOKBACK_MIN = 60
TICK_S = 15

_looked: set = set()   # (ticker, minute) already decided this process


def kraken_btc(open_ts: int) -> Optional[Tuple[float, float, float]]:
    """(proxy for the BRTI minute before the open, live spot, sigma per sqrt-second)."""
    try:
        ohlc = requests.get(f"{KRAKEN}/OHLC", params={"pair": "XBTUSD", "interval": 1}, timeout=10).json()
        rows = next(v for k, v in ohlc["result"].items() if k != "last")
        tk = requests.get(f"{KRAKEN}/Ticker", params={"pair": "XBTUSD"}, timeout=10).json()
        spot = float(next(iter(tk["result"].values()))["c"][0])
    except Exception:
        logger.exception("kraken fetch failed")
        return None
    by_ts = {int(r[0]): r for r in rows}
    pre_open = by_ts.get(open_ts - 60)
    closes = [float(by_ts[t][4]) for t in range(open_ts - VOL_LOOKBACK_MIN * 60, open_ts, 60) if t in by_ts]
    if pre_open is None or len(closes) < VOL_LOOKBACK_MIN:   # backtest needs all 60
        return None
    return (float(pre_open[1]) + float(pre_open[4])) / 2, spot, sigma_per_second(closes)


def log_decision(row: dict) -> None:
    DECISIONS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with DECISIONS_FILE.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row) + "\n")


def decide(strike: float, proxy_pre_open: float, spot: float, sigma: float, elapsed: int,
           q: dict) -> Tuple[dict, Optional[Tuple[str, float, float, float]]]:
    """Pure decision step. Returns (log row, (side, ask, contracts, edge) or None)."""
    spot_adj = spot - (proxy_pre_open - strike)        # remove the Kraken/BRTI basis
    q_up = prob_up(strike, spot_adj, WINDOW_S - elapsed, sigma)
    row = {"elapsed": elapsed, "strike": strike, "spot": spot, "spot_adj": spot_adj, "sigma": sigma,
           "q_up": q_up, "yes_ask": q.get("yes_ask"), "no_ask": q.get("no_ask"), "action": "HOLD"}
    for side, fair, ask, depth in (("YES", q_up, q.get("yes_ask"), q.get("yes_ask_size")),
                                   ("NO", 1 - q_up, q.get("no_ask"), q.get("no_ask_size"))):
        if ask is None or not (PRICE_BAND[0] <= ask <= PRICE_BAND[1]):
            continue
        contracts = round(STAKE_USD / ask, 2)
        edge = edge_per_contract(fair, ask, contracts)
        if edge >= MIN_EDGE and (depth or 0) >= contracts:
            row.update(action="BUY_" + side, edge=edge)
            return row, (side, ask, contracts, edge)
    return row, None


def maybe_enter(state: KalshiPaperState, now: int) -> None:
    m = open_market(now)
    if m is None or not m.get("floor_strike"):
        return
    o = iso_ts(m["open_time"])
    elapsed = now - o
    minute = elapsed // 60
    if minute not in DECISION_MINUTES or (m["ticker"], minute) in _looked:
        return
    if any(p["ticker"] == m["ticker"] for p in state.open):
        return
    btc = kraken_btc(o)
    if btc is None:
        return
    _looked.add((m["ticker"], minute))
    row, trade = decide(float(m["floor_strike"]), *btc, elapsed, quote(m))
    row.update(t=now, ticker=m["ticker"])
    if trade:
        side, ask, contracts, edge = trade
        open_position(state, m["ticker"], side, ask, contracts, taker_fee(contracts, ask),
                      m["close_time"], fair=row["q_up"] if side == "YES" else 1 - row["q_up"], edge=edge)
        state.save()
        logger.info("PAPER BUY %s %s @ %.3f x%.2f edge=%.3f", side, m["ticker"], ask, contracts, edge)
    log_decision(row)


def settle_due(state: KalshiPaperState, now: int) -> None:
    for p in list(state.open):
        if iso_ts(p["close_time"]) > now - 60:
            continue
        try:
            m = get_market(p["ticker"])
        except Exception:
            logger.exception("settle lookup failed for %s", p["ticker"])
            continue
        if m.get("result") in ("yes", "no"):
            for row in settle(state, p["ticker"], m["result"] == "yes"):
                logger.info("SETTLED %s won=%s pnl=%.2f equity=%.2f",
                            row["ticker"], row["won"], row["pnl"], state.equity)
            state.save()


def tick() -> None:
    state = KalshiPaperState.load()
    now = int(time.time())
    settle_due(state, now)
    maybe_enter(state, now)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    while True:
        try:
            tick()
        except Exception:
            logger.exception("tick failed")
        if args.once:
            return
        time.sleep(TICK_S)


if __name__ == "__main__":
    main()

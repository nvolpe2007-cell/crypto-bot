"""
Live PAPER runner for Polymarket 15-minute "Bitcoin Up or Down" markets.

What the backtest could not test, this does: price the market with the
endpoint fair-value model against a FRESH Kraken BTC price and the LIVE order
book at the same instant, and paper-buy at the real best ask (taker fee
charged) when the net edge clears MIN_EDGE. Holds to resolution; settles from
Polymarket's own resolved outcome. At most one position per window.

Prior (scripts/polymarket_twap_backtest.py, 7d / 672 windows): with no
information advantage the model's Brier ties the market's and the edge flips
to about -9%/trade. The expectation is that this arm LOSES; it exists to
measure that on fresh quotes instead of assuming it.

Every decision point — traded or not — goes to data/polymarket_decisions.jsonl
so calibration can be judged from the whole record, not just the fills.

No wallet, no key, no order placement: paper only.

    python -m polymarket_bot.runner            # loop forever
    python -m polymarket_bot.runner --once     # one tick (smoke test)
"""

from __future__ import annotations

import argparse
import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Optional, Tuple

import requests

from polymarket_bot.client import GAMMA_API, BinaryMarket, get_order_book
from polymarket_bot.fair_value import (
    WINDOW_S,
    edge_per_share,
    prob_up_endpoint,
    sigma_per_second,
    taker_fee_per_share,
)
from polymarket_bot.paper_sim import PolymarketPaperState, open_paper_position, settle_position

logger = logging.getLogger("polymarket_runner")

DECISIONS_FILE = Path("data/polymarket_decisions.jsonl")
KRAKEN = "https://api.kraken.com/0/public"

# Pre-registered — the same config the backtest judged. Do not tune on live P&L.
MIN_EDGE = 0.03
PRICE_BAND = (0.10, 0.90)
DECISION_WINDOW_S = (300, 780)   # minutes 5..13 of the 15
STAKE_USD = 10.0
VOL_LOOKBACK_MIN = 60
TICK_S = 15

_market_cache: Dict[int, Optional[BinaryMarket]] = {}


def fetch_window_market(start_ts: int) -> Optional[BinaryMarket]:
    if start_ts in _market_cache:
        return _market_cache[start_ts]
    try:
        ev = requests.get(f"{GAMMA_API}/events",
                          params={"slug": f"btc-updown-15m-{start_ts}"}, timeout=10).json()
        m = ev[0]["markets"][0]
        tokens = json.loads(m["clobTokenIds"])
        outcomes = json.loads(m["outcomes"])
        if outcomes != ["Up", "Down"] or len(tokens) != 2:
            raise ValueError(f"unexpected outcomes {outcomes}")
        market = BinaryMarket(condition_id=m["conditionId"], slug=m["slug"], question=m["question"],
                              yes_token_id=tokens[0], no_token_id=tokens[1],
                              end_date_iso=m.get("endDate"), active=bool(m.get("active")),
                              closed=bool(m.get("closed")))
    except Exception:
        logger.exception("no market for window %s", start_ts)
        market = None
    _market_cache[start_ts] = market
    return market


def kraken_btc(start_ts: int) -> Optional[Tuple[float, float, float]]:
    """(window start price, spot, sigma per sqrt-second) from Kraken 1m bars + ticker."""
    try:
        ohlc = requests.get(f"{KRAKEN}/OHLC", params={"pair": "XBTUSD", "interval": 1}, timeout=10).json()
        rows = next(v for k, v in ohlc["result"].items() if k != "last")
        ticker = requests.get(f"{KRAKEN}/Ticker", params={"pair": "XBTUSD"}, timeout=10).json()
        spot = float(next(iter(ticker["result"].values()))["c"][0])
    except Exception:
        logger.exception("kraken fetch failed")
        return None
    by_ts = {int(r[0]): r for r in rows}
    if start_ts not in by_ts:
        return None
    start_price = float(by_ts[start_ts][1])
    closes = [float(by_ts[t][4]) for t in range(start_ts - VOL_LOOKBACK_MIN * 60, start_ts, 60) if t in by_ts]
    if len(closes) < VOL_LOOKBACK_MIN // 2:
        return None
    return start_price, spot, sigma_per_second(closes)


def best_ask(token_id: str) -> Optional[Tuple[float, float]]:
    book = get_order_book(token_id)
    asks = (book or {}).get("asks") or []
    if not asks:
        return None
    best = min(asks, key=lambda a: float(a["price"]))
    return float(best["price"]), float(best["size"])


def log_decision(row: dict) -> None:
    DECISIONS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with DECISIONS_FILE.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row) + "\n")


def maybe_enter(state: PolymarketPaperState, now: int) -> None:
    start = now - now % WINDOW_S
    elapsed = now - start
    if not (DECISION_WINDOW_S[0] <= elapsed <= DECISION_WINDOW_S[1]):
        return
    market = fetch_window_market(start)
    if market is None or any(p["condition_id"] == market.condition_id for p in state.open):
        return
    btc = kraken_btc(start)
    up, down = best_ask(market.yes_token_id), best_ask(market.no_token_id)
    if btc is None or up is None or down is None:
        return
    start_price, spot, sig = btc
    q_up = prob_up_endpoint(start_price, spot, WINDOW_S - elapsed, sig)

    row = {"t": now, "window": start, "elapsed": elapsed, "start_price": start_price, "spot": spot,
           "sigma": sig, "q_up": q_up, "ask_up": up[0], "ask_down": down[0], "action": "HOLD"}
    for side, fair, (ask, depth) in (("YES", q_up, up), ("NO", 1 - q_up, down)):
        if not (PRICE_BAND[0] <= ask <= PRICE_BAND[1]):
            continue
        edge = edge_per_share(fair, ask)
        shares = STAKE_USD / ask
        if edge >= MIN_EDGE and depth >= shares:
            end_iso = datetime.fromtimestamp(start + WINDOW_S, timezone.utc).isoformat()
            open_paper_position(state, market, side, size_usd=STAKE_USD, entry_price=ask,
                                fee_usd=round(taker_fee_per_share(ask) * shares, 6), window_end=end_iso)
            row.update(action="BUY_" + ("UP" if side == "YES" else "DOWN"), edge=edge)
            logger.info("PAPER BUY %s %s @ %.3f fair=%.3f edge=%.3f", side, market.slug, ask, fair, edge)
            break
    log_decision(row)


def settle_due(state: PolymarketPaperState, now: int) -> None:
    for pos in list(state.open):
        end = pos.get("window_end")
        if not end or datetime.fromisoformat(end).timestamp() > now - 120:
            continue
        try:
            ev = requests.get(f"{GAMMA_API}/markets",
                              # Gamma hides closed markets unless asked.
                              params={"condition_ids": pos["condition_id"], "closed": "true"},
                              timeout=10).json()
            if not ev:
                continue  # not closed yet
            m = ev[0]
            prices = json.loads(m.get("outcomePrices") or "[]")
        except Exception:
            logger.exception("settle lookup failed for %s", pos["condition_id"])
            continue
        if m.get("closed") and prices[:1] in (["1"], ["0"]):
            settle_position(state, pos["condition_id"], resolved_yes=prices[0] == "1")
            logger.info("SETTLED %s up_won=%s equity=%.2f", pos["question"], prices[0] == "1", state.equity)


def tick() -> None:
    state = PolymarketPaperState.load()
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

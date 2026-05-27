"""
Research: cross-sectional funding RV on a WIDE universe (the only structural fix
for the 3-coin failure — more names = more dispersion + stabler ranks + lower
per-coin turnover).

Two modes:
  --dump : (run on the VPS) pick the top-N liquid Bybit USDT perps by 24h turnover,
           fetch ~166d of 4h klines + paginated funding, write data/backtest/universe.json
  (none) : (run local) quantile long-short backtest on that file — each settle, SHORT
           the top-Q funded coins, LONG the bottom-Q, dollar-neutral, equal-weight.
           Rebalance trades only the delta (turnover-controlled). PnL decomposed into
           funding carry vs price vs costs; IS/OOS split.

  python -m src.altperp.research_universe --dump        # on VPS
  python -m src.altperp.research_universe               # locally on the pulled file
"""

import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from typing import Dict, List

from . import config

_DATA = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
                     "data", "backtest")
UNIVERSE_FILE = os.path.join(_DATA, "universe.json")
FEE = config.KRAKEN_TAKER_FEE + config.PAPER_SLIPPAGE_PCT
GROSS_PER_SIDE = 1000.0


# ── dump (VPS) ────────────────────────────────────────────────────────────────
async def _dump(top_n: int, days: int):
    from .data import BybitData
    from .research_fade import fetch_window
    dc = BybitData()
    try:
        res = await dc._get("/v5/market/tickers", {"category": "linear"})
        rows = (res or {}).get("list", [])
        usdt = [(r["symbol"], float(r.get("turnover24h", 0) or 0))
                for r in rows if r["symbol"].endswith("USDT")]
        usdt.sort(key=lambda x: -x[1])
        syms = [s for s, _ in usdt[:top_n]]
        end = int(datetime.now(timezone.utc).timestamp() * 1000)
        start = end - days * 86400 * 1000
        out = {}
        for s in syms:
            bars = await fetch_window(s, start, end)
            if bars:
                out[s] = bars
            print(f"  {s}: {len(bars)} bars", flush=True)
        os.makedirs(_DATA, exist_ok=True)
        with open(UNIVERSE_FILE, "w") as fh:
            json.dump(out, fh)
        print(f"wrote {UNIVERSE_FILE} — {len(out)} coins")
    finally:
        await dc.close()


# ── backtest (local) ──────────────────────────────────────────────────────────
def _align(universe: Dict[str, List[Dict]]):
    """Keep coins present in >=95% of the longest series; align on their common ts."""
    maxbars = max(len(v) for v in universe.values())
    kept = {c: {b["ts"]: b for b in v} for c, v in universe.items()
            if len(v) >= 0.95 * maxbars}
    common = sorted(set.intersection(*[set(d) for d in kept.values()]))
    coins = sorted(kept)
    closes = [{c: kept[c][ts]["close"] for c in coins} for ts in common]
    fund = [{c: kept[c][ts].get("funding", 0.0) for c in coins} for ts in common]
    return coins, common, closes, fund


def run(coins, closes, fund, start, end, q: float) -> Dict:
    """Quantile long-short. q = fraction of universe per side."""
    U = len(coins)
    k = max(1, int(q * U))
    w = {c: 0.0 for c in coins}
    pnl_funding = pnl_price = costs = 0.0
    rebals = 0
    for i in range(start, end - 1):
        if i % 2 == 0:  # settle → rebalance + accrue funding
            ranked = sorted(coins, key=lambda c: fund[i][c])
            longs, shorts = ranked[:k], ranked[-k:]
            target = {c: 0.0 for c in coins}
            for c in longs:
                target[c] = GROSS_PER_SIDE / k
            for c in shorts:
                target[c] = -GROSS_PER_SIDE / k
            turn = sum(abs(target[c] - w[c]) for c in coins)
            if turn > 0:
                costs += turn * FEE
                rebals += 1
            w = target
            pnl_funding += sum(-fund[i][c] * w[c] for c in coins)  # short rich/long cheap → +
        # price PnL over bar i→i+1 for current weights
        for c in coins:
            if w[c]:
                pnl_price += w[c] * (closes[i + 1][c] / closes[i][c] - 1)
    costs += sum(abs(w[c]) for c in coins) * FEE  # close at end
    net = pnl_funding + pnl_price - costs
    return {"net": net, "funding": pnl_funding, "price": pnl_price, "costs": costs,
            "rebals": rebals, "U": len(coins), "k": k}


def _fmt(r):
    return (f"net=${r['net']:+9.2f}  [funding ${r['funding']:+8.2f} + "
            f"price ${r['price']:+9.2f} - costs ${r['costs']:7.2f}]  "
            f"rebals={r['rebals']:3d} (U={r['U']}, k={r['k']}/side)")


def _backtest():
    with open(UNIVERSE_FILE) as fh:
        universe = json.load(fh)
    coins, common, closes, fund = _align(universe)
    n = len(common)
    half = n // 2
    print(f"Wide cross-sectional funding RV — {len(coins)} coins, {n} aligned 4h bars "
          f"(~{n//6}d), ${GROSS_PER_SIDE:.0f}/side\n")

    print("FULL window, by quantile per side:")
    for q in (0.10, 0.20, 0.33):
        print(f"  q={q:.2f}: {_fmt(run(coins, closes, fund, 0, n, q))}")

    print("\nQuantile picked on IN-SAMPLE, judged OUT-OF-SAMPLE:")
    isr = [(q, run(coins, closes, fund, 0, half, q)) for q in (0.10, 0.20, 0.33)]
    isr.sort(key=lambda x: x[1]["net"], reverse=True)
    bestq = isr[0][0]
    for q, r in isr:
        print(f"  in-sample q={q:.2f}: {_fmt(r)}")
    print(f"  OOS verdict @ best-IS q={bestq:.2f}: {_fmt(run(coins, closes, fund, half, n, bestq))}")


def main():
    if "--dump" in sys.argv:
        top_n = 40
        days = 166
        asyncio.run(_dump(top_n, days))
    else:
        if not os.path.exists(UNIVERSE_FILE):
            print(f"missing {UNIVERSE_FILE} — run --dump on the VPS and scp it down first")
            return
        _backtest()


if __name__ == "__main__":
    main()

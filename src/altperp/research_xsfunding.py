"""
Research: cross-sectional funding relative-value on SOL/AVAX/ARB.

Each step: SHORT the highest-funded alt, LONG the lowest-funded, dollar-neutral
($N per leg). This harvests two things:
  - funding DISPERSION  = (f_short - f_long)·N per 8h settle -- ALWAYS >= 0 (you're
    short the richest and long the cheapest), the mechanical carry; and
  - relative price move  = (ret_long - ret_short)·N -- the bet that the crowded-long
    (high-funding) coin underperforms its peer. Market-wide moves cancel (neutral).

We rebalance only when the (richest, cheapest) pair flips, and sit flat when the
dispersion is below a floor (avoids churning fees in calm tape). PnL is decomposed
into funding vs price vs costs so we can see if the edge is real carry or a
disguised directional bet. Faithful + pure: cached funding+klines, no network.

  python -m src.altperp.research_xsfunding
"""

import json
import os
from typing import Dict, List

from . import config

COINS = ["SOLUSDT", "AVAXUSDT", "ARBUSDT"]
_DATA = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
                     "data", "backtest")
N = 1000.0                                         # notional per leg
FEE = config.KRAKEN_TAKER_FEE + config.PAPER_SLIPPAGE_PCT   # per fill (taker + slippage)


def load_aligned() -> List[Dict]:
    """Rows aligned on the timestamps present in all three coins."""
    series = {}
    for c in COINS:
        with open(os.path.join(_DATA, f"{c}.json")) as fh:
            series[c] = {b["ts"]: b for b in json.load(fh)}
    common = sorted(set.intersection(*[set(s) for s in series.values()]))
    rows = []
    for ts in common:
        rows.append({"ts": ts,
                     "close": {c: series[c][ts]["close"] for c in COINS},
                     "funding": {c: series[c][ts].get("funding", 0.0) for c in COINS}})
    return rows


def run(rows: List[Dict], start: int, end: int, min_disp: float) -> Dict:
    """Decomposed PnL over rows[start:end]. min_disp = funding spread (decimal/8h)
    required to hold a pair; below it we sit flat."""
    pos = None
    pnl_funding = pnl_price = costs = 0.0
    rebalances = settles = 0

    def close(pos, px):
        nonlocal pnl_price, costs
        pnl_price += (pos["px_s"] - px[pos["s"]]) / pos["px_s"] * N   # short: gains if it fell
        pnl_price += (px[pos["l"]] - pos["px_l"]) / pos["px_l"] * N   # long: gains if it rose
        costs += 2 * N * FEE                                          # 2 legs closed

    for i in range(start, end):
        fund, px = rows[i]["funding"], rows[i]["close"]
        s = max(fund, key=fund.get)          # richest funding -> short
        l = min(fund, key=fund.get)          # cheapest funding -> long
        disp = fund[s] - fund[l]
        target = (s, l) if disp >= min_disp else None
        cur = (pos["s"], pos["l"]) if pos else None
        if cur != target:
            if pos:
                close(pos, px)
                pos = None
            if target:
                costs += 2 * N * FEE         # 2 legs opened
                pos = {"s": s, "l": l, "px_s": px[s], "px_l": px[l]}
                rebalances += 1
        if pos and i % 2 == 0:               # funding settles every 8h (2× 4h bars)
            pnl_funding += (fund[pos["s"]] - fund[pos["l"]]) * N
            settles += 1

    if pos:
        close(pos, rows[end - 1]["close"])

    net = pnl_funding + pnl_price - costs
    return {"net": net, "funding": pnl_funding, "price": pnl_price, "costs": costs,
            "rebalances": rebalances, "settles": settles}


def _fmt(r: Dict) -> str:
    return (f"net=${r['net']:+8.2f}  [funding ${r['funding']:+7.2f} + "
            f"price ${r['price']:+8.2f} - costs ${r['costs']:6.2f}]  "
            f"rebal={r['rebalances']:3d}")


def main():
    rows = load_aligned()
    n = len(rows)
    half = n // 2
    print(f"Cross-sectional funding RV -- {n} aligned 4h bars (~{n//6}d), $%d/leg\n" % N)

    print("FULL window, by dispersion floor (min spread to hold a pair):")
    for md in (0.0, 0.0001, 0.0002, 0.0005):
        r = run(rows, 0, n, md)
        print(f"  min_disp={md*100:.3f}%/8h : {_fmt(r)}")

    print("\nDispersion floor picked on IN-SAMPLE, judged OUT-OF-SAMPLE:")
    is_results = [(md, run(rows, 0, half, md)) for md in (0.0, 0.0001, 0.0002, 0.0005)]
    is_results.sort(key=lambda x: x[1]["net"], reverse=True)
    best_md = is_results[0][0]
    print("  in-sample (pick):")
    for md, r in is_results:
        print(f"    min_disp={md*100:.3f}% : {_fmt(r)}")
    oos = run(rows, half, n, best_md)
    base_oos = run(rows, half, n, 0.0)
    print(f"  OOS verdict @ best-IS min_disp={best_md*100:.3f}% : {_fmt(oos)}")
    print(f"  OOS baseline (always-on, min_disp=0)            : {_fmt(base_oos)}")

    print("\nREAD: 'funding' is the mechanical carry (always >=0); 'price' is the")
    print("      directional reversion bet. If funding < costs, the carry alone")
    print("      doesn't pay -- net then rides entirely on the price term (a bet).")


if __name__ == "__main__":
    main()

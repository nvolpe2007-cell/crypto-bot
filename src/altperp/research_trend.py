"""
Research: is the trend-follower's whipsaw fixable OUT-OF-SAMPLE? (walk-forward)

Loads cached real klines (data/backtest/<coin>.json), splits each coin's history
into in-sample (first half) and out-of-sample (second half). Scans a SMALL,
principled grid of trend params on IN-SAMPLE pooled across coins, picks the single
best, then reports its OUT-OF-SAMPLE result next to the current baseline. The OOS
number is the verdict -- it is never used to choose params, so it can't be overfit.

Hypothesis under test: the whipsaw comes from entering weak/early breakouts and
trailing too tight. So we vary breakout length N (longer = only established
trends), chandelier width M (wider = breathe through noise), and a min-vol floor.

Run:  python -m src.altperp.research_trend
"""

import json
import os
from itertools import product
from typing import Dict, List

from . import config, regime as rg
from .math_utils import atr

_DATA = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
                     "data", "backtest")
COINS = ["SOLUSDT", "AVAXUSDT", "ARBUSDT"]
WARM = 58
BASE_EQUITY = 1000.0

# Small principled grid. Baseline (current live config) is (20, 3.0, 0.010).
GRID_N = [20, 30, 40]
GRID_M = [3.0, 4.0, 5.0]
GRID_MINATR = [0.010, 0.020]
BASELINE = (20, 3.0, 0.010)


def _load(coin: str) -> List[Dict]:
    with open(os.path.join(_DATA, f"{coin}.json")) as fh:
        return json.load(fh)


def _fill(price: float, side: str) -> float:
    s = config.PAPER_SLIPPAGE_PCT
    return price * (1 + s) if side == "buy" else price * (1 - s)


def _precompute(klines: List[Dict]):
    """Regime label + ATR per bar -- independent of the trend params, so compute once."""
    regimes, atrs = [None] * len(klines), [None] * len(klines)
    for i in range(len(klines)):
        if i < WARM:
            continue
        w = klines[: i + 1]
        regimes[i] = rg.classify(w).regime
        atrs[i] = atr(w, config.REGIME_ATR_PERIOD)
    return regimes, atrs


def sim(klines, regimes, atrs, N, M, min_atr, start, end) -> Dict:
    """Parameterized trend sim over bars [start,end). Fixed $1000 risk base (non-
    compounding) so combos are compared on per-trade edge, not compounding luck."""
    trades = wins = 0
    net = gp = gl = 0.0
    pos = None
    for i in range(start, end):
        bar = klines[i]
        if pos is not None:
            # chandelier trail M×ATR(at entry) from the best extreme since entry
            if pos["dir"] == "long":
                pos["anchor"] = max(pos["anchor"], bar["high"])
                stop = pos["anchor"] - M * pos["atr"]
                hit = bar["low"] <= stop
            else:
                pos["anchor"] = min(pos["anchor"], bar["low"])
                stop = pos["anchor"] + M * pos["atr"]
                hit = bar["high"] >= stop
            if hit:
                fp = _fill(stop, "buy" if pos["dir"] == "short" else "sell")
                pnl = (fp - pos["entry"]) * pos["qty"] if pos["dir"] == "long" \
                    else (pos["entry"] - fp) * pos["qty"]
                exit_fee = pos["qty"] * fp * config.KRAKEN_TAKER_FEE
                tot = pnl - exit_fee - pos["entry_fee"]
                net += tot
                trades += 1
                if tot > 0:
                    wins += 1; gp += tot
                else:
                    gl += tot
                pos = None
            continue
        if i < WARM or regimes[i] is None or atrs[i] is None:
            continue
        price = bar["close"]
        a = atrs[i]
        if price <= 0 or a / price < min_atr:
            continue
        reg = regimes[i]
        prior = klines[i - N:i]
        if not prior:
            continue
        direction = None
        if reg == rg.TRENDING_UP and price >= max(b["high"] for b in prior):
            direction = "long"
        elif reg == rg.TRENDING_DOWN and price <= min(b["low"] for b in prior):
            direction = "short"
        if not direction:
            continue
        entry = _fill(price, "sell" if direction == "short" else "buy")
        stop_frac = M * a / price
        notional = min(BASE_EQUITY * config.BASE_RISK_PCT / stop_frac,
                       BASE_EQUITY * config.MAX_LEVERAGE)
        entry_fee = notional * config.KRAKEN_TAKER_FEE
        pos = {"dir": direction, "entry": entry, "qty": notional / entry, "atr": a,
               "anchor": bar["high"] if direction == "long" else bar["low"],
               "entry_fee": entry_fee}
    pf = gp / abs(gl) if gl else float("inf")
    return {"trades": trades, "wins": wins, "net": net, "pf": pf,
            "win_rate": (wins / trades * 100 if trades else 0.0)}


def _pooled(data, regimes, atrs, N, M, min_atr, seg):
    """Sum a metric across coins over a segment ('is' or 'oos')."""
    tot = {"trades": 0, "wins": 0, "net": 0.0}
    per = {}
    for c in COINS:
        kl = data[c]
        half = len(kl) // 2
        start, end = (WARM, half) if seg == "is" else (half, len(kl))
        r = sim(kl, regimes[c], atrs[c], N, M, min_atr, start, end)
        per[c] = r
        tot["trades"] += r["trades"]; tot["wins"] += r["wins"]; tot["net"] += r["net"]
    tot["win_rate"] = tot["wins"] / tot["trades"] * 100 if tot["trades"] else 0.0
    return tot, per


def main():
    data = {c: _load(c) for c in COINS}
    regimes, atrs = {}, {}
    for c in COINS:
        regimes[c], atrs[c] = _precompute(data[c])
    print(f"Loaded {COINS} -- {[len(data[c]) for c in COINS]} bars; split 50/50 IS/OOS\n")

    # 1) scan grid on IN-SAMPLE only
    results = []
    for N, M, mn in product(GRID_N, GRID_M, GRID_MINATR):
        tot, _ = _pooled(data, regimes, atrs, N, M, mn, "is")
        results.append(((N, M, mn), tot))
    results.sort(key=lambda x: x[1]["net"], reverse=True)

    base_is, _ = _pooled(data, regimes, atrs, *BASELINE, "is")
    best_params, best_is = results[0]

    print("IN-SAMPLE (used to PICK params -- not the verdict):")
    print(f"  baseline {BASELINE}: net=${base_is['net']:+.2f} "
          f"win={base_is['win_rate']:.1f}% trades={base_is['trades']}")
    print(f"  best     {best_params}: net=${best_is['net']:+.2f} "
          f"win={best_is['win_rate']:.1f}% trades={best_is['trades']}")
    print("  top 5 in-sample:")
    for p, t in results[:5]:
        print(f"    {p}: net=${t['net']:+.2f} win={t['win_rate']:.1f}% n={t['trades']}")

    # 2) verdict on OUT-OF-SAMPLE (held out; chosen config evaluated ONCE)
    base_oos, base_per = _pooled(data, regimes, atrs, *BASELINE, "oos")
    best_oos, best_per = _pooled(data, regimes, atrs, *best_params, "oos")
    print("\nOUT-OF-SAMPLE (the verdict -- params chosen on IS, evaluated here once):")
    print(f"  baseline {BASELINE}: net=${base_oos['net']:+.2f} "
          f"win={base_oos['win_rate']:.1f}% trades={base_oos['trades']}")
    print(f"  best-IS  {best_params}: net=${best_oos['net']:+.2f} "
          f"win={best_oos['win_rate']:.1f}% trades={best_oos['trades']}")
    print("  best-IS per coin OOS:")
    for c in COINS:
        r = best_per[c]
        print(f"    {c}: net=${r['net']:+.2f} win={r['win_rate']:.1f}% n={r['trades']} PF={r['pf']:.2f}")

    improved = best_oos["net"] > base_oos["net"]
    positive = best_oos["net"] > 0
    print("\nVERDICT:", end=" ")
    if positive and improved:
        print("the IS-best config is BOTH positive AND beats baseline out-of-sample -- "
              "a tentative, non-overfit improvement worth a wider test.")
    elif improved and not positive:
        print("beats baseline OOS but still LOSES money -- less-bad is not an edge.")
    else:
        print("the IS-best config does NOT survive out-of-sample -- the 'fix' was "
              "overfitting. No trend edge demonstrated on this data.")


if __name__ == "__main__":
    main()

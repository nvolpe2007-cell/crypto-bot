#!/usr/bin/env python3
"""
Cost-gate sweep: does a stricter min_target_cost_mult raise expectancy?

IN-SAMPLE sweep (NOT proof — same caveat as swing_backtest.py). We do NOT touch
the locked edge (stop/target ATR mults, RSI, ROC). We only vary the ENTRY gate
`min_target_cost_mult` (target must clear N x round-trip cost) and measure how
expectancy / win% / t-stat / drawdown respond. Run on the live universe (16
majors x {1h,4h,daily}) so the gate actually bites on small-target 1h setups.

    python swing_gate_sweep.py
"""
from __future__ import annotations
import json, time, urllib.request
from pathlib import Path

from src.swing_strategy import SwingStrategy
from src.decision_log import DecisionLog
from swing_backtest import backtest_symbol, stats

PAIRS = {
    "BTC": "XBTUSD", "ETH": "ETHUSD", "SOL": "SOLUSD", "ADA": "ADAUSD",
    "DOT": "DOTUSD", "LINK": "LINKUSD", "AVAX": "AVAXUSD", "LTC": "LTCUSD",
    "XRP": "XRPUSD", "ATOM": "ATOMUSD", "UNI": "UNIUSD", "BCH": "BCHUSD",
    "DOGE": "XDGUSD", "AAVE": "AAVEUSD", "FIL": "FILUSD", "ALGO": "ALGOUSD",
}
INTERVALS = [60, 240, 1440]           # 1h, 4h, daily — matches live SWING_INTERVALS
GATES = [3.0, 5.0, 8.0]               # min_target_cost_mult values to compare


def fetch(pair: str, interval: int):
    url = f"https://api.kraken.com/0/public/OHLC?pair={pair}&interval={interval}"
    with urllib.request.urlopen(url, timeout=30) as r:
        data = json.loads(r.read())
    if data.get("error"):
        raise RuntimeError(data["error"])
    series = next(v for k, v in data["result"].items() if k != "last")
    return [{"t": int(x[0]), "o": float(x[1]), "h": float(x[2]),
             "l": float(x[3]), "c": float(x[4])} for x in series]


def main():
    # Fetch once; reuse across every gate setting (same bars, fair comparison).
    cache: dict[tuple[str, int], list[dict]] = {}
    print("Fetching live universe (16 majors x 1h/4h/daily)...")
    for base, pair in PAIRS.items():
        for iv in INTERVALS:
            try:
                cache[(base, iv)] = fetch(pair, iv)
                time.sleep(0.4)       # be polite to Kraken public API
            except Exception as e:
                print(f"  skip {base}@{iv}: {e}")
    print(f"  got {len(cache)} (symbol,timeframe) series\n")

    dlog = DecisionLog(path=Path("data/_gate_sweep_decisions.jsonl"))
    print(f"{'gate':>5} | {'trades':>6} | {'net$':>9} | {'win%':>5} | "
          f"{'exp$/trade':>10} | {'t-stat':>6} | {'maxDD$':>8}")
    print("-" * 70)
    results = []
    for g in GATES:
        all_nets = []
        for (base, iv), bars in cache.items():
            strat = SwingStrategy(min_target_cost_mult=g)
            all_nets += backtest_symbol(f"{base}@{iv}", bars, strat, dlog)
        s = stats(all_nets)
        results.append((g, s))
        print(f"{g:>4.0f}x | {s['n']:>6} | {s['total']:>+9.2f} | "
              f"{s['win']*100:>4.0f}% | {s['exp']:>+10.4f} | {s['t']:>+6.2f} | "
              f"{s['dd']:>+8.2f}")

    best = max(results, key=lambda r: r[1]['exp'])
    print("-" * 70)
    print(f"Highest expectancy/trade: gate {best[0]:.0f}x "
          f"(${best[1]['exp']:+.4f}/trade, t={best[1]['t']:+.2f}, n={best[1]['n']})")
    print("NOTE: in-sample sanity check. Prefer the gate that maximizes EXPECTANCY")
    print("with t>1 and acceptable drawdown — NOT the one with the highest win%.")


if __name__ == "__main__":
    main()

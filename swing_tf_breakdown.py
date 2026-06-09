#!/usr/bin/env python3
"""Where does swing make/lose money? Break the in-sample sweep down by timeframe
and by majors-vs-alts, at the locked default gate. Decides whether the new 1h
band is helping or bleeding."""
from __future__ import annotations
import json, time, urllib.request
from pathlib import Path
from src.swing_strategy import SwingStrategy
from src.decision_log import DecisionLog
from swing_backtest import backtest_symbol, stats

PAIRS = {"BTC": "XBTUSD", "ETH": "ETHUSD", "SOL": "SOLUSD", "ADA": "ADAUSD",
    "DOT": "DOTUSD", "LINK": "LINKUSD", "AVAX": "AVAXUSD", "LTC": "LTCUSD",
    "XRP": "XRPUSD", "ATOM": "ATOMUSD", "UNI": "UNIUSD", "BCH": "BCHUSD",
    "DOGE": "XDGUSD", "AAVE": "AAVEUSD", "FIL": "FILUSD", "ALGO": "ALGOUSD"}
MAJORS = {"BTC", "ETH", "SOL", "LTC", "BCH", "XRP"}
INTERVALS = [60, 240, 1440]
LABEL = {60: "1h ", 240: "4h ", 1440: "1d "}

def fetch(pair, interval, tries=3):
    url = f"https://api.kraken.com/0/public/OHLC?pair={pair}&interval={interval}"
    for k in range(tries):
        try:
            with urllib.request.urlopen(url, timeout=60) as r:
                data = json.loads(r.read())
            if data.get("error"): raise RuntimeError(data["error"])
            series = next(v for kk, v in data["result"].items() if kk != "last")
            return [{"t": int(x[0]), "o": float(x[1]), "h": float(x[2]),
                     "l": float(x[3]), "c": float(x[4])} for x in series]
        except Exception:
            if k == tries - 1: raise
            time.sleep(1.5)

def run(series, dlog):
    nets = []
    for (base, iv), bars in series.items():
        nets += backtest_symbol(f"{base}@{iv}", bars, SwingStrategy(), dlog)
    return stats(nets)

def main():
    cache = {}
    print("Fetching (with retries)...")
    for base, pair in PAIRS.items():
        for iv in INTERVALS:
            try:
                cache[(base, iv)] = fetch(pair, iv); time.sleep(0.6)
            except Exception as e:
                print(f"  skip {base}@{iv}: {e}")
    print(f"  {len(cache)} series\n")
    dlog = DecisionLog(path=Path("data/_tf_breakdown.jsonl"))

    def line(name, s):
        print(f"{name:>14} | n={s['n']:>3} | net=${s['total']:>+8.2f} | "
              f"win={s['win']*100:>3.0f}% | exp=${s['exp']:>+7.4f} | t={s['t']:>+5.2f}")

    print("BY TIMEFRAME (all 16 symbols):")
    for iv in INTERVALS:
        sub = {k: v for k, v in cache.items() if k[1] == iv}
        line(LABEL[iv] + "all", run(sub, dlog))
    print("\nBY TIMEFRAME (majors only):")
    for iv in INTERVALS:
        sub = {k: v for k, v in cache.items() if k[1] == iv and k[0] in MAJORS}
        line(LABEL[iv] + "majors", run(sub, dlog))
    print("\nKEY CONTRASTS:")
    line("4h majors", run({k: v for k, v in cache.items()
                           if k[1] == 240 and k[0] in MAJORS}, dlog))
    line("1h all", run({k: v for k, v in cache.items() if k[1] == 60}, dlog))

if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
Swing strategy backtest on REAL Kraken 4h history.

IMPORTANT — what this is and is NOT:
  This is an IN-SAMPLE SANITY CHECK, not proof. It runs the FIXED strategy (no
  parameter search — searching until it's green is the overfit that kills real
  accounts) over the last ~120 days of real 4h bars and reports honest stats.
  A pass here means "worth forward-testing", NOT "fund it". PROOF is forward
  paper trading judged by proof_scorecard.py (n>=30, expectancy>0, t>2).

Usage:  python swing_backtest.py
"""
from __future__ import annotations

import json
import math
import statistics as st
import urllib.request
from pathlib import Path

from src.swing_strategy import SwingStrategy, ROUND_TRIP_COST_FRAC
from src.decision_log import DecisionLog

KRAKEN_PAIRS = {"BTC": "XBTUSD", "ETH": "ETHUSD", "SOL": "SOLUSD"}
INTERVAL_MIN = 240          # 4h
SIZE_USD = 100.0


def fetch_ohlc(pair: str) -> list[dict]:
    url = f"https://api.kraken.com/0/public/OHLC?pair={pair}&interval={INTERVAL_MIN}"
    with urllib.request.urlopen(url, timeout=30) as r:
        data = json.loads(r.read())
    if data.get("error"):
        raise RuntimeError(f"Kraken error for {pair}: {data['error']}")
    series = next(v for k, v in data["result"].items() if k != "last")
    return [{"t": int(row[0]), "o": float(row[1]), "h": float(row[2]),
             "l": float(row[3]), "c": float(row[4])} for row in series]


def stats(nets: list[float]) -> dict:
    n = len(nets)
    if n == 0:
        return dict(n=0, total=0.0, win=0.0, exp=0.0, t=0.0, sharpe=0.0, dd=0.0)
    wins = sum(1 for x in nets if x > 0)
    mean = sum(nets) / n
    sd = st.pstdev(nets) if n < 2 else st.stdev(nets)
    t = (mean / (sd / math.sqrt(n))) if (n >= 2 and sd > 0) else 0.0
    cum = peak = dd = 0.0
    for x in nets:
        cum += x; peak = max(peak, cum); dd = min(dd, cum - peak)
    return dict(n=n, total=sum(nets), win=wins / n, exp=mean, t=t,
                sharpe=(mean / sd if sd > 0 else 0.0), dd=dd)


def backtest_symbol(base: str, bars: list[dict], strat: SwingStrategy,
                    dlog: DecisionLog) -> list[float]:
    for b in bars:
        b["symbol"] = base
    nets: list[float] = []
    pos = None  # dict(entry, stop, target, i)
    for i in range(strat.min_bars, len(bars)):
        window = bars[: i + 1]
        bar = bars[i]
        if pos is not None:
            # intrabar exits: stop first (conservative), then target, then trend break
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
                nets.append(net)
                dlog.closed(base, str(bars[pos["i"]]["t"]), str(bar["t"]),
                            pos["entry"], exit_price, SIZE_USD, net, ret * 100,
                            exit_reason, i - pos["i"])
                pos = None
            continue
        dec = strat.evaluate(window, position_open=False)
        dlog.evaluation(dec)
        if dec.is_enter:
            pos = {"entry": dec.price, "stop": dec.stop_price,
                   "target": dec.target_price, "i": i}
            dlog.opened(base, str(bar["t"]), dec.price, SIZE_USD,
                        dec.stop_price, dec.target_price, dec.rr, dec.reason)
    return nets


def main():
    strat = SwingStrategy()
    dlog = DecisionLog(path=Path("data/swing_backtest_decisions.jsonl"))
    if dlog.path.exists():
        dlog.path.unlink()  # fresh run

    print("=" * 74)
    print("SWING BACKTEST - real Kraken 4h history  (IN-SAMPLE sanity check, NOT proof)")
    print("=" * 74)
    all_nets: list[float] = []
    for base, pair in KRAKEN_PAIRS.items():
        try:
            bars = fetch_ohlc(pair)
        except Exception as e:
            print(f"\n{base}: fetch failed - {e}")
            continue
        nets = backtest_symbol(base, bars, strat, dlog)
        all_nets += nets
        s = stats(nets)
        span_days = (bars[-1]["t"] - bars[0]["t"]) / 86400 if bars else 0
        print(f"\n{base}  ({len(bars)} bars, {span_days:.0f}d)")
        print(f"  trades={s['n']:<3} net=${s['total']:+7.2f}  win={s['win']*100:3.0f}%  "
              f"exp=${s['exp']:+.3f}/trade  t={s['t']:+.2f}  maxDD=${s['dd']:+.2f}")

    c = stats(all_nets)
    print("\n" + "-" * 74)
    print(f"COMBINED  trades={c['n']}  net=${c['total']:+.2f}  win={c['win']*100:.0f}%  "
          f"exp=${c['exp']:+.3f}/trade")
    print(f"          t-stat={c['t']:+.2f}  per-trade Sharpe={c['sharpe']:+.2f}  "
          f"maxDD=${c['dd']:+.2f}")
    verdict = ("worth forward-testing" if (c["n"] >= 10 and c["exp"] > 0 and c["t"] > 1.0)
               else "not promising in-sample — iterate the THESIS, do not curve-fit params")
    print(f"          -> {verdict}")
    print("\nDecision log:")
    print("  " + dlog.summarize().replace("\n", "\n  "))
    print(f"  full per-decision trace: {dlog.path}")
    print("=" * 74)


if __name__ == "__main__":
    main()

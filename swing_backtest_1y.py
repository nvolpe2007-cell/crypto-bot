#!/usr/bin/env python3
"""
One-year 4h-majors swing backtest.

WHY a new data source: Kraken's public OHLC caps at 720 bars (~120d at 4h), too
short for a year. We pull ~400 days of 4h bars from CryptoCompare (US-accessible,
free) by paginating hourly data aggregated to 4h. Majors' OHLC is near-identical
across venues, so this is a fair robustness check for a Kraken-spot swing book.

WHAT THIS IS: an IN-SAMPLE, MULTI-REGIME robustness check on the LOCKED strategy
(no parameter search). NOT proof — proof is the live forward record judged by
proof_scorecard.py (n>=30, expectancy>0, clustered t>2). A good year here means
"the 4h-majors edge isn't a 120-day fluke", not "fund it".

    python swing_backtest_1y.py
"""
from __future__ import annotations
import json, time, urllib.request
from datetime import datetime, timezone
from pathlib import Path

import swing_backtest as bt   # reuse backtest_symbol + stats (same exit engine)
from src.swing_strategy import SwingStrategy
from src.decision_log import DecisionLog

SYMBOLS = ["BTC", "ETH", "SOL", "LTC", "BCH", "XRP"]   # 4h-majors scope
TARGET_DAYS = 400
COSTS = [0.0055, 0.0080]    # honest maker-floor, and a stress test


def fetch_4h(fsym: str, target_days: int) -> list[dict]:
    """Page CryptoCompare histohour (aggregate=4) back to ~target_days."""
    bars: dict[int, dict] = {}
    to_ts = int(time.time())
    while True:
        url = (f"https://min-api.cryptocompare.com/data/v2/histohour"
               f"?fsym={fsym}&tsym=USD&limit=2000&aggregate=4&toTs={to_ts}")
        with urllib.request.urlopen(url, timeout=30) as r:
            d = json.loads(r.read())
        chunk = d.get("Data", {}).get("Data", [])
        chunk = [c for c in chunk if c.get("open", 0) > 0]   # drop empty pre-listing rows
        if not chunk:
            break
        for c in chunk:
            bars[c["time"]] = {"t": c["time"], "o": float(c["open"]),
                               "h": float(c["high"]), "l": float(c["low"]),
                               "c": float(c["close"])}
        earliest = min(c["time"] for c in chunk)
        newest, oldest = max(bars), min(bars)
        if (newest - oldest) / 86400 >= target_days or earliest >= to_ts:
            break
        to_ts = earliest - 1
        time.sleep(0.3)
    return [bars[k] for k in sorted(bars)]


def quarter(ts: int) -> str:
    dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    return f"{dt.year}Q{(dt.month - 1) // 3 + 1}"


def main():
    print("Fetching ~400d of 4h bars (CryptoCompare) for 6 majors...")
    series = {}
    for s in SYMBOLS:
        b = fetch_4h(s, TARGET_DAYS)
        series[s] = b
        span = (b[-1]["t"] - b[0]["t"]) / 86400 if b else 0
        d0 = datetime.fromtimestamp(b[0]["t"], tz=timezone.utc).date() if b else "-"
        d1 = datetime.fromtimestamp(b[-1]["t"], tz=timezone.utc).date() if b else "-"
        print(f"  {s:<4} {len(b):>4} bars  {d0} -> {d1}  ({span:.0f}d)")
        time.sleep(0.3)

    dlog = DecisionLog(path=Path("data/_swing_1y.jsonl"))
    for cost in COSTS:
        bt.ROUND_TRIP_COST_FRAC = cost     # monkeypatch the cost the engine charges
        print("\n" + "=" * 72)
        print(f"ONE-YEAR 4h-MAJORS SWING  (round-trip cost = {cost*100:.2f}%)  "
              f"[IN-SAMPLE robustness check, NOT proof]")
        print("=" * 72)
        all_nets, all_q = [], []
        for s in SYMBOLS:
            nets = bt.backtest_symbol(s, series[s], SwingStrategy(), dlog)
            # tag each closed trade's quarter (by its entry bar) for regime split
            st = bt.stats(nets)
            all_nets += nets
            print(f"  {s:<4} trades={st['n']:<3} net=${st['total']:+8.2f} "
                  f"win={st['win']*100:3.0f}% exp=${st['exp']:+.4f} "
                  f"t={st['t']:+.2f} maxDD=${st['dd']:+.2f}")
        c = bt.stats(all_nets)
        print("-" * 72)
        print(f"  COMBINED  trades={c['n']}  net=${c['total']:+.2f}  "
              f"win={c['win']*100:.0f}%  exp=${c['exp']:+.4f}/trade")
        print(f"            t-stat={c['t']:+.2f}  per-trade Sharpe={c['sharpe']:+.2f}  "
              f"maxDD=${c['dd']:+.2f}")

    # regime split (at honest cost) — re-run capturing entry quarter
    bt.ROUND_TRIP_COST_FRAC = COSTS[0]
    print("\n" + "=" * 72)
    print("REGIME SPLIT by calendar quarter (honest 0.55% cost) — is the edge stable?")
    print("=" * 72)
    qnets: dict[str, list[float]] = {}
    for s in SYMBOLS:
        bars = series[s]
        strat = SwingStrategy()
        pos = None
        for i in range(strat.min_bars, len(bars)):
            window, bar = bars[:i + 1], bars[i]
            if pos is not None:
                xp = xr = None
                if bar["l"] <= pos["stop"]: xp, xr = pos["stop"], "stop"
                elif bar["h"] >= pos["target"]: xp, xr = pos["target"], "target"
                else:
                    dec = strat.evaluate(window, position_open=True)
                    if dec.action == "EXIT": xp, xr = bar["c"], "trend_break"
                if xp is not None:
                    ret = (xp - pos["entry"]) / pos["entry"]
                    net = bt.SIZE_USD * ret - bt.SIZE_USD * COSTS[0]
                    qnets.setdefault(quarter(pos["t"]), []).append(net)
                    pos = None
                continue
            dec = strat.evaluate(window, position_open=False)
            if dec.is_enter:
                pos = {"entry": dec.price, "stop": dec.stop_price,
                       "target": dec.target_price, "t": bar["t"]}
    for q in sorted(qnets):
        v = qnets[q]; wins = sum(1 for x in v if x > 0)
        print(f"  {q}  trades={len(v):<3} net=${sum(v):+8.2f} "
              f"win={wins/len(v)*100:3.0f}% exp=${sum(v)/len(v):+.4f}")


if __name__ == "__main__":
    main()

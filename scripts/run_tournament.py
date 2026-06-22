#!/usr/bin/env python3
"""Run the strategy tournament and write data/tournament.json for the dashboard.

Generates 100+ candidate strategies (param grids over established families),
fetches ~2y of daily Kraken data for a basket, scores every candidate with the
honest cost + robustness + Šidák-family discipline in src/tournament.py, and
writes a machine-readable leaderboard the live dashboard reads.

    python scripts/run_tournament.py                 # BTC+ETH+SOL, 730d
    python scripts/run_tournament.py --coins BTC/USD,ETH/USD --days 540

Designed to run on a schedule (cron). Each run re-scores in-sample over the
trailing window — clearing the bar makes a candidate a CANDIDATE for forward
proof (proof_scorecard / the stage-3 allocator), never an auto-deploy.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.tournament import evaluate, generate_candidates, summarize  # noqa: E402

DATA_DIR = os.environ.get("DASHBOARD_DATA_DIR") or os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")


def _fetch(coins: list[str], days: int) -> dict:
    import ccxt  # noqa: PLC0415
    import pandas as pd  # noqa: PLC0415

    ex = ccxt.kraken({"enableRateLimit": True})
    data: dict = {}
    for sym in coins:
        o = ex.fetch_ohlcv(sym, timeframe="1d", limit=days)
        data[sym] = pd.DataFrame(o, columns=["ts", "open", "high", "low", "close", "vol"])
    return data


def _atomic_write(path: str, payload: dict) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh)
    os.replace(tmp, path)


def main() -> None:
    ap = argparse.ArgumentParser(description="strategy tournament → data/tournament.json")
    ap.add_argument("--coins", default="BTC/USD,ETH/USD,SOL/USD")
    ap.add_argument("--days", type=int, default=730)
    ap.add_argument("--top", type=int, default=25, help="rows to print to console")
    args = ap.parse_args()

    coins = [c.strip() for c in args.coins.split(",") if c.strip()]
    cands = generate_candidates()
    print(f"generated {len(cands)} candidates across "
          f"{len({c.family for c in cands.values()})} families; fetching {coins} …", flush=True)

    data = _fetch(coins, args.days)
    n_bars = len(next(iter(data.values())))
    rows = evaluate(cands, data)
    summ = summarize(rows, len(cands))

    payload = {
        "generated_at": int(time.time()),
        "coins": coins,
        "n_bars": n_bars,
        "summary": summ,
        "candidates": rows,
    }
    out = os.path.join(DATA_DIR, "tournament.json")
    _atomic_write(out, payload)

    print(f"\n{summ['n_candidates']} candidates · Šidák |t| bar = {summ['family_t_bar']} "
          f"· {summ['n_robust']} robust · {summ['n_passes_family']} pass family-bar "
          f"({summ['n_long_only_executable']} long-only executable)\n")
    print(f"{'#':>3} {'strategy':<20}{'fam':<11}{'sharpe':>7}{'t':>6}{'ret%':>8}{'mdd%':>7}"
          f"{'trd':>5}  flags")
    for i, r in enumerate(rows[:args.top], 1):
        flags = []
        if r["passes_family"]:
            flags.append("PASS-FAMILY")
        elif r["robust"]:
            flags.append("robust")
        if r["long_only_ok"]:
            flags.append("LO-exec")
        print(f"{i:>3} {r['name']:<20}{r['family']:<11}{r['sharpe']:>7.2f}{r['t_stat']:>6.1f}"
              f"{r['ret_pct']:>8.1f}{r['mdd_pct']:>7.1f}{r['trades']:>5}  {' '.join(flags)}")
    print(f"\nwrote {out}")


if __name__ == "__main__":
    main()

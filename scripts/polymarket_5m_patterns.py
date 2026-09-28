#!/usr/bin/env python3
"""
Pattern search on Polymarket 5-minute "Bitcoin Up or Down" markets.

Only patterns that need NO speed advantage are tested — every trade enters at
a price Polymarket actually printed (the prices-history point itself), so the
stale-quote artifact that killed the 15m fair-value backtest cannot occur.

Families (each judged in-sample on the first half of days, then OOS on the
second half; a pattern counts only if it is net-positive in BOTH halves):
  A. price-bucket bias   buy the side priced in bucket B at minute k
                         (favourite-longshot bias)
  B. streaks             after N same-direction windows, bet continuation / reversal
  C. hour of day         bet Up (or Down) in UTC hour h

Costs: fill at print + HALF_SPREAD, taker fee 0.07*p*(1-p)/share.
With ~100+ cells tried, expect several false IS "winners" — that is what the
OOS half is for.

Usage:  python scripts/polymarket_5m_patterns.py --days 14
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from polymarket_bot.fair_value import taker_fee_per_share  # noqa: E402
from scripts.polymarket_twap_backtest import CLOB, GAMMA, _get  # noqa: E402

W = 300
HALF_SPREAD = 0.005
CACHE = Path("data/polymarket_5m_cache.json")


def fetch(start_ts: int):
    try:
        ev = _get(f"{GAMMA}/events", {"slug": f"btc-updown-5m-{start_ts}"})
        if not ev or not ev[0].get("markets"):
            return None
        m = ev[0]["markets"][0]
        prices = json.loads(m.get("outcomePrices") or "[]")
        tokens = json.loads(m.get("clobTokenIds") or "[]")
        if not m.get("closed") or len(tokens) != 2 or prices[:1] not in (["1"], ["0"]):
            return None
        hist = _get(f"{CLOB}/prices-history",
                    {"market": tokens[0], "startTs": start_ts, "endTs": start_ts + W, "fidelity": 1})
        return {"start": start_ts, "up_won": prices[0] == "1",
                "hist": [(int(h["t"]), float(h["p"])) for h in hist.get("history", [])]}
    except Exception as exc:
        print(f"  skip {start_ts}: {exc}", flush=True)
        return None


def load(days: int):
    now = int(time.time())
    end = now - now % W - W
    starts = list(range(end - days * 86400, end, W))
    cache = json.loads(CACHE.read_text()) if CACHE.exists() else {}
    missing = [s for s in starts if str(s) not in cache]
    print(f"windows: {len(starts)} (fetching {len(missing)})", flush=True)
    with ThreadPoolExecutor(8) as pool:
        for s, w in zip(missing, pool.map(fetch, missing)):
            cache[str(s)] = w
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    CACHE.write_text(json.dumps(cache))
    return [cache[str(s)] for s in starts if cache.get(str(s))]


def ret(won: bool, print_price: float):
    ask = min(print_price + HALF_SPREAD, 0.999)
    return ((1.0 if won else 0.0) - ask - taker_fee_per_share(ask)) / ask


def stats(rs):
    n = len(rs)
    if n < 2:
        return n, 0.0, 0.0
    m = sum(rs) / n
    sd = math.sqrt(sum((r - m) ** 2 for r in rs) / (n - 1))
    return n, m, (m / (sd / math.sqrt(n)) if sd > 0 else 0.0)


def cells(windows):
    """{cell_name: [(start_ts, return), ...]} — one bet per window per cell."""
    out = defaultdict(list)
    # A. price-bucket bias at minute k: side whose printed price is in the bucket.
    for w in windows:
        for k in (1, 2, 3, 4):
            pts = [(t, p) for t, p in w["hist"] if t <= w["start"] + 60 * k]
            if not pts:
                continue
            p_up = pts[-1][1]
            for side, p, won in (("UP", p_up, w["up_won"]), ("DOWN", 1 - p_up, not w["up_won"])):
                b = int(p * 10) / 10
                if 0.05 <= p <= 0.95:
                    out[f"A bucket {b:.1f}-{b + 0.1:.1f} @min{k}"].append((w["start"], ret(won, p)))
    # B. streaks: bet at the window's first print.
    by_start = {w["start"]: w for w in windows}
    for w in windows:
        if not w["hist"]:
            continue
        p0 = w["hist"][0][1]
        for n in (1, 2, 3, 4):
            prev = [by_start.get(w["start"] - W * i) for i in range(1, n + 1)]
            if any(x is None for x in prev) or len({x["up_won"] for x in prev}) != 1:
                continue
            last_up = prev[0]["up_won"]
            cont_up = last_up
            out[f"B {n}-streak continue"].append(
                (w["start"], ret(w["up_won"] == cont_up, p0 if cont_up else 1 - p0)))
            out[f"B {n}-streak reverse"].append(
                (w["start"], ret(w["up_won"] != cont_up, 1 - p0 if cont_up else p0)))
    # C. hour of day, bet at first print.
    for w in windows:
        if not w["hist"]:
            continue
        p0 = w["hist"][0][1]
        h = datetime.fromtimestamp(w["start"], timezone.utc).hour
        out[f"C hour {h:02d} UP"].append((w["start"], ret(w["up_won"], p0)))
        out[f"C hour {h:02d} DOWN"].append((w["start"], ret(not w["up_won"], 1 - p0)))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=14)
    args = ap.parse_args()
    windows = load(args.days)
    split = windows[len(windows) // 2]["start"]
    ups = sum(w["up_won"] for w in windows)
    print(f"resolved windows: {len(windows)}   Up won {ups / len(windows):.1%}\n")

    rows = []
    for name, bets in cells(windows).items():
        is_ = stats([r for s, r in bets if s < split])
        oos = stats([r for s, r in bets if s >= split])
        rows.append((name, is_, oos))
    k = len(rows)
    t_bar = 3.0  # Harvey-Liu-Zhu honest floor for a mined family this size

    print(f"cells tried: {k}.  A pattern needs IS mean>0 AND OOS mean>0 net of costs;")
    print(f"'robust' additionally needs OOS t > {t_bar} (multiple-testing floor).\n")
    print(f"{'pattern':32s} {'IS n':>5s} {'IS mean':>8s} {'IS t':>6s}   {'OOS n':>5s} {'OOS mean':>8s} {'OOS t':>6s}")
    rows.sort(key=lambda r: -r[1][2])
    for name, (n1, m1, t1), (n2, m2, t2) in rows[:15]:
        flag = "  <- ROBUST" if m1 > 0 and m2 > 0 and t2 > t_bar else ("  <- holds OOS" if m1 > 0 and m2 > 0 else "")
        print(f"{name:32s} {n1:5d} {m1:+8.2%} {t1:+6.2f}   {n2:5d} {m2:+8.2%} {t2:+6.2f}{flag}")
    survivors = [r for r in rows if r[1][1] > 0 and r[2][1] > 0]
    robust = [r for r in survivors if r[2][2] > t_bar]
    print(f"\nIS-positive: {sum(r[1][1] > 0 for r in rows)}/{k}   positive in both halves: {len(survivors)}   "
          f"robust (OOS t>{t_bar}): {len(robust)}")
    for name, _, (n2, m2, t2) in sorted(survivors, key=lambda r: -r[2][2]):
        print(f"  both-halves: {name:32s} OOS n={n2} mean={m2:+.2%} t={t2:+.2f}")


if __name__ == "__main__":
    main()

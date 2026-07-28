#!/usr/bin/env python3
"""
Lev-perp research round 4 (2026-07-26) — round 3 showed DSR climbing sharply
with universe breadth (3 symbols: DSR 0.45-0.61 -> 8 symbols: DSR 0.89-0.90).
This round pushes to this repo's own already-vetted 16-coin liquid universe
(the same list brain_paper.py/swing_paper.py use), to see whether DSR clears
0.95 with the two round-2/3 survivors (MA10/40 cross, 2-of-3 confluence).
No new candidates invented -- k stays at 2, same as round 3.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import lev_perp_paper as lp
from proof_scorecard import _stats, _family_t_bar, _expected_max_sharpe, _deflated_sharpe, DSR_MIN
from scripts.lev_perp_regime_research import week_cluster
from scripts.lev_perp_entry_signal_research import sig_ma_cross, sig_confluence
from scripts.lev_perp_universe_research import simulate_entry_universe

CACHE = Path(__file__).resolve().parent.parent / "data" / "research_cache_lev_perp_universe16.json"
# This repo's own vetted 16-coin liquid set (brain_paper.KRAKEN_PAIRS_ALL), mapped to
# Coinbase USD product ids.
UNIVERSE = {b: f"{b}/USD" for b in
            ["BTC", "ETH", "SOL", "ADA", "DOT", "LINK", "AVAX", "LTC",
             "XRP", "ATOM", "UNI", "BCH", "DOGE", "AAVE", "FIL", "ALGO"]}
START_ISO = "2021-06-01T00:00:00Z"


def fetch_universe() -> dict[str, list[dict]]:
    if CACHE.exists():
        cached = json.loads(CACHE.read_text())
        if cached:
            print(f"[data] using cache {CACHE} ({len(cached)} symbols)")
            return cached
    import ccxt
    ex = ccxt.coinbase({"enableRateLimit": True})
    since = ex.parse8601(START_ISO)
    out: dict[str, list[dict]] = {}
    for base, pair in UNIVERSE.items():
        print(f"[data] fetching {pair} ...")
        bars: list[dict] = []
        cursor = since
        now_ms = int(time.time() * 1000)
        stall = 0
        try:
            while cursor < now_ms:
                chunk = ex.fetch_ohlcv(pair, timeframe="1d", since=cursor, limit=300)
                if not chunk:
                    stall += 1
                    if stall >= 3:
                        break
                    cursor += 300 * 86_400_000
                    continue
                stall = 0
                for row in chunk:
                    bars.append({"t": int(row[0] // 1000), "o": row[1], "h": row[2],
                                 "l": row[3], "c": row[4], "v": row[5]})
                new_cursor = chunk[-1][0] + 86_400_000
                if new_cursor <= cursor:
                    break
                cursor = new_cursor
                time.sleep(ex.rateLimit / 1000.0)
        except Exception as e:
            print(f"  {base}: fetch error ({e}) -- skipping")
            continue
        if len(bars) < 200:
            print(f"  {base}: only {len(bars)} bars -- skipping")
            continue
        seen = {b["t"]: b for b in bars}
        bars = sorted(seen.values(), key=lambda b: b["t"])
        today_cutoff = int(time.time() // 86400 * 86400)
        bars = [b for b in bars if b["t"] < today_cutoff]
        out[base] = bars
        print(f"  {base}: {len(bars)} bars")
    CACHE.parent.mkdir(exist_ok=True)
    CACHE.write_text(json.dumps(out))
    return out


def report(results: list[dict]) -> list[dict]:
    rows = []
    for r in results:
        nets = [c["pnl"] for c in r["closed"]]
        clusters = [week_cluster(c["entry_ts"]) for c in r["closed"]]
        s = _stats(nets, clusters)
        rows.append({**r, **s})
    return rows


def main():
    bars = fetch_universe()
    print(f"\nuniverse fetched: {list(bars.keys())} ({len(bars)} symbols)\n")
    warmup = 160

    candidates = [
        simulate_entry_universe(bars, warmup=warmup, entry_fn=sig_ma_cross, label="V1_ma10_40_cross_16sym"),
        simulate_entry_universe(bars, warmup=warmup, entry_fn=sig_confluence, label="V2_confluence_16sym"),
    ]
    rows = report(candidates)
    k = len(rows)
    t_family = _family_t_bar(k)
    sharpes = [r["sharpe"] for r in rows]
    sr0 = _expected_max_sharpe(sharpes)

    print("\n" + "=" * 100)
    print(f"ROUND 4 (16-coin universe) -- k={k} -> Sidak family t-bar={t_family:.3f}  sr0={sr0:.4f}")
    print("=" * 100)
    out_rows = []
    for r in rows:
        dsr = _deflated_sharpe(r["sharpe"], r["eff_n"], r["skew"], r["kurt"], sr0)
        nets_sorted = sorted(r["closed"], key=lambda c: c["entry_ts"])
        half = len(nets_sorted) // 2
        h1 = sum(c["pnl"] for c in nets_sorted[:half]) if half else 0.0
        h2 = sum(c["pnl"] for c in nets_sorted[half:]) if half else 0.0
        passed = (r["n"] >= 30 and r["expectancy"] > 0 and r["t_clustered"] > t_family and dsr > DSR_MIN)
        out_rows.append({**r, "dsr": dsr, "half1": h1, "half2": h2, "PRELIM_PASS": passed})
        print(f"{r['label']:26s} n={r['n']:4d} total=${r['total']:+9.2f} exp=${r['expectancy']:+7.3f} "
              f"WR={r['win_rate']*100:5.1f}% t_clu={r['t_clustered']:+6.2f} sharpe={r['sharpe']:+6.3f} "
              f"DSR={dsr:.3f} maxDD=${r['max_dd']:+8.2f} split=({h1:+.1f}/{h2:+.1f}) PASS={passed}")

    out_path = Path(__file__).resolve().parent.parent / "data" / "lev_perp_universe16_results.json"
    out_path.write_text(json.dumps({"k": k, "t_family": t_family, "sr0": sr0, "rows": [
        {kk: vv for kk, vv in row.items() if kk != "closed"} for row in out_rows
    ]}, indent=2, default=str))
    print(f"\nwrote {out_path}")


if __name__ == "__main__":
    main()

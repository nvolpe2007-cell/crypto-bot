#!/usr/bin/env python3
"""
Lev-perp research round 3 (2026-07-26) — round 2 found MA10/40-cross and
2-of-3 confluence entries meaningfully better than SMA-50 (best DSR 0.61 vs
0.45, both split-halves positive) but still short of the bar. This round
tests the one remaining structural lever that has worked elsewhere in this
repo (the AI-brain arm's "expand universe, don't loosen any gate" result):
more independent liquid majors -> more independent trades -> more
statistical power, if the edge genuinely generalizes rather than being a
BTC/ETH/SOL-specific fluke.

Universe: BTC, ETH, SOL, ADA, XRP, DOT, AVAX, LINK (Coinbase-listed liquid
majors, same list style as MAJOR_SYMBOLS elsewhere in this repo). Same v2
chandelier exit, same vol-targeted leverage, same production entry filter
(RSI/trend-age/volume/ADX), margin re-split evenly across however many
symbols actually have data. Only the two round-2 survivors (MA10/40 cross,
2-of-3 confluence) are retested here -- no new candidates invented, to keep
k small and the family-wise bar as loose as honestly possible.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import lev_perp_paper as lp
import lev_perp_v2_paper as v2mod
from proof_scorecard import _stats, _family_t_bar, _expected_max_sharpe, _deflated_sharpe, DSR_MIN
from scripts.lev_perp_regime_research import _close_generic, week_cluster
from scripts.lev_perp_entry_signal_research import sig_ma_cross, sig_confluence

CACHE = Path(__file__).resolve().parent.parent / "data" / "research_cache_lev_perp_universe8.json"
UNIVERSE = {"BTC": "BTC/USD", "ETH": "ETH/USD", "SOL": "SOL/USD", "ADA": "ADA/USD",
            "XRP": "XRP/USD", "DOT": "DOT/USD", "AVAX": "AVAX/USD", "LINK": "LINK/USD"}
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
            print(f"  {base}: only {len(bars)} bars -- skipping (not enough Coinbase history)")
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


def simulate_entry_universe(bars_by_symbol: dict[str, list[dict]], *, warmup: int, entry_fn,
                             label: str = "") -> dict:
    n_symbols = len(bars_by_symbol)
    margin_each = round(lp.STARTING_EQUITY / max(1, n_symbols), 2)
    closed: list[dict] = []
    for base, bars in bars_by_symbol.items():
        if len(bars) < warmup:
            continue
        closes_full = [b["c"] for b in bars]
        pos = None
        prior_side = 1
        for idx in range(warmup, len(bars)):
            bar = bars[idx]
            closes = closes_full[: idx + 1]
            bars_so_far = bars[: idx + 1]

            want = entry_fn(closes, prior_side) if entry_fn.__name__ == "sig_donchian" else entry_fn(closes)
            if want is None:
                continue
            prior_side = want

            if pos:
                ex = v2mod._check_exit(pos, bar)
                if ex:
                    price, reason = ex
                    rec = _close_generic(pos, price, str(bar["t"]), reason)
                    closed.append(rec)
                    pos = None
            if pos and pos["side"] != want:
                rec = _close_generic(pos, bar["c"], str(bar["t"]), "flip")
                closed.append(rec)
                pos = None
            if pos:
                v2mod._ratchet(pos, bar)
            if not pos:
                skip: list = []
                if lp._entry_filter(bars_so_far, closes, skip):
                    lev = max(0.1, min(lp.LEVERAGE, lp._effective_leverage(closes)))
                    atr = v2mod._atr(bars_so_far) or bar["c"] * 0.02
                    pos = {
                        "symbol": base, "side": want, "entry": bar["c"], "entry_ts": str(bar["t"]),
                        "margin_usd": margin_each, "leverage": round(lev, 2),
                        "notional_usd": round(margin_each * lev, 2), "peak": bar["c"], "atr": round(atr, 8),
                        "trail": round(bar["c"] - want * v2mod.ATR_MULT * atr, 8),
                        "liq": round(bar["c"] * (1 - want * max(1e-6, (1 - lp.MAINT) / lev)), 8),
                        "open_idx": idx,
                    }
        if pos:
            rec = _close_generic(pos, closes_full[-1], str(bars[-1]["t"]), "window_end")
            closed.append(rec)
    return {"label": label, "closed": closed}


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
        simulate_entry_universe(bars, warmup=warmup, entry_fn=sig_ma_cross, label="U1_ma10_40_cross_8sym"),
        simulate_entry_universe(bars, warmup=warmup, entry_fn=sig_confluence, label="U2_confluence_8sym"),
    ]
    rows = report(candidates)
    k = len(rows)
    t_family = _family_t_bar(k)
    sharpes = [r["sharpe"] for r in rows]
    sr0 = _expected_max_sharpe(sharpes)

    print("\n" + "=" * 100)
    print(f"ROUND 3 (universe expansion) -- k={k} -> Sidak family t-bar={t_family:.3f}  sr0={sr0:.4f}")
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

    out_path = Path(__file__).resolve().parent.parent / "data" / "lev_perp_universe_results.json"
    out_path.write_text(json.dumps({"k": k, "t_family": t_family, "sr0": sr0, "rows": [
        {kk: vv for kk, vv in row.items() if kk != "closed"} for row in out_rows
    ]}, indent=2, default=str))
    print(f"\nwrote {out_path}")


if __name__ == "__main__":
    main()

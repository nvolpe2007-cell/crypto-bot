#!/usr/bin/env python3
"""
Lev-perp entry-signal research, round 2 (2026-07-26) — round 1 found the
SMA-50 entry itself carries too thin an edge for any exit/sizing/regime
refinement to survive the proof bar. This round replaces the ENTRY signal,
keeping the proven v2 ATR-chandelier exit and vol-targeted leverage fixed,
to isolate whether the bottleneck is really the entry.

Candidates (each a standard, well-known time-series technique — not a
"strategy lookup", first-principles signal processing / trend statistics):
  D0 — TSMOM-14: sign of the 14-day return (time-series momentum; this
       repo's own 2026-07-01 research found this the strongest survivor
       pre-leverage, +25-27% over 410 days on BTC).
  D1 — MA 10/40 cross: fast/slow moving-average crossover direction.
  D2 — Donchian-20 breakout: long if close > 20-day high, short if
       close < 20-day low, else hold prior side.
  D3 — 2-of-3 confluence: SMA-50, TSMOM-14 and MA10/40 cross all vote;
       trade only when >=2 agree (reduces whipsaw from a single signal).
  D4 — TSMOM-14 + Hurst gate (round 1's best regime filter, retested on a
       different entry in case the interaction differs).

All run through the SAME production cost/margin/funding/liquidation model
(lev_perp_paper.TRADE_COST_FRAC, FUNDING_APY, MAINT) and the SAME v2
chandelier exit, and judged by the SAME proof_scorecard stats machinery.
"""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import lev_perp_paper as lp
import lev_perp_v2_paper as v2mod
from proof_scorecard import _stats, _family_t_bar, _expected_max_sharpe, _deflated_sharpe, DSR_MIN
from scripts.lev_perp_regime_research import fetch_all_daily, hurst_exponent, _close_generic, week_cluster

CACHE = Path(__file__).resolve().parent.parent / "data" / "research_cache_lev_perp_daily.json"


# ── Entry signals (return +1 / -1; None = insufficient data) ────────────────
def sig_tsmom(closes: list[float], n: int = 14) -> int | None:
    if len(closes) < n + 1:
        return None
    ret = closes[-1] / closes[-n - 1] - 1
    return 1 if ret >= 0 else -1


def sig_ma_cross(closes: list[float], fast: int = 10, slow: int = 40) -> int | None:
    if len(closes) < slow:
        return None
    fma = sum(closes[-fast:]) / fast
    sma = sum(closes[-slow:]) / slow
    return 1 if fma >= sma else -1


def sig_donchian(closes: list[float], n: int = 20, prior_side: int = 1) -> int | None:
    if len(closes) < n + 1:
        return None
    window = closes[-(n + 1):-1]
    hi, lo = max(window), min(window)
    c = closes[-1]
    if c > hi:
        return 1
    if c < lo:
        return -1
    return prior_side  # inside the channel: hold prior side (Donchian is stateful)


def sig_confluence(closes: list[float]) -> int | None:
    votes = []
    sma = lp._sma(closes, lp.SMA_N)
    if sma is not None:
        votes.append(lp._target_side(closes[-1], sma))
    t = sig_tsmom(closes)
    if t is not None:
        votes.append(t)
    m = sig_ma_cross(closes)
    if m is not None:
        votes.append(m)
    if len(votes) < 3:
        return None
    up = sum(1 for v in votes if v > 0)
    down = sum(1 for v in votes if v < 0)
    if up >= 2:
        return 1
    if down >= 2:
        return -1
    return None  # no confluence -> stay flat


# ── Simulator (entry-signal pluggable; exit = v2 chandelier; lev = vol-target) ─
def simulate_entry(bars_by_symbol: dict[str, list[dict]], *, warmup: int, entry_fn,
                    gate_fn=None, label: str = "") -> dict:
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

            want = entry_fn(closes, prior_side) if entry_fn is sig_donchian else entry_fn(closes)
            if want is None:
                continue
            prior_side = want

            # 1) exit vs prior-bar chandelier / liq
            if pos:
                ex = v2mod._check_exit(pos, bar)
                if ex:
                    price, reason = ex
                    rec = _close_generic(pos, price, str(bar["t"]), reason)
                    closed.append(rec)
                    pos = None

            # 2) signal-flip exit at close
            if pos and pos["side"] != want:
                rec = _close_generic(pos, bar["c"], str(bar["t"]), "flip")
                closed.append(rec)
                pos = None

            # 3) ratchet survivor
            if pos:
                v2mod._ratchet(pos, bar)

            # 4) re-entry (same production entry FILTER — RSI/trend-age/vol/ADX — stays on;
            #    only the DIRECTION signal changes)
            if not pos:
                skip: list = []
                if lp._entry_filter(bars_so_far, closes, skip) and (gate_fn is None or gate_fn(closes, bars_so_far)):
                    lev = max(0.1, min(lp.LEVERAGE, lp._effective_leverage(closes)))
                    margin = lp.MARGIN
                    atr = v2mod._atr(bars_so_far) or bar["c"] * 0.02
                    pos = {
                        "symbol": base, "side": want, "entry": bar["c"], "entry_ts": str(bar["t"]),
                        "margin_usd": margin, "leverage": round(lev, 2),
                        "notional_usd": round(margin * lev, 2), "peak": bar["c"], "atr": round(atr, 8),
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
    bars = fetch_all_daily()
    warmup = 160

    def gate_hurst(closes, _bars):
        h = hurst_exponent(closes)
        return h is not None and h > 0.5

    candidates = [
        simulate_entry(bars, warmup=warmup, entry_fn=sig_tsmom, label="D0_tsmom14"),
        simulate_entry(bars, warmup=warmup, entry_fn=sig_ma_cross, label="D1_ma10_40_cross"),
        simulate_entry(bars, warmup=warmup, entry_fn=sig_donchian, label="D2_donchian20"),
        simulate_entry(bars, warmup=warmup, entry_fn=sig_confluence, label="D3_confluence_2of3"),
        simulate_entry(bars, warmup=warmup, entry_fn=sig_tsmom, gate_fn=gate_hurst, label="D4_tsmom14_hurst_gate"),
    ]
    rows = report(candidates)
    k = len(rows)
    t_family = _family_t_bar(k)
    sharpes = [r["sharpe"] for r in rows]
    sr0 = _expected_max_sharpe(sharpes)

    print("\n" + "=" * 100)
    print(f"ROUND 2 -- k={k} candidates -> Sidak family t-bar={t_family:.3f}  sr0={sr0:.4f}")
    print("=" * 100)
    out_rows = []
    for r in rows:
        dsr = _deflated_sharpe(r["sharpe"], r["eff_n"], r["skew"], r["kurt"], sr0)
        nets_sorted = sorted(r["closed"], key=lambda c: c["entry_ts"])
        half = len(nets_sorted) // 2
        h1 = sum(c["pnl"] for c in nets_sorted[:half]) if half else 0.0
        h2 = sum(c["pnl"] for c in nets_sorted[half:]) if half else 0.0
        split_ok = h1 > 0 and h2 > 0
        passed = (r["n"] >= 30 and r["expectancy"] > 0 and r["t_clustered"] > t_family
                  and dsr > DSR_MIN)
        out_rows.append({**r, "dsr": dsr, "half1": h1, "half2": h2, "split_both_positive": split_ok,
                          "PRELIM_PASS": passed})
        print(f"{r['label']:24s} n={r['n']:4d} total=${r['total']:+9.2f} exp=${r['expectancy']:+7.3f} "
              f"WR={r['win_rate']*100:5.1f}% t_clu={r['t_clustered']:+6.2f} sharpe={r['sharpe']:+6.3f} "
              f"DSR={dsr:.3f} maxDD=${r['max_dd']:+8.2f} split=({h1:+.1f}/{h2:+.1f}) PASS={passed}")

    out_path = Path(__file__).resolve().parent.parent / "data" / "lev_perp_entry_signal_results.json"
    out_path.write_text(json.dumps({"k": k, "t_family": t_family, "sr0": sr0, "rows": [
        {kk: vv for kk, vv in row.items() if kk != "closed"} for row in out_rows
    ]}, indent=2, default=str))
    print(f"\nwrote {out_path}")


if __name__ == "__main__":
    main()

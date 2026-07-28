#!/usr/bin/env python3
"""
Owner question 2026-07-26: lev_perp v1 (fixed-TP) looks decent on the forward
clock right now (n=11, +$124, 73% win) -- why, and can MORE trades be taken
at the SAME win rate?

Digging into the actual closed-trade record (data/lev_perp_state.json on the
VPS) answered the "why": all 11 trades are SHORT, entered 2026-06-14 through
2026-06-25 and exited through 2026-07-04 -- one clean, monotonic BTC/ETH/SOL
downtrend produced 8 straight take-profit hits, then the trend reversed and
produced 2 clean -5% stop-losses (the pre-2026-07-01 SOL trade that lost
-44.25% on a "flip" close is the EXACT historical incident that motivated
adding the hard stop-loss that session -- not a new bug, already fixed). Zero
NEW entries since 2026-07-04 (three weeks idle) confirms the live constraint:
SMA-50 direction changes on 3 symbols are rare.

This script tests the direct, filter-preserving answer to "more trades, same
quality": widen the traded universe the way `scripts/lev_perp_universe_research.py`
(2026-07-26, PR #85) already validated for OTHER entries (MA10/40-cross,
confluence) -- 8 liquid majors, same production entry filters (RSI/trend-age/
volume/ADX) -- but for v1's OWN actual direction signal and exit mechanics
(SMA-50 vs close, fixed TP/SL, vol-targeted leverage, correlation cap), which
that prior session did NOT test (it swapped v1's SMA-50 signal out for
MA10/40-cross before widening the universe). Reuses `lev_perp_paper.py`
UNMODIFIED (process_symbol, _check_exit, _open, _close, _entry_filter) --
only MARGIN is rescaled for a fair n-symbol split, matching the pattern in
`lev_perp_universe_research.py`. No in-memory state is ever saved to
data/lev_perp_state.json -- this never touches the live paper book.

Data: same Coinbase daily cache already fetched in
data/research_cache_lev_perp_universe8.json (BTC, ETH, SOL, ADA, XRP, DOT,
AVAX, LINK, 2021-06 onward) -- no new fetch needed.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import lev_perp_paper as lp  # noqa: E402
from proof_scorecard import _stats, _family_t_bar, _expected_max_sharpe, _deflated_sharpe, DSR_MIN  # noqa: E402

CACHE = ROOT / "data" / "research_cache_lev_perp_universe8.json"
UNIVERSE_8 = ["BTC", "ETH", "SOL", "ADA", "XRP", "DOT", "AVAX", "LINK"]
UNIVERSE_3 = ["BTC", "ETH", "SOL"]
WARMUP = lp.SMA_N + 5  # a few extra bars so _trend_age/_adx/_vol_ratio all have data


def week_cluster(ts: str) -> str:
    from datetime import datetime, timezone
    dt = datetime.fromtimestamp(int(ts), tz=timezone.utc)
    return f"{dt.isocalendar().year}-W{dt.isocalendar().week:02d}"


def run(bars_by_symbol: dict[str, list[dict]], symbols: list[str], label: str) -> dict:
    universe = {b: bars_by_symbol[b] for b in symbols if b in bars_by_symbol}
    n = len(universe)
    lp.MARGIN = round(lp.STARTING_EQUITY / max(1, n), 2)  # fair per-symbol split, like universe_research.py

    state = {"positions": {}, "closed": [], "last_bar_t": {},
              "leverage": lp.LEVERAGE, "tp_price_frac": lp.TP_PRICE_FRAC,
              "liq_price_frac": lp.LIQ_PRICE_FRAC,
              "starting_equity": lp.STARTING_EQUITY, "equity": lp.STARTING_EQUITY,
              "started_at": ""}

    for base, bars in universe.items():
        if len(bars) < WARMUP + 10:
            continue
        # Seed last_bar_t at warmup so process_symbol walks the REST of history
        # as "new" bars (RESEARCH_2026-07-02's method) instead of forward-only seeding.
        state["last_bar_t"][base] = bars[WARMUP]["t"]
        lp.process_symbol(base, bars, state)

    return {"label": label, "closed": state["closed"], "equity": state["equity"]}


def report(result: dict) -> dict:
    nets = [c["pnl"] for c in result["closed"]]
    clusters = [c["symbol"] for c in result["closed"]]
    s = _stats(nets, clusters)
    print(f"\n=== {result['label']} ===")
    print(f"n={s['n']} total=${s['total']:+.2f} win_rate={s['win_rate']*100:.1f}% "
          f"expectancy=${s['expectancy']:+.4f}/trade")
    print(f"sharpe(per-trade)={s['sharpe']:.4f} t_clustered={s['t_clustered']:.2f} eff_n={s['eff_n']:.1f}")
    by_reason: dict[str, int] = {}
    for c in result["closed"]:
        by_reason[c["reason"]] = by_reason.get(c["reason"], 0) + 1
    print(f"  close reasons: {by_reason}")
    return s


def main():
    if not CACHE.exists():
        print(f"Cache {CACHE} not found -- run scripts/lev_perp_universe_research.py first to fetch it.")
        sys.exit(1)
    bars_by_symbol = json.loads(CACHE.read_text())
    print(f"Loaded cached daily bars for: {list(bars_by_symbol.keys())}")

    # Suppress process_symbol's per-action print noise (it's designed for a live
    # log and includes emoji that crash on the Windows cp1252 console anyway).
    import builtins
    _real_print = builtins.print
    def _quiet_print(*a, **k):
        pass

    results = []
    for symbols, label in ((UNIVERSE_3, "v1 SMA-50, 3-coin (production universe)"),
                           (UNIVERSE_8, "v1 SMA-50, 8-coin (widened universe)")):
        builtins.print = _quiet_print
        try:
            r = run(bars_by_symbol, symbols, label)
        finally:
            builtins.print = _real_print
        print(f"[done] {label}: {len(r['closed'])} closed trades")
        results.append(r)

    stats = [report(r) for r in results]

    print("\n" + "=" * 78)
    print("VERDICT (k=1: one new candidate -- wider universe -- vs the existing")
    print("production 3-coin config; no sweep)")
    t_bar = _family_t_bar(1)
    for r, s in zip(results, stats):
        if s["n"] < 2:
            print(f"{r['label']}: n={s['n']} -- too few trades to judge.")
            continue
        dsr = _deflated_sharpe(s["sharpe"], s["eff_n"], s["skew"], s["kurt"], sr0=0.0)
        cleared = s["n"] >= 30 and s["expectancy"] > 0 and s["t_clustered"] >= t_bar and dsr > DSR_MIN
        print(f"{r['label']}: n={s['n']} win={s['win_rate']*100:.0f}% "
              f"expectancy=${s['expectancy']:+.4f} t_clustered={s['t_clustered']:.2f} "
              f"DSR={dsr:.3f} -> {'CLEARS BAR' if cleared else 'does not clear bar'}")
    print("=" * 78)


if __name__ == "__main__":
    main()

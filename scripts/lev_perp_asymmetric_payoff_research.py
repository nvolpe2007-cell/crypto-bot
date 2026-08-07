#!/usr/bin/env python3
"""
lev_perp: asymmetric payoff (TP = 3x SL) on v1's fixed-exit mechanism, 8 coins.

PRE-REGISTERED HYPOTHESIS (declared before the run, single value, no sweep)
--------------------------------------------------------------------------
v1 exits at a FIXED +5% take-profit against a FIXED -5% stop. That is a
symmetric payoff bolted onto a trend-following system, and trend-following's
entire economic basis is POSITIVE SKEW -- many small losses funded by rare
large winners. Capping every winner at exactly +5% structurally forbids the
fat right tail the strategy exists to harvest, leaving a ~53% win rate on a
1:1 payoff, which costs eat.

The repo's own data already says the cap is what hurts, holding entry and
universe constant:
    C0_v1_fixed_tp_sl   (capped   +5%/-5%)  sharpe = +0.0106
    C1_v2_chandelier    (uncapped, trailing) sharpe = +0.0662   <- 6x
and on 8 coins: v1 fixed-TP 0.0415 vs U1 chandelier 0.0860 (~2x).

But the chandelier buys that Sharpe by holding longer, collapsing trade count
(n=307 vs n=856 on the same 8 coins) -- and t = sharpe * sqrt(eff_n) means
frequency is half the battle. THE UNTESTED MIDDLE: keep the fixed-exit
mechanism's high re-entry frequency, but restore skew by widening ONLY the
take-profit to 3x the stop.

    TP_PRICE_FRAC 0.05 -> 0.15   (SL_PRICE_FRAC stays 0.05)

3:1 is pre-registered from theory (the standard trend-following payoff ratio),
NOT swept. If this fails, record the null -- do not then try 2:1, 4:1, 5:1.
That would convert k=1 into a sweep and raise the bar for all of them.

HONEST k -- THIS IS THE EXPENSIVE PART
--------------------------------------
RESEARCH_2026-07-26_lev_perp_regime_and_entry_search.md's standing lesson:
judging a round's k in isolation is cherry-picking, because a round's
survivors have pre-selected similar Sharpes, which depresses sr0 and inflates
DSR. So this script pools the Sharpe of EVERY lev_perp candidate ever tried
(19 on record, including the strongly negative ones) plus this one, and feeds
that pool to _expected_max_sharpe for sr0. The Sidak t-bar likewise uses the
cumulative k, not k=1.

SAFETY: read-only. Never touches data/lev_perp_state.json.
"""
from __future__ import annotations

import builtins
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import lev_perp_paper as lp  # noqa: E402
from proof_scorecard import (  # noqa: E402
    _stats, _family_t_bar, _expected_max_sharpe, _deflated_sharpe, DSR_MIN, N_MIN,
)
from scripts.lev_perp_v1_macross_universe_research import (  # noqa: E402
    run, sig_sma50, week_cluster, UNIVERSE_8, CACHE,
)

OUT = ROOT / "data" / "lev_perp_asymmetric_payoff_results.json"
PRIOR_POOL = ROOT / "data" / "lev_perp_pooled_sharpes.json"

TP_ASYM = 0.15   # pre-registered: 3x the stop
SL_FIXED = 0.05


def judge(result: dict) -> dict:
    nets = [c["pnl"] for c in result["closed"]]
    s_sym = _stats(nets, [c["symbol"] for c in result["closed"]])
    s_week = _stats(nets, [week_cluster(c["entry_ts"]) for c in result["closed"]])
    ordered = sorted(result["closed"], key=lambda c: int(c["entry_ts"]))
    half = len(ordered) // 2
    by_reason: dict[str, int] = {}
    for c in result["closed"]:
        by_reason[c["reason"]] = by_reason.get(c["reason"], 0) + 1
    return {
        "label": result["label"], "n": s_sym["n"], "total": s_sym["total"],
        "win_rate": s_sym["win_rate"], "expectancy": s_sym["expectancy"],
        "sharpe": s_sym["sharpe"], "skew": s_sym["skew"], "kurt": s_sym["kurt"],
        "t_symbol": s_sym["t_clustered"], "eff_n_symbol": s_sym["eff_n"],
        "t_week": s_week["t_clustered"], "eff_n_week": s_week["eff_n"],
        "half1": sum(c["pnl"] for c in ordered[:half]) if half else 0.0,
        "half2": sum(c["pnl"] for c in ordered[half:]) if half else 0.0,
        "close_reasons": by_reason,
    }


def main() -> None:
    bars = json.loads(CACHE.read_text())
    print(f"Universe: {UNIVERSE_8}")
    print(f"PRE-REGISTERED: TP={TP_ASYM:.0%} / SL={SL_FIXED:.0%} (3:1), "
          f"SMA-50 entry, fixed-exit mechanism, 8 coins\n")

    arms = []
    real_print = builtins.print
    for tp, label in ((0.05, "BASELINE  TP 5% / SL 5% (production, symmetric)"),
                      (TP_ASYM, "CANDIDATE TP 15% / SL 5% (3:1 asymmetric)")):
        lp.TP_PRICE_FRAC = tp
        lp.SL_PRICE_FRAC = SL_FIXED
        builtins.print = lambda *a, **k: None
        try:
            r = run(bars, UNIVERSE_8, sig_sma50, label)
        finally:
            builtins.print = real_print
        arms.append(judge(r))
        j = arms[-1]
        print(f"=== {j['label']} ===")
        print(f"  n={j['n']}  total=${j['total']:+.2f}  win={j['win_rate']*100:.1f}%  "
              f"expectancy=${j['expectancy']:+.4f}  sharpe={j['sharpe']:+.4f}  skew={j['skew']:+.3f}")
        print(f"  t(sym)={j['t_symbol']:+.2f}  t(week)={j['t_week']:+.2f}  "
              f"eff_n(week)={j['eff_n_week']:.1f}")
        print(f"  halves={j['half1']:+.1f}/{j['half2']:+.1f}   reasons={j['close_reasons']}\n")

    # ── honest cumulative multiple-testing correction ────────────────────────
    prior = json.loads(PRIOR_POOL.read_text()) if PRIOR_POOL.exists() else []
    prior_sharpes = [s for _, s in prior]
    cand = arms[1]
    pool = prior_sharpes + [cand["sharpe"]]
    k = len(pool)
    t_bar = _family_t_bar(k)
    sr0 = _expected_max_sharpe(pool)
    dsr = _deflated_sharpe(cand["sharpe"], cand["eff_n_week"], cand["skew"],
                           cand["kurt"], sr0)
    print("=" * 78)
    print(f"CUMULATIVE multiple-testing correction over ALL lev_perp candidates")
    print(f"  pooled k = {k}   Sidak t-bar = {t_bar:.2f}   sr0 = {sr0:.4f}")
    print(f"  candidate sharpe = {cand['sharpe']:+.4f}")
    if cand["sharpe"] <= sr0:
        print(f"  -> candidate Sharpe does NOT exceed sr0: it is indistinguishable")
        print(f"     from the luckiest of {k} attempts. DSR is 0 by construction.")
    print(f"  t(week) {cand['t_week']:.2f} vs bar {t_bar:.2f}  -> "
          f"{'PASS' if cand['t_week'] >= t_bar else 'FAIL'}")
    print(f"  DSR(week, pooled sr0) = {dsr:.3f} vs bar {DSR_MIN}  -> "
          f"{'PASS' if dsr > DSR_MIN else 'FAIL'}")
    cleared = (cand["n"] >= N_MIN and cand["expectancy"] > 0
               and cand["t_week"] >= t_bar and dsr > DSR_MIN)
    print(f"  VERDICT: {'CLEARS' if cleared else 'DOES NOT CLEAR'} the bar")
    print("=" * 78)

    # what Sharpe would actually be needed, at this frequency
    deff = cand["n"] / cand["eff_n_week"]
    need_sharpe = t_bar / ((cand["n"] / deff) ** 0.5)
    print(f"\nAt this candidate's n={cand['n']} and week design-effect {deff:.2f},")
    print(f"clearing t={t_bar:.2f} would require per-trade sharpe >= {need_sharpe:.4f}")
    print(f"(best ever found across all {k} lev_perp candidates: {max(pool):+.4f})")

    OUT.write_text(json.dumps({"pooled_k": k, "t_family": t_bar, "sr0": sr0,
                               "required_sharpe": need_sharpe, "rows": arms},
                              indent=2, default=str))
    print(f"\nwrote {OUT}")


if __name__ == "__main__":
    main()

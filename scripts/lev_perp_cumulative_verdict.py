#!/usr/bin/env python3
"""Honest cumulative verdict: pools the Sharpe ratios of EVERY candidate tried
across all 4 research rounds this session (17 total) to compute the TRUE
Sidak family-wise t-bar and expected-max-Sharpe (sr0) for the deflated-Sharpe
calc -- judging each round's k in isolation understates how many things were
actually tried in one continuous search."""
from __future__ import annotations
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from proof_scorecard import _family_t_bar, _expected_max_sharpe, _deflated_sharpe, DSR_MIN

DATA = Path(__file__).resolve().parent.parent / "data"
files = ["lev_perp_regime_research_results.json", "lev_perp_entry_signal_results.json",
         "lev_perp_universe_results.json", "lev_perp_universe16_results.json"]

all_rows = []
for f in files:
    d = json.loads((DATA / f).read_text())
    all_rows.extend(d["rows"])

k = len(all_rows)
t_family = _family_t_bar(k)
sharpes = [r["sharpe"] for r in all_rows]
sr0 = _expected_max_sharpe(sharpes)

print(f"CUMULATIVE across the whole session: k={k} candidates tried")
print(f"Sidak family t-bar = {t_family:.3f}   expected-max-Sharpe sr0 = {sr0:.4f}\n")

best = None
for r in all_rows:
    dsr = _deflated_sharpe(r["sharpe"], r["eff_n"], r["skew"], r["kurt"], sr0)
    passed = r["n"] >= 30 and r["expectancy"] > 0 and r["t_clustered"] > t_family and dsr > DSR_MIN
    r["_cumulative_dsr"] = dsr
    r["_cumulative_pass"] = passed
    if best is None or r["sharpe"] > best["sharpe"]:
        best = r
    print(f"{r['label']:26s} n={r['n']:4d} t_clu={r['t_clustered']:+6.2f} sharpe={r['sharpe']:+6.3f} "
          f"DSR(cumulative)={dsr:.3f} PASS={passed}")

print(f"\nBest single candidate by Sharpe: {best['label']} "
      f"(sharpe={best['sharpe']:.3f}, cumulative DSR={best['_cumulative_dsr']:.3f}, "
      f"needs t_clu>{t_family:.2f} has {best['t_clustered']:.2f})")

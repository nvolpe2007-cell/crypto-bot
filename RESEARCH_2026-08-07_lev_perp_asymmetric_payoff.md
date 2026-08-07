# RESEARCH 2026-08-07 — lev_perp asymmetric payoff (TP 3× SL): hypothesis validated, bar not cleared

Second pre-registered test of the 2026-08-07 session (the first,
`RESEARCH_2026-08-07_lev_perp_v1_macross_universe.md`, was a mechanical null).
Script: `scripts/lev_perp_asymmetric_payoff_research.py`. Read-only.

## Pre-registered hypothesis (declared before the run, one value, no sweep)

v1 exits at a fixed **+5% TP against a −5% SL** — a symmetric payoff bolted
onto a trend-following system. Trend-following's entire economic basis is
**positive skew**: many small losses funded by rare large winners. Capping every
winner at +5% structurally forbids the fat right tail the strategy exists to
harvest, leaving a ~53% win rate on a 1:1 payoff for costs to eat.

The repo's own trial history already pointed here, holding entry and universe
constant:

| config | exit | sharpe |
|---|---|---|
| `C0_v1_fixed_tp_sl` | capped +5%/−5% | +0.0106 |
| `C1_v2_chandelier` | uncapped trailing | **+0.0662** (6×) |

But the chandelier buys Sharpe by holding longer, collapsing trade count
(n=307 vs 856 on the same 8 coins) — and `t = sharpe × √eff_n` means frequency
is half the battle. **The untested middle:** keep the fixed-exit mechanism's
high re-entry frequency, restore skew by widening only the take-profit to 3× the
stop. `TP_PRICE_FRAC 0.05 → 0.15`, `SL_PRICE_FRAC` unchanged at 0.05. 3:1 chosen
from theory (standard trend-following payoff ratio), not swept.

## Result — every predicted effect appeared

| | n | win | expectancy | sharpe | **skew** | t(wk) | halves |
|---|---|---|---|---|---|---|---|
| BASELINE 5%/5% | 856 | 52.8% | +$0.161 | +0.0415 | **−0.087** | 0.82 | −10.2 / +147.7 |
| **CANDIDATE 15%/5%** | 562 | 28.6% | **+$0.447** | **+0.0735** | **+1.407** | 1.23 | **+87.4 / +163.6** |

- **Skew flips −0.087 → +1.407.** The mechanism now has the trend-following
  signature it was structurally prevented from having. This is the hypothesis
  confirmed directly, not inferred.
- Sharpe **+77%**, expectancy per trade **+178%**, total +$137 → +$251.
- Win rate collapses 52.8% → 28.6% exactly as predicted — you win far less
  often, for much more. Close reasons shift from 451 TP / 391 SL to 155 TP /
  376 SL.
- **First config in this repo's lev_perp history with both split-halves
  positive** (+87.4 / +163.6). The baseline's first half is negative.

## But it does not clear the bar — and the reason is the trial history

Applying the standing lesson from
`RESEARCH_2026-07-26_lev_perp_regime_and_entry_search.md` (pool ALL candidates
ever tried before computing sr0, not just a round's survivors):

```
pooled k   = 20        Sidak t-bar = 3.02        sr0 = 0.0953
candidate sharpe = 0.0735   ->  BELOW sr0
t(week) 1.23 vs 3.02  FAIL      DSR 0.351 vs 0.95  FAIL
```

The candidate's Sharpe is **below the expected maximum Sharpe of 20 random
attempts**. Under this repo's own honest accounting it is indistinguishable
from the luckiest draw of the search, and DSR is ~0 by construction.

To clear t=3.02 at n=562 with week design-effect 2.01 would need per-trade
sharpe ≥ **0.1806** — against a best-ever-found of 0.0980 across all 20
candidates. The gap is a factor of ~1.8 on the best result the repo has ever
produced, not a tuning distance.

## What this is and isn't

**It is not proven edge.** It cannot be claimed as one, and the allocator should
keep lev_perp benched.

**It is a better-motivated configuration than production.** Both 5%/5% and
15%/5% are unproven, but the paper arm is going to accrue forward sample on
*one* of them, and 15%/5% dominates on every robustness measure available
(skew, both split-halves, expectancy/trade) and rests on an economic argument
rather than a fitted parameter. Switching the paper arm's TP is a defensible
choice about *which unproven config to gather evidence on* — not a claim of
edge. Cost: n drops 856 → 562 over the same window, so forward sample accrues
~35% slower.

## Do not re-propose without new evidence

- **Sweeping the payoff ratio** (2:1, 4:1, 5:1, …). 3:1 was pre-registered from
  theory and run once precisely so it stayed k=1 within this test. Trying more
  ratios converts it into a sweep, pushes pooled k past 20, and raises the bar
  for every candidate including this one.
- **Claiming 15%/5% as an edge** because skew and split-halves improved. Those
  are robustness diagnostics, not significance; the pooled DSR is 0.351.
- **Any further lev_perp variant judged at k=1.** The honest cumulative count is
  now 20; `_family_t_bar(20) = 3.02`, and every new candidate raises it further.

## Standing follow-on

The binding constraint is unchanged and now quantified twice: **per-trade Sharpe
must roughly double** (0.098 best-ever → ~0.18 needed). Nothing in the
price-derived, daily-bar, 8-liquid-major search space has produced that in 20
attempts across four sessions. Any future attempt should come from a
**structurally different information source** (order flow, funding, cross-
sectional rank) rather than another function of the same daily closes — and
should be pre-registered as a single hypothesis before it is run.

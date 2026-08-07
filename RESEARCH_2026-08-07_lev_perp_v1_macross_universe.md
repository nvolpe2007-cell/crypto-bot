# RESEARCH 2026-08-07 — lev_perp v1: MA10/40-cross × 8-coin × fixed-TP exit (PRE-REGISTERED, k=1)

Owner asked for a profitable strategy for the bot. Rather than sweeping new
candidates (which raises the Šidák bar — `proof_scorecard._family_t_bar`), this
round ran the **single hypothesis already written down** as the standing
follow-on in `RESEARCH_2026-07-26_lev_perp_v1_frequency.md`:

> whether combining this session's universe widening WITH PR #85's better entry
> signal (MA10/40-cross) clears the bar outright — that specific combination
> (MA10/40-cross entry × 8-coin universe × v1's own fixed-TP exit, as opposed to
> v2's chandelier) has not been tested yet.

Because it was named in writing *before* the run, this is judged **k=1**
(t-bar 2.00, DSR 0.95, n≥30). Script:
`scripts/lev_perp_v1_macross_universe_research.py`. Read-only — the live
`data/lev_perp_state.json` hash was verified byte-identical before and after.

## Harness validation (control reproduces PR #87 exactly)

v1's SMA-50 arm re-run through the new loop, versus PR #87's published numbers:

| | n | win | expectancy | t | DSR |
|---|---|---|---|---|---|
| PR #87 published | 856 | 52.8% | +$0.161 | 1.21 | 0.887 |
| this run (control) | 856 | 52.8% | +$0.161 | 1.21 | 0.887 |

Exact match. The harness reuses `lp._entry_filter`, `lp._effective_leverage`,
`lp._open`, `lp._close`, `lp._check_exit` unmodified; only the direction
decision is swapped.

## Result — the pre-registered hypothesis is NULL

| arm | n | win | expectancy | sharpe | t(sym) | DSR(sym) | t(week) | DSR(week) |
|---|---|---|---|---|---|---|---|---|
| CONTROL SMA-50 × 8 | 856 | 52.8% | +$0.161 | 0.0415 | 1.21 | 0.887 | 0.82 | 0.793 |
| **CANDIDATE MA-cross × 8** | **858** | **53.1%** | **+$0.181** | **0.0464** | **1.20** | **0.885** | **0.90** | **0.815** |

**Does not clear the bar.** Blockers: t_symbol 1.20<2.00, DSR_symbol
0.885≤0.95, t_week 0.90<2.00, DSR_week 0.815≤0.95. The candidate is
indistinguishable from the control — marginally better raw total (+$155 vs
+$137) and marginally *worse* DSR under symbol clustering.

## Why it's null: the two signals are the same signal

Direct measurement of `close-vs-SMA50` against `MA10/40-cross` over the full
8-coin history: **they agree on 87.9% of bars** (84.1–89.6% per symbol,
12,025 / 13,676). The production entry filters (RSI/trend-age/volume/ADX) gate
most of the remaining 12%. ADA and DOT produce *identical* net P&L in both arms
(+$18.53, +$82.88) — the same trades, because the signals never disagreed at a
bar that passed the filters.

**This is a mechanical null, not evidence about entry-signal quality.** PR #85's
finding that MA10/40 "beat SMA-50 on Sharpe in the 3-coin round" was a
small-sample artifact of the 12% of bars where they differ.

## The more important finding: PR #87's 0.887 was optimistic

PR #87 clustered by **symbol**, which yields a design effect of **1.00** for the
control (eff_n = 856.0 = n exactly — i.e. no deflation applied at all).
Sequential trades on one coin are roughly independent draws, so symbol
clustering detects nothing. What actually correlates in crypto is trades opened
in the *same week across different coins*.

Clustering by week gives design effect **2.19–2.27** and drops the control's DSR
from **0.887 → 0.793**. The number that looked "closest the repo has come to the
bar" was substantially a clustering artifact. The honest DSR for v1 on 8 coins
is ~0.79, not ~0.89.

Both arms also **fail the split-half gate** PR #85 required: first half negative
in both (−$10.2 control, −$0.5 candidate); essentially all profit is in the
second half. And DOT alone is $82.88 of the candidate's $155 (53%), with
BTC/SOL/XRP negative in both arms.

## Decisive: this mechanism cannot be proven at this frequency

At the observed per-trade Sharpe, sample required to reach t=2.00 under
week-clustering (`eff_n = (2.0/sharpe)²`, `n = eff_n × design_effect`):

| arm | sharpe | deff | n now | n for t=2.00 | ≈ years of 8-coin daily | or coins needed for 5yr |
|---|---|---|---|---|---|---|
| CONTROL | 0.0415 | 2.19 | 856 | 5,091 | **30.6** | 48 |
| CANDIDATE | 0.0464 | 2.27 | 858 | 4,229 | **25.4** | 39 |

At the live forward rate (~165 trades/yr on 8 coins) that is **~26 years**. The
"more coins" lever is closed: PR #85 already found 16 coins is *worse* than 8,
and 39 simultaneously liquid majors with 5 years of history do not exist.

**The binding constraint is per-trade Sharpe, not trade count.** To clear the
bar at the current n=858, the mechanism would need sharpe ≈ **0.103** — a
**2.2×** improvement over the observed 0.046. No amount of universe widening
delivers that.

## Verdict

lev_perp v1 has a small positive expectancy (+$0.16–0.18/trade) that is
**statistically indistinguishable from zero and not provable in a human
timeframe**. The allocator's existing behaviour — leaving it benched — is
correct. This closes the universe-widening line of research.

## Do not re-propose without new evidence

- **Varying the fast/slow MA lengths** (5/20, 20/60, …) to chase the 0.95 bar.
  That converts this pre-registered k=1 into a sweep; at k=10 the t-bar rises to
  2.80 and the observed t=1.20 falls even further short.
- **Widening the universe past 8.** Tested twice now: 16 coins is worse (PR #85),
  and the power calc shows even 39 coins only reaches t=2.00 at the current
  Sharpe.
- **Citing DSR 0.887 as "nearly proven."** It is 0.793–0.815 under time
  clustering, which is the dependence structure that actually applies.
- **Treating the live 73%/n=11 window as informative** — already refuted in
  `RESEARCH_2026-07-26_lev_perp_v1_frequency.md`; real long-run rate is ~51–53%.

## Standing follow-on

If lev_perp is revisited, the only lever with the right sign is **raising
per-trade Sharpe ~2.2×**, which means changing what the arm *is* (exit
mechanics, position sizing, or a genuinely uncorrelated entry — signals that
agree 88% of the time are not candidates), not how many coins it trades. Any
such attempt should be pre-registered as a single hypothesis before it is run.

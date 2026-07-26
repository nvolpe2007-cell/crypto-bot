# RESEARCH 2026-07-26 — lev_perp regime/entry/universe search (honest null result)

Owner-requested search for a genuinely more profitable lev_perp config, using
math/statistics derived from first principles (no internet strategy lookup),
judged by an independent Fable-model reviewer against this repo's own
pre-registered proof bar (`proof_scorecard.py`: n>=30, expectancy>0,
Sidak family-corrected t-bar, Deflated Sharpe Ratio > 0.95). Method notes and
scripts kept for reuse: `scripts/lev_perp_regime_research.py`,
`scripts/lev_perp_entry_signal_research.py`, `scripts/lev_perp_universe_research.py`,
`scripts/lev_perp_universe16_research.py`, `scripts/lev_perp_cumulative_verdict.py`.

## Method

Every candidate reused the PRODUCTION cost/margin/funding/liquidation model
(imported directly from `lev_perp_paper.py`/`lev_perp_v2_paper.py`, not
reimplemented) so results are directly comparable to the live arms. Data:
Coinbase daily OHLCV via ccxt (keyless), 2021-06 onward.

Four rounds, 17 total candidates:
- **Round 1** (exit/sizing mechanics, SMA-50 entry unchanged): Hurst-exponent
  (R/S analysis) regime gate, lag-1 autocorrelation gate, GARCH(1,1)
  grid-MLE vol-forecast sizing, continuous-time Kelly (mu/sigma^2) leverage,
  chop-timeout exit, and a Hurst+Kelly combo — vs the v1/v2 production
  baselines.
- **Round 2** (entry signal replaced, v2 chandelier exit kept): TSMOM-14,
  MA10/40 cross, Donchian-20 breakout, 2-of-3 confluence, TSMOM+Hurst.
- **Round 3** (universe expanded 3 -> 8 liquid coins, best round-2 entries
  retested): MA10/40 cross, confluence.
- **Round 4** (universe expanded to this repo's existing 16-coin vetted
  list): same two entries.

## Findings, in order of what actually moved the needle

1. **Kelly/GARCH/Hurst/autocorrelation/chop-timeout refinements to the
   existing SMA-50 entry did nothing** (round 1). Kelly-leverage variants
   inflated total P&L via variance, not edge — max drawdown 87-95% of the
   book, worst Sharpe/DSR of the round. Autocorrelation gate actively lost
   money. This reconfirms `RESEARCH_2026-07-01`'s volatility-drag finding on
   a wider set of mechanics.
2. **Replacing the entry signal (MA10/40 cross, 2-of-3 confluence) helped
   more than any exit/sizing tweak** — best Sharpe in round 2 (0.098 vs
   round 1's best 0.066), both split-halves of the window positive.
3. **Universe breadth from 3 -> 8 liquid coins helped a lot** (DSR-in-isolation
   0.61 -> 0.89), but **8 -> 16 coins made it WORSE** (0.89 -> 0.85) — the
   extra 8 alts (LTC/ATOM/UNI/BCH/DOGE/AAVE/FIL/ALGO) diluted the edge with
   noise rather than adding proportional power. 8 liquid majors was the
   local optimum found.
4. **The honest cumulative verdict is the one that matters.** Judging each
   round's small k in isolation was itself methodologically wrong — Fable
   caught this independently: a round's "winners" have nearly-identical
   Sharpes (near-zero variance), which mechanically depresses
   `_expected_max_sharpe` (sr0) and makes DSR look good. Pooling the Sharpe
   of all 17 candidates tried this session (several strongly negative:
   Donchian -0.060, autocorrelation-gate -0.117) gives the honest sr0=0.098
   — which is **essentially equal to the best Sharpe found (0.098,
   MA10/40-cross / confluence, 8-coin universe)**. That is the plain-language
   verdict: the best result of the day is statistically indistinguishable
   from the luckiest draw among 17 attempts. Cumulative DSR: 0.43-0.50 vs
   the 0.95 bar. t_clustered 0.90-1.22 vs the required 2.97. Not a
   near-miss — roughly 40-50% of the way there on both measures.

## Verdict

**Nothing cleared the bar.** Best candidate: MA10/40-cross or 2-of-3
confluence entry, v2 chandelier exit, 8-coin liquid universe (BTC, ETH, SOL,
ADA, XRP, DOT, AVAX, LINK), vol-targeted leverage — Sharpe 0.086-0.098,
DSR 0.43-0.50 (need >0.95). Consistent with every other exhaustive search
already on record in this repo (`exhaustive_search_320_zero`,
`strategy_lab_100_zero_survivors`).

## Standing lesson for future agents

**Judging each research round's multiple-testing correction in isolation is
a subtle form of cherry-picking** — a round's surviving candidates tend to
have similar (already-selected) Sharpes, which understates the trial pool's
true variance and inflates the Deflated Sharpe Ratio. Any multi-round search
session should pool ALL candidates tried (including the failures) before
computing `_expected_max_sharpe`/DSR for a final verdict, not just the
round-local winners. Also: **"try a wider universe" has a peak, not a
monotonic benefit** — 8 quality-liquid coins beat both 3 and 16 here; don't
assume more coins is always more power.

## Do not re-propose without new evidence

- Kelly/GARCH/Hurst/autocorrelation-gate/chop-timeout refinements on the
  SMA-50 entry (round 1, all failed, several actively harmful).
- A 16-coin (or wider) universe for this specific entry/exit combo (worse
  than 8).
- Re-running this exact search with the same data source/window expecting a
  different answer — the honest constraint is structural (see round 4
  finding #4), not a tuning problem.

# RESEARCH 2026-07-26 — funding decay/half-life entry timing (honest null result)

Owner-requested continuation of market research. Tests the one remaining
unaddressed item from `STRATEGY_REVIEW.md`'s Week-3 roadmap: *"Time entries on
decay/half-life (enter only when funding z-score is high and persistence >=3
cycles and predicted hold clears the wall)."* Never built until this session.
Script: `scripts/funding_decay_research.py` (kept for reuse).

## What was ruled out first (before designing this test)

Checked whether Kraken's dated/calendar futures (`FI_*`) offer a basis/
term-structure carry — `STRATEGY_REVIEW.md`'s other unaddressed Week-3 item
("the real path to break the maker wall"). Live check of
`futures.kraken.com/derivatives/api/v3/tickers`: all `FI_*` contracts have
**zero bid/ask, zero 24h volume, zero open interest, and mark price pegged
exactly to index** — a listed-but-untraded product. No basis exists to
harvest; this is a liquidity/product-availability dead end, not a math
problem (consistent with this repo's recurring "the gap is the data feed/
venue, not the math" pattern). Not pursued further.

## Method

Real Kraken Futures hourly funding-rate history (public, keyless,
`historicalfundingrates` endpoint), ~377 days (2025-07-13 -> 2026-07-26), 29
liquid majors overlapping `arbitrage.funding_arb_paper.MAJOR_SYMBOLS`. Cash-
and-carry funding-arb P&L is delta-neutral by construction, so no price data
is needed — only the funding-rate series and the production cost/gate model.

Two variants, same cost/exit model (Kraken-arm production defaults: cost
0.54% round-trip, 7d max hold, exit on funding flip or APY decay to 40% of
entry confirmed over 2h, 48h cooldown after a losing close, max 6 sign-flips
per trailing 30d), differing ONLY in the entry-persistence signal:

- **Baseline** — production logic exactly: snapshot-rate breakeven gate
  (cost clears within 4 cycles, ~148% APY floor at this cost) + simple
  consecutive-positive-hours gate (>=24h).
- **OU-timed** — replaces the consecutive-hours check with a causal AR(1)
  fit (mean `mu`, decay `phi`) projecting cumulative expected funding over
  ~3 half-lives forward; only enters if the projection clears the 0.54% cost.

Method note caught mid-session: fitting AR(1) on the *raw* hourly rate gives
half-lives of ~1.5-2h (hour-to-hour noise decay, not the day-scale
persistence the gate cares about) — projected capture never cleared cost.
Fitting on a causal 24h rolling mean of the rate instead gives half-lives of
tens to hundreds of hours (the actual funding-level persistence) and
projected capture in the same order of magnitude as the cost. This fix was
made once, before running the full 29-symbol universe — not a parameter
sweep.

Only ONE new candidate (OU-timed) is judged against the existing production
logic (baseline) — not a multi-config search — so `proof_scorecard`'s
family-wise correction is k=1 (`_family_t_bar(1)` = `T_MIN` = 2.0, no Sidak
widening). Verdict uses `proof_scorecard._stats` / `_deflated_sharpe`
directly, `sr0=0` (single hypothesis, no multiple-testing deflation needed).

## Results

| Variant | Trades (29 majors, ~1yr) | Net | Win rate |
|---|---|---|---|
| Baseline (production, as configured) | **0** | — | — |
| OU-timed | **1** | −$0.38 | 0% |

Baseline reproduces the already-known "zero qualifying setups" finding
exactly (`hypothesis_registry.yaml`: `funding-arb-kraken`) — confirms the
replication is faithful. OU-timed finds one entry (XRP, held 51h, exited on
`apy_decayed`) that baseline's simpler gate misses — and it lost money. n=1
is not remotely enough to judge, but it's the whole result: across 29 coins
and ~254,000 symbol-hours, decay-aware timing surfaces one opportunity
baseline structurally cannot see, and that opportunity was a loser.

**Diagnostic (not a proposal): what if the 6-flips/30d gate were removed?**
Kraken settles funding hourly, and hourly rates flip sign constantly — median
36-96 sign flips per 30-day window on the coins that otherwise would have
cleared the OU-projected-capture bar (TIA 622 candidate hours, median 68
flips; INJ 340 candidate hours, median 58 flips; HBAR, LINK similar). The
6-flip cap (tuned for 8-hourly Binance/Bybit cadence) is the actual binding
constraint keeping both variants near-zero, not the entry-timing math. Re-ran
OU-timed with that gate disabled to see whether it was hiding real edge:
**n=102, net −$45.16, 1% win rate, expectancy −$0.44/trade, t=−41.24.**
Unambiguous — the flip gate is correctly protective, not miscalibrated. Its
removal reveals an even worse book, not a hidden one. Do not revisit the
flip-count threshold expecting it to be gatekeeping profit away.

## Verdict

**Nothing cleared the bar; the hypothesis is rejected, not just "insufficient
data."** Funding-decay/half-life timing does not rescue the Kraken funding-arb
arm. The 0.54% round-trip cost against Kraken's genuinely noisy hourly
funding (constant sign-flipping even on majors) leaves essentially no
capturable persistence at any entry-timing sophistication tested. This
completes both remaining unaddressed items from `STRATEGY_REVIEW.md`'s Week-3
roadmap (basis/term-structure: dead market; decay-timed entries: rejected,
n=1/n=102 both negative) with the same conclusion the rest of this repo's
research keeps reaching: the arm is correctly idle, not under-engineered.

## Do not re-propose without new evidence

- AR(1)/OU decay timing (in this form) as a way to unlock more Kraken
  funding-arb trades — tested, rejected (n=1 loser; n=102 diagnostic clearly
  negative once the flip gate is relaxed).
- Loosening or removing the 6-flips/30d persistence gate to "let more trades
  through" — the diagnostic shows exactly what's on the other side of that
  gate, and it's worse, not better.
- Kraken calendar/dated-futures basis trading — the market is untraded
  (zero OI/volume/bid-ask), not a signal-quality problem.

## Standing lesson for future agents

Fitting a decay/half-life model on raw high-frequency data captures whatever
noise process dominates that frequency, not necessarily the slower process
the strategy actually cares about — check what timescale the model's
half-life implies (here: ~1.5h, obviously too short for a multi-day carry
decision) before trusting a projection built on it. A causal rolling-mean
smooth before fitting is a reasonable, one-time methodological fix; sweeping
the smoothing window to chase a better number would not be.

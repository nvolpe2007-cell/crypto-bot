# Volatility-based buy/sell signals — three designs tested, two killed, one premise falsified

**Date:** 2026-09-06
**Ask:** a simpler indicator using the volatility/dealer-exposure idea that beats the
current win rate with "real buy signals before a big move", shippable to TradingView.
**Outcome:** the volatility angle does not work here in any of the three shapes tested.
What shipped instead is the plain trend rule (`pine/trend_signal_honest.pine`), which
is the only thing measured with a profit factor above 1.
**Scripts:** scratchpad only (`volwall_backtest.py`, `volsqueeze_backtest.py`,
`premise_test.py`, `trend_vol_overlay.py`, `verify_shipped_rule.py`) — none promoted,
because none of them found anything worth re-running.

**Data for everything below:** BTC and ETH daily bars, Coinbase, 2019-01-01 to
2026-09-06 (~2805 bars each), 0.54% round-trip cost, non-overlapping trades.

---

## Why this was attempted at all

The GEX pipeline built 2026-09-05 (`src/gex/`) computes dealer-exposure walls from
Deribit's option chain. TradingView's Pine has no options-chain access at all, so a
faithful port is impossible. The question became whether the *mechanism* behind the
walls — volatility suppression and release — is visible in price alone, since that
part is Pine-computable.

Three designs, tested in order. Each was abandoned on its measurement, not tuned.

## 1. Fade the volatility bands (mean reversion) — FAILS

Bands at `SMA(20) * (1 ± k*realized_vol)`, enter on reclaim of a breached band while
volatility is not expanding, exit at the basis.

| k | trades | win% | PF | total | buy&hold |
|---|---|---|---|---|---|
| 1.5 | 68 | 33.8 | 0.421 | -57.7% | +1984% |
| 2.0 | 80 | 37.5 | 0.640 | -45.9% | +1984% |
| 2.5 | 76 | 36.8 | 0.662 | -45.5% | +1984% |

All 9 cells (3 k-values x 3 hold limits) lose, both split-halves negative in every
one. The diagnosis is in the trade mix: **43-50 shorts against 26-37 longs across a
20x bull market** — the rule systematically fought the dominant trend. Consistent
with `meanrev_dead`.

## 2. Volatility compression -> expansion breakout — FAILS, AND ITS PREMISE IS FALSE

Enter on a break of the recent range while realized vol sits in the bottom quartile
of its own 100-day history; trail out on a vol-scaled chandelier.

| variant | trades | win% | PF | total |
|---|---|---|---|---|
| long-only, trend-filtered (pct=0.20) | 33 | 30.3 | 0.324 | -38.2% |
| long-only, trend-filtered (pct=0.25) | 39 | 30.8 | 0.338 | -41.9% |
| both sides (pct=0.25) | 73 | 32.9 | 0.456 | -65.3% |
| **control: same breakout, NO compression filter** | 136 | 33.1 | **0.553** | -133.9% |

Note the control: the compression filter scored *worse* than not filtering at all in
several cells. That prompted testing the premise directly rather than another variant.

**The premise test — the most valuable result in this document.** Forget trading
rules: does realized-vol compression predict a larger subsequent move?

| horizon | \|move\| after compression | after high vol | unconditional | t | p |
|---|---|---|---|---|---|
| 5d | 4.67% | 5.27% | 5.13% | **-2.46** | **0.014** |
| 10d | 7.31% | 7.54% | 7.52% | -0.64 | 0.521 |
| 20d | 11.91% | 11.06% | 11.33% | +1.45 | 0.148 |

**No. At 5 days it significantly predicts the OPPOSITE** — moves after compression
are *smaller*. At 10 and 20 days there is no difference. Volatility clusters: low
volatility begets low volatility. The "coiled spring must release" idea did not
survive contact with this data at these horizons. Direction conditional on
compression was also insignificant (p = 0.12 to 0.85).

This kills the whole family, not just the one implementation.

## 3. Volatility regime as an OVERLAY on trend — MAKES A GOOD RULE WORSE, 12/12

The 2026-06-08 market-structure verdict said GEX's only defensible use would be an
overlay on the trend allocation ("negative-GEX = trends extend"). Tested directly:
plain `close > SMA(n)` baseline, versus the same rule gated on volatility regime.

| BTC daily, SMA(100) | trades | win% | PF | total |
|---|---|---|---|---|
| trend only | 50 | 46.0 | **5.30** | +1299% |
| + vol-expanding filter | 91 | 47.3 | 1.42 | +49% |
| + vol-contracting filter | 95 | 49.5 | 2.01 | +116% |

| ETH daily, SMA(100) | trades | win% | PF | total |
|---|---|---|---|---|
| trend only | 60 | 26.7 | **3.74** | +441% |
| + vol-expanding filter | 96 | 45.8 | 1.27 | **-59%** |
| + vol-contracting filter | 105 | 52.4 | 1.62 | +185% |

**Every overlay, on both assets, at all three MA lengths — 12 of 12 cells — raised
the win rate and destroyed the profit factor.** The ETH row is the cleanest
demonstration this repo has produced of the win-rate trap: the filter nearly doubled
the win rate (26.7% -> 45.8%) and turned +441% into -59%.

Mechanism: the filter chops long trends into more, shorter trades (60 -> 96), each
paying the round-trip toll, and it exits precisely during the conditions in which the
few enormous winners accrue. Trend following earns from a small number of very large
wins; anything that cuts those short trades expectancy for hit rate.

This independently reproduces, in a new shape, the lesson from the 2026-09-05
`orderflow_universal.pine` tpMult sweep (win rate up, profit factor down) — there via
an asymmetric take-profit, here via a regime filter. **Two different levers, same
trap. It is worth naming as a standing failure mode: any change that raises the win
rate while increasing trade count should be assumed to be destroying expectancy until
proven otherwise.**

## What shipped, and its honest limits

`pine/trend_signal_honest.pine` — close vs SMA(100) with a 2% hysteresis band, and
nothing else. Measured on the shipped defaults:

| | trades | win% | PF | total | maxDD | Sharpe | buy&hold (its DD) |
|---|---|---|---|---|---|---|---|
| BTC | 19 | 47.4 | 8.08 | +2841% | -44.1% | 1.02 | +1437% (-76.7%) |
| ETH | 23 | 34.8 | 8.21 | +1576% | -68.3% | 0.60 | +1318% (-79.4%) |

Same parameters on both assets, both beat buy-and-hold with smaller drawdown, both
split-halves positive. **And it is still not proven:**

| | t | p | median trade | biggest win | its share of gross profit |
|---|---|---|---|---|---|
| BTC | +1.44 | 0.17 | **-1.92%** | +476% | **59%** |
| ETH | +1.08 | 0.29 | **-7.93%** | +1203% | **81%** |

Neither clears t > 2, let alone the family-wise Sidak bar (~3.0 at the ~20 candidates
tried across these sessions). **The median trade loses money on both assets**; the
result rests on one enormous trade each. n = 19-23 is small, the window contains one
dominant bull market, and there is no walk-forward or out-of-sample split.

A 4% band scored higher than the shipped 2% (BTC PF 11.18). It was deliberately not
selected — 2% was chosen a priori to match `tsmom_paper.py`'s existing convention, and
picking the best-scoring band after seeing results is the overfit this whole exercise
exists to avoid.

## Do not re-propose without new evidence

- **Volatility-compression breakout / "squeeze" signals** on daily crypto bars. The
  premise was tested directly and falsified; it is not a matter of finding better
  entry mechanics.
- **Mean-reversion fades of volatility bands.** Loses, and fights the dominant trend.
- **Volatility-regime filters on the trend rule.** 12/12 cells worse.
- **Any variant justified by an improved win rate.** That number was raised
  repeatedly here while the money got worse.

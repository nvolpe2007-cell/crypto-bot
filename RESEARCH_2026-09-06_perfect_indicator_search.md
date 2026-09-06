# The "100% win rate" indicator: built, measured, and what was found instead

**Date:** 2026-09-06 · **Tools:** `scripts/win_rate_100_lab.py`, `scripts/equity_indicator_lab.py`
**Ships:** `pine/trend_signal_stocks.pine` (live in the owner's TradingView account)

## The request

> "a perfect trading indicator for crypto and a separate one for stocks that has a
> 100% win rate and beats fees"

A 100% win rate is not achievable for a directional strategy in a liquid market. But
asserting that is cheap, so it was **built three ways and measured** instead.

---

## Part 1 — The 100% win rate, constructed and priced

`win_rate_100_lab.py`, on BTC/ETH/SOL + SPY/QQQ/GLD, real costs.

### A. Never realise a loss (hold every position until green)

Realised win rate **100% on every asset, by construction**. What the win rate does not report:

| asset | closed | win% | avg win | median hold | max hold | worst unrealised | signals missed | open at end |
|---|---|---|---|---|---|---|---|---|
| BTC | 18 | 100% | +1.81% | 2 | 12 | −10.1% | 1 | — |
| ETH | 21 | 100% | +2.82% | 1 | 126 | −35.9% | 1 | — |
| SOL | 12 | 100% | +2.39% | 2 | **614** | **−91.7%** | 19 | **−51.1%** |
| SPY | 7 | 100% | +0.27% | 1 | 326 | −22.2% | 4 | — |
| QQQ | 11 | 100% | +0.74% | 1 | 99 | −16.1% | 1 | — |
| GLD | 9 | 100% | +0.64% | 1 | 9 | −3.6% | 0 | — |

SOL: one position held **614 bars (1.7 years)**, down **91.7%** at its worst, still open
and **−51%** at the end of the sample, and **19 BUY signals missed** while the capital was
trapped in it. The losses did not disappear — they moved to a column the win rate does not
report, and took the opportunity cost with them.

### B. Martingale (double down every 10% adverse move)

100% win rate on every asset. Peak position size required as a multiple of the first bet:
BTC 1x, QQQ 2x, SPY 4x, ETH 8x, **SOL 64x**. The strategy is solvent until the run of
doublings that ends it, and then it is not a losing trade — it is the account.

### C. Tiny target (+0.5%) vs wide stop (−20%)

| asset | closed | win% | avg win | avg loss | expectancy | terminal |
|---|---|---|---|---|---|---|
| BTC | 19 | **0%** | 0.00% | −0.04% | −0.040% | 0.99x |
| ETH | 23 | **0%** | 0.00% | −0.93% | −0.931% | 0.79x |
| SOL | 31 | **0%** | 0.00% | −1.36% | −1.363% | 0.62x |
| SPY | 9 | 89% | +0.40% | −20.10% | −1.878% | 0.82x |
| QQQ | 11 | **100%** | +0.40% | 0.00% | **+0.400%** | **1.04x** |
| GLD | 7 | **100%** | +0.40% | 0.00% | +0.400% | 1.03x |

Two things here answer the original request directly.

**The crypto rows have a 0% win rate.** A 0.5% target cannot clear a 0.54% round-trip
cost — every "winner" is a loss before direction is even considered. That is what "beats
fees" actually binds on, and no amount of signal quality fixes it.

**QQQ and GLD literally satisfy the request.** 100% win rate, positive expectancy, beats
fees. QQQ turned 1.00x into **1.04x over eleven years** while holding QQQ made roughly 3x.
The specification was met and the result is the worst outcome on the page.

### Conclusion of Part 1

Win rate is a **free parameter**. It can be set anywhere up to 100% without touching
whether the strategy makes money — A moves losses to the unrealised column and locks up
capital, B converts a bounded loss into an unbounded one, C makes losses rare and enormous.
What cannot be manufactured is **expectancy**. This is the fourth independent reproduction
in this repo (tpMult, the 12/12 volatility overlay, the 3R/5R take-profit, this).

---

## Part 2 — The stock indicator: a broad-universe test that corrected a prior result

The 2026-09-06 R:R run found SPY/QQQ/GLD posting a 60–64% win rate and 3.2–6.8 payoff on
the SMA(100)+2% rule — the best-looking result in this repo. That was three assets with
8–11 trades each. `equity_indicator_lab.py` retested across **70 US tickers** (large caps
across 9 sectors + 17 ETFs), 2015→2026, 0.10% round-trip, reporting the **median across
the universe**, never the best name.

### It did not survive

| | median |
|---|---|
| win rate | **37%** (not 60–64%) |
| expectancy | +2.98%/trade |
| t-stat | **0.98** |
| strategy | 1.56x |
| buy & hold | **3.72x** |
| **beat buy-and-hold** | **6% of 70 assets** |
| max drawdown | −29.3% (buy & hold −43.4%) |

Both halves of the sample agree (13% / 9% beat B&H), so this is not a regime story.

### The full parameter grid — 0 of 16 cells beat buy-and-hold on a majority

| MA | band | win% | exp% | payoff | t | strat | B&H | beatBH | maxDD |
|---|---|---|---|---|---|---|---|---|---|
| 50 | 0% | 24% | 0.83 | 4.68 | 1.09 | 1.87x | 3.73x | 1% | −28.4% |
| 50 | 1% | 36% | 1.64 | 3.08 | 1.23 | 1.80x | 3.73x | 4% | −29.2% |
| 50 | 2% | 42% | 2.57 | 2.55 | 1.29 | 1.90x | 3.73x | 4% | −28.5% |
| 50 | 4% | 45% | 3.71 | 2.48 | 1.06 | 1.75x | 3.73x | 1% | −31.8% |
| 100 | 0% | 21% | 1.03 | 6.36 | 0.98 | 1.68x | 3.72x | 4% | −32.8% |
| 100 | 1% | 30% | 2.14 | 4.40 | 1.00 | 1.66x | 3.72x | 4% | −30.8% |
| 100 | 2% | 37% | 2.98 | 3.58 | 0.98 | 1.56x | 3.72x | 6% | −29.3% |
| 100 | 4% | 43% | 4.33 | 2.74 | 0.98 | 1.61x | 3.72x | 6% | −29.3% |
| 150 | 0% | 21% | 1.71 | 7.73 | 1.06 | 1.88x | 3.79x | 3% | −30.5% |
| 150 | 1% | 30% | 2.98 | 5.55 | 1.06 | 1.85x | 3.79x | 4% | −28.4% |
| 150 | 2% | 33% | 4.26 | 4.72 | 0.98 | 1.76x | 3.79x | 4% | −28.7% |
| 150 | 4% | 46% | 5.32 | 3.10 | 0.98 | 1.53x | 3.79x | 6% | −27.5% |
| 200 | 0% | 20% | 2.13 | 9.38 | 1.06 | 1.99x | 3.82x | 4% | −26.2% |
| **200** | **1%** | **29%** | **4.40** | **7.03** | **1.08** | **2.05x** | **3.82x** | **4%** | **−24.5%** |
| 200 | 2% | 36% | 5.61 | 5.10 | 1.12 | 1.97x | 3.82x | 7% | −24.2% |
| 200 | 4% | 46% | 10.65 | 3.87 | 1.03 | 1.99x | 3.82x | 7% | −25.3% |

No ridge, no spike, no cell that works. The rule does not produce alpha on stocks.

### What it DOES do, measured rather than asserted

At SMA(200)+1%: median drawdown **−24.5%** vs buy-and-hold's **−43.2%**, and it is smaller
on **90% of the 70 assets**. That is a real, robust, reproducible effect — it is just a
**risk** effect, not a return effect. On 90% of names it takes materially less pain; on 96%
it makes less money.

**So the stock indicator ships as a drawdown tool, labelled as one.** SMA(200) was chosen
because it is the canonical industry trend filter — decided decades before this grid was
run — and it also posted the smallest drawdown. Had an unusual length merely scored best it
would have been rejected as a fit.

---

## Verification (this is the part that matters)

- **15 unit tests**, including a proof that the Pine script's stateless `ta.barssince`
  formulation is equivalent to the plain long/flat state machine on 3,000 random bars at
  four band settings — the one place the script could silently implement a different rule
  from the one measured.
- Two of those tests **failed on first run and were bugs in the tests, not the code**: one
  put its crash inside the MA warmup (which `replay()` correctly excludes from buy-and-hold),
  the other used monotonic ramps that yield 1 trade against a ≥3-trade filter. Fixed
  properly rather than by weakening the assertions; a test was added for each behaviour.
- Full suite: **3587 passed, 9 failed — all pre-existing** and unrelated.
- **Compiled and saved in TradingView** as "Trend Signal (Stocks)". The pasted source was
  hashed in-browser and matched the repo file **byte-for-byte** (SHA-256
  `9d2055a2…f230c9`).
- **Verified computing on live SPY daily data**, not just compiling. Legend reported
  `200 1 SMA 712.15 719.27 705.03`; 712.15 × 1.01 = 719.27 ✓ and × 0.99 = 705.03 ✓, and an
  independent Python SMA(200) of SPY returned **712.15 — matching to the cent**.
- That live check surfaced a real discrepancy: **TradingView charts unadjusted closes**
  (adjusted SMA200 = 709.87) while the lab used dividend-adjusted data. Signal timing moves
  ~0.3%; total return does not match unless ADJ is on. Documented in the script header.

## Limits

70 tickers over one 11-year window that contains a single major bear market; the median is
a robust statistic but the assets are correlated, so the effective sample is far below 70.
Median t = 0.98 — **nothing here clears the proof bar, and nothing here is claimed to.**
The drawdown result is the only finding stated as reliable, and it is a risk claim.

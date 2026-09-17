# Walk-forward: how much of the trend rule survives when you cannot peek

**Date:** 2026-09-08 · **Tool:** `scripts/walkforward_trust.py`
**Data:** 10 Coinbase daily assets (2018→2026) + 70 US tickers (2015→2026).
**Costs:** 0.54% round-trip crypto, 0.10% equities.

## Why this test and not another backtest

Every prior measurement of this rule — the original crypto backtest, the 8-asset OOS
check, the 70-ticker grid — chose parameters with the whole history visible. That is
exactly how a result looks trustworthy and then isn't. Walk-forward is the only test that
answers *"would this have worked in real time"*:

> Split each asset's history into consecutive windows. For each test window, choose
> `(ma_len, band)` using **only** the windows before it. Trade the test window with that
> choice and never revisit it. Concatenate the test windows — that sequence is the only
> honest track record.

Three arms on identical test windows: **ADAPTIVE** (re-tunes every fold), **FIXED** (the
shipped parameters, never tuned), **HOLD** (buy and hold, pays no fees, needs no signal).

No lookahead: the MA at bar *i* uses only bars ≤ *i*, parameter choice for a test window
uses only bars strictly before it, entries are on confirmed closes, and an open position at
a window's end is marked to market so a winning open trade cannot be quietly dropped.

## Result 1 — tuning this rule is noise

| | crypto (10) | equities (70) |
|---|---|---|
| ADAPTIVE beat FIXED | **2 / 10** | **28 / 70** |
| best parameter CHANGED between consecutive folds | **54%** | **54%** |

Re-tuning lost to leaving it alone on 8 of 10 crypto assets and 42 of 70 equities — worse
than a coin flip — and the "optimal" setting was unstable more than half the time. **The
untuned rule is the trustworthy one.** This is the strongest available argument for the
fixed defaults, and the reason not to sweep them.

It also independently confirms the standing rule in memory `win_rate_trap` from a new
direction: the thing that felt like diligence (re-optimising on recent data) was the thing
that destroyed the result.

## Result 2 — crypto: the untuned rule beat holding on 8 of 10, out-of-sample

| asset | FIXED | HOLD | | asset | FIXED | HOLD |
|---|---|---|---|---|---|---|
| BTC | **21.78x** | 9.86x | | ATOM | **0.27x** | 0.07x |
| ETH | **13.02x** | 9.82x | | BCH | **0.71x** | 0.60x |
| XLM | **3.43x** | 2.46x | | LINK | 0.76x | **0.96x** |
| ALGO | **3.58x** | 0.31x | | LTC | **0.45x** | 0.39x |
| ETC | **2.07x** | 2.04x | | SOL | 1.55x | **5.03x** |

**8 of 10 beat holding. But only 6 of 10 made money at all** — on ATOM, BCH, LINK and LTC
the rule lost, it simply lost *less* than holding did. Both facts matter: this is
predominantly **downside protection**, which is what every other line of evidence in this
repo already says.

## Result 3 — the basket, which is the actual product

Diversification is the one change ever measured to improve this rule. Equal-weight, daily
rebalance, costs charged on every state switch, **walk-forward test period only**:

| CRYPTO basket (10) | growth | maxDD | Sharpe |
|---|---|---|---|
| **rule** | **1.50x** | **−38.4%** | **0.44** |
| hold | 1.10x | −65.7% | 0.34 |

**Better on all three axes.** This is the single most trustworthy result behind the
indicator — and note it is the *basket*, not any one chart.

| EQUITIES basket (70) | growth | maxDD | Sharpe |
|---|---|---|---|
| rule | 2.55x | **−14.6%** | **1.36** |
| hold | **4.98x** | −33.6% | 1.28 |

On stocks the same test says the opposite: **half the return** for half the drawdown, with
Sharpe essentially tied (1.36 vs 1.28). On equities this is a **risk tool**, not a return
tool — which is precisely how `pine/trend_signal_stocks.pine` is labelled.

## What this does and does not establish

**Does:** the crypto result is not an artifact of parameter fitting — it was produced
without ever seeing the test data, on 10 assets, and the untuned version beat the tuned
one. That is a real increase in trustworthiness over everything measured before it.

**Does not:** make it proven. There is still no multiple-testing correction on the headline
PF figures, the sample spans one crypto cycle, the 10 assets are highly correlated (so the
effective sample is far below 10), and 4 of 10 lost money outright. Walk-forward raises
confidence; it does not clear the bar in `proof_scorecard.py`.

**The honest one-line summary:** on a *basket* of crypto, on *daily* bars, with the
parameters *left alone*, this rule beat buy-and-hold out-of-sample on return, drawdown and
Sharpe. Every qualifier in that sentence is load-bearing.

---
date: 2026-09-06
agent: claude-computer
branch: research/perfect-indicator-search
pr: pending
lane: directional
files: [scripts/win_rate_100_lab.py, scripts/equity_indicator_lab.py, pine/trend_signal_stocks.pine, tests/test_equity_indicator_lab.py, RESEARCH_2026-09-06_perfect_indicator_search.md]
---

# The "100% win rate" ask, built and priced — and a stock indicator that corrected my own result

Owner asked for a perfect indicator, crypto and stocks, 100% win rate, beats fees, "be
revolutionary". A 100% win rate is not achievable for a directional strategy, but saying so
is cheap, so it was **constructed three ways and measured** instead of refused.

## The 100% win rate exists and costs everything

**Never realise a loss** — 100% realised win rate on all six assets by construction. SOL
held ONE position 614 bars (1.7 years), −91.7% at worst, still −51% and open at sample end,
with **19 BUY signals missed** while the capital sat trapped. **Martingale** — 100% win
rate, peak size required 64x the first bet on SOL. **Tiny target / wide stop** — the two
results that answer the request literally:

- BTC/ETH/SOL win rate **0%**: a 0.5% target cannot clear a 0.54% round-trip cost, so every
  "winner" is a loss before direction is even considered. That is what "beats fees" binds on.
- **QQQ posted a 100% win rate, positive expectancy, and beat fees** — turning 1.00x into
  **1.04x over eleven years** while holding QQQ made ~3x. The specification was met exactly
  and produced the worst outcome on the page. That single row is the best answer to the
  question that exists.

Fourth independent reproduction of `win_rate_trap`.

## The stock indicator — and a correction to something I told the owner an hour earlier

I had reported that SPY/QQQ/GLD showed a 60-64% win rate and 3.2-6.8 payoff, "close to
exactly the shape you asked for". **That was three assets and it did not survive.** Across
**70 US tickers** the median win rate is **37%** — the same as crypto — median t=0.98, and
only **6% of assets beat buy-and-hold** (1.56x vs 3.72x median). All 16 cells of the
MA/band grid tested; **0 of 16** had a majority of assets beat B&H. Both sample halves
agree, so it is not regime-dependent. Told the owner plainly rather than letting the
earlier number stand.

**What is real:** at SMA(200)+1%, median drawdown **−24.5% vs buy-and-hold's −43.2%,
smaller on 90% of the 70 assets.** Robust, reproducible, and a RISK effect not a return
effect. So `pine/trend_signal_stocks.pine` ships labelled a drawdown tool, with "It is NOT
an alpha indicator. It will not beat buying and holding." in the header and a live panel
that shows return next to drawdown — showing only the drawdown saving would be selling it
on its good half. SMA(200) chosen because it is the canonical industry filter (decided
decades before this grid existed) AND posted the smallest drawdown; stated in the file so
the choice can be audited rather than trusted.

## Verification — the owner explicitly asked not to be told what he wants to hear

- 15 unit tests, including an equivalence proof that the Pine `ta.barssince` formulation
  matches the plain state machine over 3,000 random bars × 4 band settings — the one place
  the script could silently implement a different rule from the one measured.
- **Two tests failed on first run; both were bugs in my tests, not the code** (a crash
  placed inside the MA warmup, which `replay()` correctly excludes; monotonic ramps giving
  1 trade against a ≥3 filter). Fixed properly, not by weakening assertions, and a test
  added for each behaviour.
- Full suite **3587 passed / 9 failed, all pre-existing** (test_bot_main 4, test_dashboard
  3, test_exchange 1, test_notifications 1).
- **TradingView: compiled, saved, and verified COMPUTING** — not just compiling. Pasted
  source hashed in-browser matched the repo file byte-for-byte (SHA-256 `9d2055a2…f230c9`).
  On live SPY daily the legend read `200 1 SMA 712.15 719.27 705.03`; band math checks
  (×1.01, ×0.99) and an independent Python SMA(200) returned **712.15, matching to the cent**.
- That live check found a real discrepancy: **TradingView charts UNADJUSTED closes**
  (adjusted SMA200 = 709.87). Timing moves ~0.3%, total return does not match unless ADJ is
  on. Documented in the script header and re-synced to the saved TradingView copy (v2) so
  the two do not drift.

**Cross-lane note:** none — new scripts, a new pine file, a new test file, a research doc.
No existing file touched.

**Depends on:** `scripts/trend_signal_rr_research.py` from PR #115 (`win_rate_100_lab.py`
imports its data loader). Merge #115 first.

**Not done / limits:** 70 tickers over one 11-year window with a single major bear; assets
are correlated so effective n is far below 70; median t=0.98. **Nothing here clears the
proof bar and nothing is claimed to.** The drawdown result is the only finding stated as
reliable. No alerts were created on the owner's account and no existing script or chart
indicator was modified — the stocks script went to a NEW layout ("Stocks - Trend Signal
test") because the BTC chart was at its 5-indicator plan limit.

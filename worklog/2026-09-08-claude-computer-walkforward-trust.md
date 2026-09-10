---
date: 2026-09-08
agent: claude-computer
branch: feat/trend-signal-honest-indicator
pr: 113
lane: directional
files: [scripts/walkforward_trust.py, pine/trend_signal_honest.pine, RESEARCH_2026-09-08_walkforward_trust.md]
---

# Walk-forward: the untuned rule beats the tuned one, and the basket beats holding

Owner asked to get as close as possible to a working and **trustworthy** buy/sell
indicator. "Trustworthy" has a specific, testable meaning that had never been checked
here: every prior measurement of this rule chose parameters with the whole history
visible. So I ran the one test that answers "would it have worked in real time".

**Method:** 5-fold walk-forward. Parameters chosen ONLY on data before each test window;
test windows concatenated; nothing revisited. No lookahead — the MA at bar i uses bars <= i,
entries on confirmed closes, and an open position at a window's end is marked to market so
a winning open trade cannot be quietly dropped.

## Tuning this rule is noise

|  | crypto (10) | equities (70) |
|---|---|---|
| ADAPTIVE (re-tunes each fold) beat FIXED | **2/10** | **28/70** |
| best parameter CHANGED between folds | **54%** | **54%** |

Re-tuning lost to leaving it alone on 8 of 10 crypto and 42 of 70 equities — worse than a
coin flip — and the "optimal" setting was unstable more than half the time. Independently
confirms `win_rate_trap` from a new direction: **the thing that felt like diligence was the
thing that destroyed the result.**

## Crypto: 8 of 10 beat holding, out-of-sample

BTC 21.78x vs 9.86x, ETH 13.02x vs 9.82x, ALGO 3.58x vs 0.31x, XLM 3.43x vs 2.46x, ETC
2.07x vs 2.04x, ATOM 0.27x vs 0.07x, BCH 0.71x vs 0.60x, LTC 0.45x vs 0.39x. Lost on LINK
and SOL. **But only 6 of 10 made money at all** — on ATOM/BCH/LINK/LTC it lost, just less
than holding. Predominantly downside protection, consistent with everything else here.

## The basket, which is the actual product

Equal-weight, daily rebalance, cost charged on every state switch, test period only:

```
CRYPTO basket   growth   maxDD  Sharpe      EQUITIES basket  growth   maxDD  Sharpe
  rule           1.50x  -38.4%    0.44        rule            2.55x  -14.6%    1.36
  hold           1.10x  -65.7%    0.34        hold            4.98x  -33.6%    1.28
```

Crypto: better on all three axes. Equities: half the return for half the drawdown with
Sharpe essentially tied — a risk tool, exactly as `trend_signal_stocks.pine` is labelled.

## Pine updated

The header bullet used to read *"No walk-forward, no out-of-sample split, no
multiple-testing correction."* True when written, no longer. It now carries the numbers
above **including the stocks result that points the other way**. Portfolio-panel defaults
switched to the tested crypto basket, 8 slots filled by **longest Coinbase history** — a
performance-blind criterion, chosen deliberately so this is not the best-8-of-10. ALGO and
ATOM both lost money outright and the basket still beat holding; said so in the file.

**Verification:** widened the crypto universe from 3 to 10 assets specifically because a
2-of-3 result was too thin to trust — the wider set strengthened it to 8-of-10 rather than
weakening it, which is the direction that matters. Costs charged on every basket state
switch, not just at the ends.

**Cross-lane note:** none — a new research script plus the pine file this branch owns.

**Not done / limits:** no multiple-testing correction on the headline PF figures; one
crypto cycle; the 10 assets are highly correlated so effective n is far below 10; 4 of 10
lost money outright. Walk-forward raises confidence, it does not clear the proof bar. The
basket arm is NOT wired to any live runner — it is a measurement, and standing one up would
be a new forward arm with its own pre-registration.

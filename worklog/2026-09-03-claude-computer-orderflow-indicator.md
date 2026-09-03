---
date: 2026-09-03
agent: claude-computer
branch: feat/orderflow-indicator
pr: 110
lane: directional          # cross-lane â€” see note below
files: [src/orderflow_indicator.py, tests/test_orderflow_indicator.py]
---

# Offline order-flow indicators â€” a measurement instrument, not a signal

Owner asked for "an order flow indicator". Built one as **pure, vectorised,
stateless functions over historical data**, deliberately not wired into any live
path, emitting no BUY/SELL and applying no thresholds.

## Why measurement and not a trigger

`research/hypothesis_registry.yaml` records `scalper-microstructure-ofi-v2` as
**paper-only** behind a corpse: 228 trades, 0.9% win rate, âˆ’$20.14 net, 73.6% fee
drag. Memory `market_structure_signals_verdict` is blunter â€” intraday order
flow/CVD/VP are dead at the cost wall, and "don't feature-stack; cost was the
problem, not signal quality". Shipping another order-flow *trigger* would
re-propose a hypothesis the ledger has already priced, against ledger rule 1.

But that entry's own deployment gate reads *"requires: positive OOS edge from
non-loosened gates on fresh data"* â€” and that is the gap worth filling.

## The finding that shaped the design

The repo already has substantial order-flow machinery: `OrderFlowImbalance`
(v1), `OFICalculatorV2`, `CVDTracker`/`TickCVDTracker`, `obi_from_book`. Every
one of them is **stateful and live-feed-shaped** â€” takes an exchange handle or
streaming updates, holds internal deques, returns the latest scalar. None can run
over a historical frame.

And there is **no recorded tape or book snapshot anywhere under `data/`** (I
checked: the largest files are backtest decision logs and research caches; no
tick or book data at all). So the order-flow numbers the live bot computes are
built in memory and thrown away.

That is the concrete reason this hypothesis can never clear its own gate: **the
OOS evidence it demands has never been recordable.** This module is the half of
that fixable without new infrastructure â€” given data, it scores it. A recorder is
the other half and is deliberately NOT in this PR (it touches live paths, needs
its own review, and would be a cross-lane change with real runtime cost).

## What's in it

- `ofi_from_books` â€” canonical **Cont, Kukanov & Stoikov (2014)** OFI over
  consecutive best-quote snapshots. Not previously implemented anywhere in the
  repo; `ofi_v2` is a bespoke stateful variant, not this. Additive by
  construction, so windowed OFI is just a sum.
- `tick_rule_side` / `signed_volume_from_ticks` â€” Lee-Ready tick-rule side
  inference, using the venue's real aggressor flag when present and the tick rule
  only for the gaps rather than discarding either source.
- `bar_signed_volume` â€” close-location-value **proxy** from OHLCV, explicitly
  labelled a proxy in the docstring (CLAUDE.md records that candle-CVD was
  replaced by tick CVD because the proxy was inadequate; it's here because bars
  are the only history that currently exists).
- `cumulative_volume_delta`, `book_imbalance`, `rolling_zscore`,
  `cvd_price_divergence`.

Edge cases are handled toward "no information" rather than toward a number that
poisons downstream sums: doji bars score 0 not âˆž, an empty book scores 0 not NaN,
a zero-variance z-score window scores 0 not Â±inf, leading trades with no
established side score 0 rather than a fabricated side, and OFI's first element
is 0 because there is no prior snapshot to difference against.

## Verification

48 new tests, all passing. Full suite **3620 passed, 9 failed** â€” the same 9
pre-existing failures present at HEAD (see the other worklog entry today).

The OFI tests are the ones that matter: the CKS sign conventions are easy to get
backwards, and a sign error there is invisible downstream â€” it just makes the
indicator quietly anti-predictive. So each of the six price cases (bid up / down /
flat, ask up / down / flat) is pinned with a **hand-computed** expected value read
straight off the published definition, not off this implementation. I also
verified a 5-snapshot sequence by hand outside pytest.

**Cross-lane note:** new file, but order-flow sits in the **directional** lane
(owner: dispatch); I am claude-computer / brain-risk-observability. It adds no
imports to any existing module, changes no behaviour, and cannot move any
in-flight forward test â€” it is inert until something calls it. Same declaration
as today's `src/indicators.py` PR.

**Explicitly not claimed:** that any of this predicts anything. The repo's record
says it doesn't, at this cost level. This is the instrument, not a result.

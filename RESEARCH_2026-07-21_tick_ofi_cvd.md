# RESEARCH 2026-07-21 — Tick-level OFI/CVD backtest + "balls indicator" note

## The "balls indicator"

Confirmed with the owner: Bookmap-style order-flow bubbles, sized by executed
trade/order volume at each price level. This is a VISUALIZATION of the same
signed-trade-volume data our OFI/CVD already computes numerically — it shows
a human where big prints landed, it does not measure anything new. It cannot
add edge on its own; whatever edge exists is in the underlying order-flow
data, which is exactly what this backtest tests. Verdict below applies
whether you watch it as a chart or compute it as a number.

## Method

Real Kraken tick-by-tick trades (not synthetic bars) via `fetch_ticks.py`,
3 days each, BTC/USD (149,471 ticks) and ETH/USD (61,398 ticks),
2026-07-18→21. Built into a continuous 1-second grid (forward-filled through
quiet gaps so "forward N seconds" is always defined). OFI = rolling sum of
signed trade volume (buy trades positive, sell negative) over a window;
z-scored against its own trailing 1-hour mean/std. CVD is the running total
of the same signed volume.

**Step 1 — pure predictiveness** (no cost, single best cell): 300s OFI
window → 900s forward return, entries where |z|>1.5: mean +1.46bps,
t=16.15, n=33,330 overlapping windows. This is real, not noise — order flow
imbalance DOES predict short-horizon direction on this data, consistent with
market microstructure theory.

**Step 2 — actual tradeable expectancy** (non-overlapping trades, i.e. you
can't hold the same signal twice): the strongest single-day cell (BTC, 300s
window, z>2.5, 900s hold) gave 67 trades, gross +5.65bps/trade, win rate
10.4%. Net of Kraken TAKER round-trip (26bps): −20.35bps/trade. Net of
MAKER round-trip (16bps, i.e. assuming you always get a maker fill for
free): still −10.35bps/trade.

**Step 3 — full grid sweep**, both symbols, OFI windows {30,60,300,600}s ×
holds {60,300,900,1800}s × entry z {1.5,2.0,2.5} = 96 cells: **zero cells
net positive against even the maker cost model.** Best case across the
entire grid (BTC, 300s window, z>2.5, 30-min hold): 6.14bps gross vs
15.96bps maker cost = −9.86bps net. ETH corroborates independently — its
best cell is also ~−10.5bps net.

## Verdict

Order flow imbalance carries real, statistically robust short-horizon
predictive signal (t>16 on the gross number) — but the signal itself is
roughly 1-6bps, and even a PERFECT maker fill (free entry, no adverse
selection) costs 16bps round-trip. The edge is 2-3x too small to clear cost
at any window/horizon/threshold combination tested. This is the same
conclusion the shelved directional engine already reached in production
(t=-8.82 on 229 live trades) — this backtest independently confirms it from
raw tick data rather than the live paper record, closing the loop on "is
there something we missed by not testing ticks directly." There isn't.

## What this means for the maker-only microstructure re-test (CLAUDE.md)

The re-test's premise was that maker fills (collecting the spread instead of
paying it) might be the difference. This backtest suggests otherwise: even
in the best case, maker cost alone (16bps) is ~3x the raw OFI edge (5-6bps).
For the maker-only re-test to succeed, it would need the ACT of resting
(rather than a hypothetical zero-cost fill) to add several bps of edge on
top of raw OFI — e.g. by only getting filled on favorable adverse-selection
draws — which is a different and unproven hypothesis than "OFI predicts
price." Recommend treating the 90-day maker re-test's bar as genuinely
hard to clear, not a formality.

## Honest caveats

- 3-day sample, one volatility regime — a longer/multi-regime tick sample
  (weeks) could shift the numbers, though the gap (2-3x) is large enough
  that regime alone is unlikely to close it.
- 1-second aggregation loses true tick-by-tick timing; a production system
  reacting within milliseconds might capture a few more bps of the initial
  move — plausible upside, not tested here.
- Did not test CVD divergence (price vs CVD disagreement) as a separate
  signal from raw OFI level — a different hypothesis, cheap to test if
  wanted.

## Disposition

Do not pursue tick-level OFI/CVD as a standalone entry signal at this size
and cost structure. If revisiting, the CVD-divergence variant and a longer
multi-week sample are the two cheapest next checks; absent a specific reason
to think either changes the 2-3x gap, this line of research is closed.

# RESEARCH 2026-07-17 — Liquidation-cascade strategies (Moon Dev stream idea)

Owner saw Moon Dev's stream showing "Regime-Switch Cascade" (+1,312%, Sharpe
4.53) and "Swing-Proximity Cascade" (+1,100%, Sharpe 4.69) — liquidation-wave
strategies found by an agent search on his June 10th ideas. Question: do these
shapes survive OUR data and OUR cost model?

Method: `backtest_liq_cascade.py`. 1h OKX bars, BTC/ETH/SOL, 2021-07→2026-07
(43,800 bars each). No historical liquidation feed exists to us, so cascades
are PROXIED: |return z| > 2.5 (vs rolling 200-bar sigma) AND volume z > 2.0
(~1.4% of bars, ~1,900 events). Costs = lev_perp production: 0.15% notional
round-trip + 10% APY funding drag, $1k notional/trade, entries next-bar open.

## Result 1: the stream's strategies as described DIE here

With the v2-style ATR(14)×2 chandelier trail, over 5 years on $1k/trade:

| Variant | Trades | WR | Net PnL |
|---|---|---|---|
| with_cascade (baseline) | 1474 | 26% | −$9,216 |
| fade_cascade (baseline) | 1498 | 23% | −$13,369 |
| regime_switch (stream #1) | 1493 | 23% | −$12,207 |
| swing_prox (stream #2) | 1169 | 25% | −$7,381 |

Cause: it is the EXIT, not the signal. A 2×ATR trail right after a cascade is
placed inside the post-cascade whipsaw zone — ~75% of trades stop out within
hours at roughly −0.5% each. Any tight-stop intraday design in cascade
conditions is a whipsaw harvester for the exchange.

## Result 2: raw cascade CONTINUATION drift is real (time exits, gross)

Forward drift in the cascade's direction, all ~1,877 events:
1h −0.03%, 4h −0.03%, 12h +0.21% (t=2.2), 24h +0.33% (t=2.6), 48h +0.58%
(t=3.4). Liquidation waves DO snowball — but on the 12–48h horizon, not
intraday, and only if you let them breathe (no trail).

## Result 3: the stretch filter works BACKWARDS from the stream's claim

Moon Dev's regime-switch rides cascades only when price is NOT yet stretched
and fades stretched ones. On our data at 24h: not-stretched cascades −0.19%
(t=−0.8, dead); ALREADY-STRETCHED (|close−SMA50| ≥ 3×ATR) +0.59% (t=+3.7).
Momentum begets momentum; the dip-buy half of his bot is the weak half.

Best surviving shape — **stretched-cascade continuation, 48h time exit**, net
of full costs: 681 trades, 50.5% WR, **+$4,572 on $1k notional/trade
(+0.67%/trade), maxDD −$939**, positive every year, both sides positive.
Day-clustered t = +2.18 (438 trade-days).

## Honest caveats (why this is NOT deployable as proven)

1. **Selection bias**: the stretch split and 48h horizon were found on this
   same sample after the primary hypothesis (the stream's shapes) failed.
   t=2.18 does not clear the repo's family-wise bar for the number of cells
   examined. This is a HYPOTHESIS GENERATOR result, not proof.
2. **Edge decay**: PnL by year: 2021 +$745, 2022 +$1,090, 2023 +$2,048,
   2024 +$610, **2025 +$48, 2026H1 +$30**. The edge was a 2021–23 phenomenon
   (SOL contributed $2.6k of $4.6k) and is ~flat for the last 18 months —
   consistent with markets pricing in cascade-momentum as liq tooling
   (Coinglass etc.) went mainstream.
3. **Proxy risk**: return+volume z-scores approximate liquidation waves;
   a real liq feed (e.g. Binance forceOrder websocket, recordable forward)
   may select different, possibly better, events.
4. Consistent with standing conclusions: intraday taker dies to costs
   (Result 1); the only edges that survive are multi-day and momentum-shaped
   (Result 2/3 rhymes with TSMOM-7-14d).

## Disposition

- Stream numbers (+1,312%) are in-sample agent-search survivors on someone
  else's data; on ours the same shapes lose money. Do not chase.
- IF pursuing: paper arm `cascade_cont_paper` (stretched-cascade continuation,
  48h hold, $333 margin @3x, same proof scorecard bar as every arm) + start
  RECORDING the real Binance forceOrder liq feed now so a genuine liq-based
  version can be tested later. Given the 2025–26 decay, expectation should be
  that the forward test reads ~flat.

# Pine Script ports of the bot's strategies

TradingView versions of the strategy arms that live in this repo, so they can be
looked at on a chart instead of only in a state file.

**These are inspection tools, not a second trading system.** The forward paper
runners in the repo root remain the authority: they hold the real ledgers, and
`proof_scorecard.py` judges them on the pre-registered bar (n≥30, expectancy>0,
family-wise clustered t, DSR>0.95). Nothing on a TradingView chart changes a
verdict.

## The files

| File | Ports | Kind | Honest status |
|---|---|---|---|
| `tsmom_sma200_band.pine` | `tsmom_paper.py` | strategy | Candidate. Low-turnover long/cash, SMA200 + 2% band. |
| `conf_trend_momo.pine` | `conf_paper.py`, `btc_trend_paper.py` | strategy | Candidate. Best drawdown-adjusted spot bot in the long-only tournament. |
| `kelly_trend_compounding.pine` | `kelly_trend_paper.py` | strategy | A/B of **sizing** against `conf_trend_momo` — same entries, same dates. |
| `trend_ensemble_2of3.pine` | `trend_ensemble_paper.py` | strategy | Candidate. 2-of-3 vote; chosen over a better-backtesting single SMA on purpose. |
| `tsmom_ls_sma50.pine` | `tsmom_ls_paper.py` | strategy | **Short leg looks bad.** Later research had it losing in every config. |
| `regime_intraday.pine` | `regime_arm.py` | strategy | Promising, not proven. The ATR cost gate is the point — don't loosen it. |
| `swing_4h_majors.pine` | `src/swing_strategy.py` | strategy | **Killed by the 1-year backtest** (−$0.47/trade, t=−0.98). Ported for inspection. |
| `lev_perp.pine` | `lev_perp_paper.py` (+v2, +ema) | strategy | Leverage is settled-dangerous. Runs all three variants via two dropdowns. |
| `pairs_market_neutral.pine` | `pairs_paper.py` | indicator | Pinned to the runner's 3 Kraken pairs. Cost wall is brutal; cointegration pairs broke OOS in the 320-strategy search. |
| `rebalance_allocation.pine` | `rebalance_paper.py` | indicator | The **one** positive prediction-free result. Downside protection, not alpha. |
| `trend_signal_honest.pine` | — | indicator | Pre-existing. The SMA100+2% BUY/SELL viewer. |
| `micro_cvd_cost_wall.pine` | — | indicator | Pre-existing. CVD + cost-wall viewer. |

## What could NOT be ported, and why

Three arms are not expressible in Pine. This is a real limitation, not an
oversight — if you go looking for them, they are absent on purpose.

- **`micro_paper.py` — maker-only microstructure.** Needs live order-book depth,
  the raw trade tape, and a post-only fill model where a resting bid fills *only*
  when the tape trades through it (and otherwise **does not trade at all**). Pine
  has no order book and no concept of a non-fill. A Pine version would silently
  assume every order fills, which converts a maker strategy into a taker one and
  measures something else entirely. `micro_cvd_cost_wall.pine` shows the *inputs*
  on a chart; it cannot simulate the arm.
- **`brain_paper.py` — the Claude discretionary arm.** It asks an LLM for a
  decision each day. There is no API call from Pine.
- **The funding-arb arms** (`arbitrage/funding_arb_paper.py`). They need
  per-exchange perpetual funding rates and a delta-neutral spot+perp position.
  TradingView does not carry the funding feed, and Pine cannot hold two
  instruments at once.

## Which chart to put each one on

Every runner in this repo prices itself off **Kraken spot** (`api.kraken.com/0/public/OHLC`).
That includes the perp arms — they *simulate* leverage and funding, but the price
series underneath is spot. So a Kraken Futures / perp chart is the **wrong** chart
for `lev_perp.pine`: different series, different bar times, and a basis the runner
never sees.

| Script | Chart | Timeframe |
|---|---|---|
| `lev_perp.pine` | `KRAKEN:XBTUSD`, `KRAKEN:ETHUSD`, `KRAKEN:SOLUSD` (one at a time) | Daily |
| `pairs_market_neutral.pine` | any — it fetches both legs by name | set by its own input (60m) |
| `tsmom_sma200_band`, `conf_trend_momo`, `trend_ensemble_2of3`, `kelly_trend_compounding`, `tsmom_ls_sma50` | `KRAKEN:XBTUSD` / `ETHUSD` / `SOLUSD` | Daily |
| `swing_4h_majors` | the 6 majors: `XBTUSD ETHUSD SOLUSD LTCUSD BCHUSD XRPUSD` | 4h |
| `regime_intraday` | `KRAKEN:XBTUSD` / `ETHUSD` / `SOLUSD` | 1h or 4h |
| `rebalance_allocation` | any — it fetches all 11 by name | Daily |

`lev_perp.pine` carries a guard that draws an orange banner when the chart's
venue, timeframe or symbol is off-spec. `pairs_market_neutral.pine` needs no
guard: the pair is a dropdown and both legs are fetched by name, so it reads the
same series regardless of what chart it sits on.

**Pair orientation is not cosmetic.** The runner builds its three pairs with
`sorted(combinations(...))`, which fixes leg A as the alphabetically-first coin:
`(BTC,ETH)`, `(BTC,SOL)`, `(ETH,SOL)`. The spread is `ln(P_a) − ln(P_b)`, so
swapping the legs flips the sign of z and inverts every long/short instruction.
That is why the pair is a dropdown rather than two symbol boxes.

**The 8-coin note on `lev_perp`.** `RESEARCH_2026-07-26_lev_perp_v1_frequency.md`
found that widening v1's universe from 3 to 8 coins — same entry signal, same
filters, nothing else changed — moved DSR from 0.538 to 0.887, the largest single
improvement in this project's search history. The extra five are `ADAUSD XRPUSD
DOTUSD AVAXUSD LINKUSD`. The finding is about **breadth (more independent
setups)**, not about a better rule, so a single chart cannot show it; it only
appears across the whole set. In the runner these are reachable only via an
explicit `LEV_PERP_SYMBOLS` override, which is why the script's Universe input
defaults to the 3-coin production set.

## Things to know before you read a Strategy Tester result

1. **The tester's number is in-sample on whatever window you loaded.** Every one
   of these rules was already measured honestly in this repo; the chart is for
   seeing *behaviour*, not for re-deciding whether the edge exists. If you tune
   the inputs until the curve looks good you have overfit the visible window,
   which is precisely the failure the proof apparatus exists to catch.
2. **Costs are set as commission per side**, halved from the runner's round-trip
   figure (0.54% round trip → 0.27% per side). Don't zero it. `trade_forensics`
   showed cost is 18.6× the move being chased — cost *is* the problem.
3. **TradingView cannot charge a time-based funding drag.** For the perp arms
   (`tsmom_ls_sma50`, `lev_perp`) the tester's Net Profit is therefore **too
   generous**. Both scripts accrue the drag themselves and report *equity net of
   funding* in the on-chart panel. Judge that figure.
4. **A high win rate is a warning sign here, not a goal.** Standing rule
   (`win_rate_trap`): any change that raises win rate while increasing trade
   count is destroying expectancy. It has been proven twice. Judge profit factor
   and expectancy.
5. **Shorts are hypothetical.** A US Kraken-spot account cannot short. Every
   short bar in `tsmom_ls_sma50`, `regime_intraday` and `lev_perp` is paper until
   Kraken US perps (Bitnomial) are integrated.

## Parity notes

The ports are faithful to the runners' defaults, with two known gaps, both
documented in the scripts themselves:

- **`lev_perp.pine` has no correlation cap.** The runner halves margin when a
  second same-direction major is already open. One chart cannot see the other
  symbols, so this script is slightly *more* aggressive than the runner whenever
  BTC/ETH/SOL agree.
- **`swing_4h_majors.pine` has no cadence caps.** `swing_paper.py` limits entries
  per day/night window and ranks by conviction when more setups qualify than the
  budget allows. On a single-symbol chart there is nothing to rank.

Multi-symbol arms (tsmom, conf, tsmom_ls, regime, swing) split their book across
several coins; one chart is one coin. Set the position size accordingly if you
want the runner's per-symbol allocation rather than a whole-book bet.

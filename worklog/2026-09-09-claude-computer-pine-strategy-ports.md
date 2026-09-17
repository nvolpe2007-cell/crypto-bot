# Every portable strategy arm, ported to Pine — and the three that cannot be

**Agent:** claude-computer · **Branch:** `feat/pine-strategy-ports`
**Lane:** directional (docs//pine only — no runner or `src/` file touched)

## What was asked

Hold off on the VPS and the bot itself; make all the strategies available as Pine
Script for TradingView.

## What landed

Ten new files in `pine/`, plus a `README.md` index. Eight are `strategy()` scripts
that backtest; two are `indicator()` scripts, for a reason given below.

| File | Ports |
|---|---|
| `tsmom_sma200_band.pine` | `tsmom_paper.py` |
| `conf_trend_momo.pine` | `conf_paper.py` **and** `btc_trend_paper.py` |
| `kelly_trend_compounding.pine` | `kelly_trend_paper.py` |
| `trend_ensemble_2of3.pine` | `trend_ensemble_paper.py` |
| `tsmom_ls_sma50.pine` | `tsmom_ls_paper.py` |
| `regime_intraday.pine` | `regime_arm.py` |
| `swing_4h_majors.pine` | `src/swing_strategy.py` + `swing_paper.py` |
| `lev_perp.pine` | `lev_perp_paper.py`, `_v2_`, `_ema_` (two dropdowns) |
| `pairs_market_neutral.pine` | `pairs_paper.py` |
| `rebalance_allocation.pine` | `rebalance_paper.py` |

Two consolidations are worth noting because they are facts about the runners, not
shortcuts. `conf_paper` and `btc_trend_paper` ride the *identical* signal and
differ only in universe and book size — on a single chart that is one script. The
three `lev_perp` variants share entry filters, leverage, liquidation and costs
byte-for-byte and differ only in direction signal and exit engine, so they are two
dropdowns rather than three files.

## Three arms are not expressible in Pine, and I did not fake them

- **`micro_paper.py`** needs order-book depth, the raw tape, and a post-only fill
  model whose defining property is that it **often does not trade at all**. Pine
  has no book and no concept of a non-fill; a Pine version would assume every
  order fills, which silently converts a maker strategy into a taker one. Given
  yesterday's finding that this arm's gate already fires on 16% of ticks, a port
  that also removed the non-fill would be measuring nothing at all.
- **`brain_paper.py`** asks an LLM for a decision. No API calls from Pine.
- **The funding-arb arms** need per-exchange funding rates and a two-instrument
  delta-neutral position. TradingView carries neither.

`pairs_market_neutral` and `rebalance_allocation` are indicators for the same
class of reason: a Pine strategy can only send orders for the one instrument on
the chart, and those two hold two and eleven respectively. Rather than backtest a
one-legged imitation of a dollar-neutral trade, they compute the exact signal (or
the exact portfolio equity curve) and plot it.

## Two fidelity gaps, recorded rather than papered over

- **`lev_perp.pine` has no correlation cap.** The runner halves margin when a
  second same-direction major is already open. One chart cannot see the other
  symbols, so the port is slightly MORE aggressive than the runner exactly when
  BTC/ETH/SOL agree.
- **`swing_4h_majors.pine` has no cadence caps.** `swing_paper.py` limits entries
  per day/night window and conviction-ranks the surplus. Nothing to rank on one
  symbol.

## The thing most likely to cause harm later, so it is in every header

A Strategy Tester result is in-sample on whatever window was loaded. These rules
were already measured honestly in-repo, and several are already dead: the swing
arm was killed by the one-year backtest (−$0.47/trade, t = −0.98, LTC 0-for-13)
after a 120-day window flattered it, and the L/S short leg lost in every
configuration of the later research. Both ports say so in their headers, above the
code, rather than in a footnote. The perp scripts additionally accrue the funding
drag themselves and report equity net of it, because TradingView has no
time-based carry cost and its Net Profit for those arms is therefore too generous.

Costs are set as commission per side (0.54% round trip → 0.27%/side) in every
script. The `win_rate_trap` standing rule is quoted in the README.

## Verification

Pine cannot be compiled locally — these need pasting into TradingView's editor to
confirm they build, which is the obvious next step and has NOT been done. I
reviewed for the API errors that survive a read: `strategy.entry` has no
`qty_percent` (fixed in two files, now sizes via explicit `qty`), `ta.dmi` returns
a tuple (fixed), comma-separated declarations are invalid (fixed), and
`strategy.close_all()` fills at the bar close rather than at the stop price —
`lev_perp` now issues real `strategy.exit` stop/limit orders so stopped-out trades
are not flattered.

No Python changed, so the suite is untouched by this branch.

Related: `swing_one_year_backtest_negative` · `ls_momentum_short_leg_loses` ·
`rebalance_premium_verdict` · `win_rate_trap` · `doubling_in_a_month_verdict`

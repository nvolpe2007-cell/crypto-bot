# pine/ — TradingView indicators

Pine Script versions of the order-flow work. These are **chart-side measurement
tools**, not part of the bot: nothing here executes, and nothing here is imported by
any Python module.

## `orderflow_regime.pine`

Volume delta, CVD, ΔOI, and the flow×OI regime classifier, plus a CVD/price
divergence marker and a forced-flow exhaustion detector.

### The porting boundary — read this first

`src/orderflow_indicator.py` and this script are **not** the same feature set, because
TradingView does not give Pine an order book. There is no bid/ask depth, no L2, no
quote stream.

| Construct | Python | Pine | Why |
|---|---|---|---|
| CKS OFI (`ofi_from_books`) | ✅ | ❌ | needs best-quote prices *and sizes* per snapshot |
| Multi-level OFI | ✅ | ❌ | needs depth to N levels |
| Book imbalance | ✅ | ❌ | needs resting bid/ask size |
| Volume delta / CVD | ✅ | ✅ | bar and intrabar data suffice |
| Rolling z-score | ✅ | ✅ | |
| ΔOI % | ✅ | ✅ | TradingView publishes OI for most perps |
| **Flow × OI regime** | ✅ | ✅ | the valuable half, and it ports intact |
| CVD/price divergence | ✅ | ✅ | |
| Liquidation exhaustion | ✅ | ✅ | |

There is no workaround for the ❌ rows. If you want OFI on a chart you need a
platform with depth-of-market (Bookmap, Sierra Chart, Quantower), or the recorder
described in `RESEARCH_2026-09-03_orderflow_capture_schema.md`.

### Install

1. TradingView → **Pine Editor** → paste the contents of `orderflow_regime.pine`
2. **Save**, then **Add to chart**
3. Use a symbol that publishes open interest — crypto **perpetuals** (e.g. a
   `…USDT.P` ticker). On spot symbols OI is absent, the regime row reads `no OI`,
   and everything else still works.

### Settings that matter

- **Intrabar timeframe** must be *lower* than the chart timeframe. 5m under a 1h
  chart is a sensible start. Free plans cap intrabar history; if the request comes
  back empty the script silently falls back to the close-location proxy and the
  status table's `source` row turns amber to tell you.
- **Min |ΔOI| %** (default 0.5) is the noise floor. Below it, OI is moving too
  little to distinguish opening from closing, so the bar is labelled `churn`
  rather than forced into a regime. Raise it on noisy low-liquidity symbols.
- **OI symbol override** — auto-detection appends `_OI` to the chart's ticker,
  which is TradingView's usual convention. If the regime row says `no OI` on a
  symbol you know has it, set the override manually.

### Reading it

The two **forced** regimes are shaded loudly on purpose. They are the states where
the naive reading of the flow is backwards: heavy taker selling with OI *falling*
is positions being closed, not new conviction arriving. Everything else is shaded
faintly or not at all.

The amber diamond is exhaustion — forced flow decaying while price has stopped
making new extremes.

## `orderflow_regime_5m.pine`

The same flow×OI regime edge as `orderflow_regime_strategy.pine`, retuned for
running **directly on a 5-minute chart** rather than as an intrabar feed under a
higher one. It is deliberately *not* a scalper: a raised delta-z threshold
(1.25 vs 1.0), a wide ATR stop, and an 8-hour time exit keep it selective and
slow, because this repo has already measured what a fast, tight-target 5-minute
system does here — the microstructure scalper took 228 trades that way for a
0.9% win rate at 73.6% fee drag, and the standalone tick-OFI/CVD hypothesis is
filed `killed`. If you tighten this strategy's exits or lower its threshold to
trade more often, you are recreating that result, not improving on it.

Same install steps and same open-interest requirement as above. Same honest
framing: a hypothesis you can see on a 5m chart, not a validated edge.

**Status: compiled and run on TradingView** (BTCUSDT.P, Binance, 5m). Also carries
an entry-timing pass: `confirmBars` (require the signal to persist N bars — tested
at 2 on BTCUSDT.P and it filtered out every signal in a 6-week window, a real
finding about how noisy this OI source is bar-to-bar, not a bug) and an optional
breakout-confirmation filter (tested: cut trades 26→22, raised win rate
15.4%→18.2%, cut max drawdown ~16%, left total PnL flat — a real but modest
timing improvement, not a fix for the underlying edge question).

### What it is not

Not a signal, and not a claim that any of this is profitable. See the header
comment in the script: the originating repo's measured record on buying weakness
is uniformly negative, and the reason the regime split is worth *looking at*
anyway is that none of those tests had open interest to separate forced flow from
conviction flow. That separation is a hypothesis, not a result.

## `orderflow_universal.pine`

The two scripts above are **crypto-perpetual only** — their core edge is the
flow×open-interest regime classifier, and open interest in that sense doesn't
exist for stocks or crypto spot. This script drops OI entirely and rebuilds the
signal from what every market publishes: price and volume. It runs on any symbol
with a volume feed — crypto spot, crypto perps, or stocks/ETFs — on any timeframe.

This is **not** the OI version with a broader ticker whitelist. Removing OI
removes the layer that told you *why* volume moved (fresh conviction vs. forced
liquidation vs. covering); this script can only see *that* volume moved. A trend
filter (price vs. its own moving average) stands in as the second confirming leg,
which is weaker and more generic than the regime classifier it replaces. Treat it
as a separate, weaker hypothesis, not a portable version of the stronger one.

**Two modes**: Trend (buy volume bursts that agree with the prevailing trend —
momentum) and Reversal (fade a decaying selling burst once price stops making new
lows — capitulation-fade, no OI needed to call it "forced"). Same entry-timing
tools as `orderflow_regime_5m.pine` (`confirmBars`, breakout confirmation).

Commission defaults to 0.1%/side as a rough crypto/stock middle ground — **set it
to what you actually pay** before trusting a backtest number; a stock account with
zero commission still pays the bid/ask spread via the slippage input.

**Status: compiled and run on TradingView on both asset classes**, 5m chart,
default settings (Trend mode):
- `BTCUSDT.P` (Binance perp): 41 trades, −$232.62 (−23.3%), 24.9% win rate,
  $236.84 max drawdown.
- `AAPL` (NASDAQ stock): ~160 trades, −$206.87 (−20.7%), 36.3% win rate,
  $225.34 max drawdown, over the shorter intraday history TradingView retains
  for equities.

Both numbers are what you'd expect from an unfiltered, unproven hypothesis at
5m speed with real costs on — this confirms the script computes correctly and
produces signals on both asset classes, it is **not** a claim that either
result is good. No forward or out-of-sample test — this is a fresh hypothesis
with no track record, weaker than the OI version by construction, not stronger.

### Win rate vs. edge — a take-profit input, and why it's not the fix it looks like

Added `useTP`/`tpMult` (a take-profit at a smaller ATR multiple than the stop) to
directly test whether a high win rate is reachable. It is: Reversal mode,
`tpMult=1.5` against the 3.0 stop (a 1:2 reward:risk), full available history,
1h chart:

| Symbol | Win rate | Trades | Profit factor | Equity |
|---|---|---|---|---|
| `AAPL` (NASDAQ) | 69.16% | 856 | 0.868 | −57.1% |
| `BTCUSDT.P` (Binance) | 64.14% | 909 | 0.638 | −91.4% |

Both clear 60-70%. **Neither is profitable.** A target much smaller than the
stop mechanically produces many small wins and a few large losses — the
win-rate number climbs while expectancy falls, because the rare losses are
large enough to erase a lot of small wins. This is the repo's "costs dominate,
a high win rate isn't an edge" lesson recurring in a new shape: on the OI
version the failure mode was fees eating a tiny edge; here the failure mode is
tail risk. `useTP` defaults to **off** in the file — turning it on is a
deliberate A/B of this exact tradeoff, not a fix, and judging it by win rate
alone (rather than profit factor / expectancy) is the mistake this table exists
to prevent.

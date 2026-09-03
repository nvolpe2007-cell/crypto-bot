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

### Status: untested on TradingView

Written against the Pine v6 reference but **not compiled or run on a chart** — that
can't be done from here. If it throws on paste, the likely culprits, in order:

1. the `_OI` symbol convention for your exchange (use the override input)
2. an intrabar timeframe not lower than the chart timeframe
3. a v6/v5 difference — the script deliberately avoids v6-only constructs, so
   changing `//@version=6` to `5` should work

Tell me the error text and I'll fix it.

### What it is not

Not a signal, and not a claim that any of this is profitable. See the header
comment in the script: the originating repo's measured record on buying weakness
is uniformly negative, and the reason the regime split is worth *looking at*
anyway is that none of those tests had open interest to separate forced flow from
conviction flow. That separation is a hypothesis, not a result.

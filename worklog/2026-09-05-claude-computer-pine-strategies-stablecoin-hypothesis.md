---
date: 2026-09-05
agent: claude-computer
branch: feat/orderflow-indicator
pr: 110
lane: directional
files: [pine/orderflow_regime_5m.pine, pine/orderflow_regime_strategy.pine, pine/orderflow_universal.pine, pine/README.md, scripts/stablecoin_basis_research.py, scripts/stablecoin_basis_crisis_check.py]
---

# Pine buy/sell strategies (crypto-perp + universal) + a new stress-signal hypothesis

Two unrelated pieces of work landed in the same session.

## 1. Pine Script strategies (TradingView, not part of the live bot)

`pine/orderflow_regime_5m.pine` — the existing flow×OI regime edge retuned to run
directly on a 5-minute chart, with an entry-timing pass added: `confirmBars`
(persistence) and an optional breakout-confirmation filter. Compiled and run on
TradingView, BTCUSDT.P 5m: confirmBars=2 filtered out every signal in a 6-week
window (the OI-regime label is too noisy bar-to-bar to survive persistence at
this size — a real finding, defaults left at confirmBars=1). Breakout confirm cut
trades 26→22, raised win rate 15.4%→18.2%, cut max drawdown ~16%, left PnL flat.

`pine/orderflow_universal.pine` — new script, drops the open-interest dependency
so it runs on stocks and crypto spot, not just crypto perps (OI doesn't exist for
either). Compiled and run on both BTCUSDT.P and AAPL.

Owner then asked for >60-70% win rate specifically. Added `useTP`/`tpMult` (target
smaller than the stop). Tested on TradingView, full available history, 1h chart,
Reversal mode, tpMult=1.5 vs a 3.0 stop: **AAPL 69.16% win rate (856 trades),
BTCUSDT.P 64.14% (909 trades)** — both clear the target. **Neither is profitable**
(profit factor 0.868 and 0.638; BTC arm draws down 91% of equity). Set as the new
defaults on request, with the profit-factor caveat written into the script header
and every relevant tooltip so opening the settings can't miss it. This is the same
"high win rate ≠ edge" lesson the rest of this repo has already learned, in a new
shape: bought with tail risk instead of with transaction costs.

## 2. New hypothesis: stablecoin peg-basis as a stress signal

Owner asked for a strategy that doesn't exist anywhere in the corpus yet. Proposed
using stablecoin cross-basis (USDC/USDT deviating from par) as a liquidity-stress
signal — the crypto analogue of a TED spread — distinct from every existing
`funding-arb-*`/pairs/momentum entry. Pre-registered in the vault
(`Notes/Hypotheses/stablecoin-basis-stress-signal.md`, `registry: false`, not yet
in `hypothesis_registry.yaml`).

Then actually measured it, twice:

- **Calm-regime pass** (`scripts/stablecoin_basis_research.py`, CoinGecko free
  tier, ~90d hourly / ~364d daily): no signal. Deviations were noise-level
  (~3.5bps stdev), correlation to BTC forward returns was contemporaneous rather
  than leading, and the one nominally-significant cell (t=+2.65) was the wrong
  sign and one pick out of an uncorrected ~20-cell grid.
- **Crisis-window pass** (`scripts/stablecoin_basis_crisis_check.py`) — worked
  around CoinGecko's 365-day free-tier wall using Coinbase Exchange's public
  candles (no such limit) and an implied USDT/USD rate from BTC-USD vs BTC-USDT.
  Ran the pre-registered sanity check against three real, pre-named-before-testing
  crises: Terra/UST (2022-05), FTX (2022-11), SVB/USDC (2023-03). **Magnitude
  mechanism confirmed** — peak deviations were 38-82x the calm-regime stdev.
  **Direction was not consistent across the three**: Terra matched the "discount
  = sell" hypothesis (BTC -9.8% at 24h), FTX inverted it (BTC +8.2% at 12h despite
  a discount), SVB inverted it with the opposite polarity (a *premium*, from
  capital fleeing the depegging USDC into USDT, preceding a +20.5% rally at 72h).

n=3 real crises is a qualitative read, not a statistic, and is reported as one.
The conclusion that matters: the original hypothesis's implicit "one consistent
sign" assumption looks wrong as specified — a stablecoin-specific run, an
exchange-contagion panic, and a banking-sector shock are different mechanisms
that don't share a tradeable direction. The corrected next version needs to
classify stress *type* before assigning direction, not just tune the threshold.
Vault note and `MEMORY.md`-linked memory file both updated with this finding;
status left `paper-only`/OPEN, not `sit-out` — this is progress on the
hypothesis, not a kill.

**Cross-lane note:** touches `directional` (this branch's existing lane) plus a
new, self-contained research script under `scripts/`. No changes to any live bot
path, gate, or config — Pine scripts are chart-side only and the stablecoin
scripts are offline research, neither imports into `src/`.

**Not committed:** `scratch_stablecoin/` (raw fetched price-data cache, ~275KB
JSON) — added to `.gitignore` alongside the existing `data/` rule. Re-fetchable
from the scripts in under a minute; not source, not worth tracking.

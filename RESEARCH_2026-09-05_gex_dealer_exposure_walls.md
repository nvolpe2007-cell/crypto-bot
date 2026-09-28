# Dealer gamma-exposure (GEX) walls from the Deribit options chain

**Status:** infrastructure built, first live numbers captured, ZERO forward evidence
yet. This is the pipeline the 2026-06-08 verdict said was missing, not a proven signal.
**Date:** 2026-09-05
**Related:** memory `market_structure_signals_verdict` (parked GEX on 2026-06-08),
`src/gex/`, `gex_paper.py`

---

## Why this exists

2026-06-08's market-structure review called GEX "the only genuinely new idea" of the
five reviewed (GEX/COT/order-flow/volume-profile/CVD), but parked it: no Deribit
options-data pipeline existed, and the idea was unvalidated in crypto at any horizon.
This request revives exactly that parked item — the pipeline now exists
(`src/gex/`), and this document is the pre-registration for what gets measured before
anything is trusted.

## What it computes, and the assumption underneath all of it

**We cannot observe actual dealer positioning.** Deribit's public API gives open
interest and mark IV per strike, not who holds which side. Every number this module
produces rests on the same assumption every public GEX tracker uses:

> Call open interest contributes positive dealer gamma; put open interest contributes
> negative dealer gamma, weighted by Black-Scholes gamma at each strike.

This is a **retail approximation**, not a measurement. It can be wrong for any given
strike and there is no way to verify it from public data alone. See
`src/gex/gex_calculator.py`'s module docstring — it is written to be read before
trusting any single number this pipeline reports.

From that assumption:

1. **Per-strike net GEX** — `(call_OI - put_OI) * gamma_BS(strike) * spot^2 * 0.01`,
   summed by strike across all active expiries.
2. **Zero-gamma flip point** — the hypothetical spot price where net GEX crosses zero,
   found by recomputing Black-Scholes gamma at candidate spot prices (holding
   strikes/OI/IV fixed) and interpolating the sign change. Above it, dealers are
   (assumed) long gamma and dampen moves; below it, short gamma and amplify moves.
3. **Ceiling/floor walls** — the strikes with the largest positive / most negative net
   GEX, a naive "price magnet" proxy.
4. **HV vs IV** — Deribit's own realized-volatility index vs. a ~30-day-tenor ATM IV
   proxy (nearest-to-30-days expiry, closest-to-spot strike — NOT the literal nearest
   expiry, which can be a same-day quote with an incomparably low IV).

## Data source

Two Deribit public endpoints, no API key:
- `get_book_summary_by_currency` — OI + mark IV + underlying price for every active
  option in one call (~800-1000 instruments for BTC). Greeks are computed locally
  (Black-Scholes) rather than fetched per-instrument, which would be ~1000 calls for
  data this one call already implies.
- `get_historical_volatility` — Deribit's own realized-vol index, hourly.

## First live read, 2026-09-05 ~20:08 UTC (BTC)

```
spot: 79,863
regime: positive (dealer-long-gamma, dampening)  net GEX at spot: +261M
zero-gamma flip point: 68,283  (+14.5% below spot)
ceiling wall: 82,000
floor wall:   77,000
ATM IV (~30d): 36.3%   realized vol: 38.0%   spread: -1.7pp
```

**This is one snapshot, not a result.** It says the pipeline runs end-to-end and
produces numbers of the right shape (flip point below spot, walls bracketing spot,
IV/RV roughly in line) — nothing about whether the walls or the regime label predict
anything.

## What has NOT been done, on purpose

- **No forward-return measurement.** Nothing here has been checked against what price
  actually did near a wall, or after crossing the flip point.
- **No backtest.** Deribit's options chain is a live snapshot; a historical options
  chain (needed for a real backtest) is a separate, harder data problem (Deribit does
  not serve historical OI/IV chains this way) — likely requires paying for historical
  options data or building a forward-only record instead, which is what `gex_paper.py`
  does (logs one row per run to `data/gex_log.csv`, appended, never edited).
- **No wiring into `entry_checklist`/`probability_gate`/any live strategy.** Per the
  2026-06-08 verdict's own reasoning (cost dominates, not signal quality) and this
  repo's standing rule to never loosen a gate to force trades, this stays
  signal-logging-only until it has a forward record to judge.
- **No claim about which side of the sign-convention assumption is closer to true.**

## Gate for promotion out of "infrastructure"

Per the ledger's own rule (never edit an existing evidence block — append instead),
future runs of `gex_paper.py` accumulate in `data/gex_log.csv`. This becomes a
judgeable hypothesis (via `proof_scorecard.py`, family-wise corrected, same bar as
every other directional entry) once there is enough forward data to test a concrete
claim — e.g. "price fades within N% of a wall while in the positive-GEX regime, more
often / more profitably than an unconditional baseline." That claim is not yet
pre-registered with fixed parameters (N%, hold period, which wall rank counts) — doing
that precisely, before looking at accumulated data, is the next step, not this one.

## Do not re-propose without new evidence

- Trusting the sign convention as fact. It's a documented assumption.
- Backtesting this "as if" historical options chains were available — they're not,
  without a paid data source.
- Wiring this into live entries before a forward record exists.

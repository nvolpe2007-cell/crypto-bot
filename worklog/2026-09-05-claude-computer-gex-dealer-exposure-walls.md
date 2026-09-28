---
date: 2026-09-05
agent: claude-computer
branch: feat/gex-dealer-exposure-walls
pr: 112
lane: directional
files: [src/gex/black_scholes.py, src/gex/deribit_client.py, src/gex/gex_calculator.py, gex_paper.py, tests/test_gex_calculator.py, research/hypothesis_registry.yaml, RESEARCH_2026-09-05_gex_dealer_exposure_walls.md]
---

# Dealer gamma-exposure (GEX) walls — pipeline built, parked idea revived

Owner asked for an indicator combining historical volatility + option pricing to take
reversals off dealer-exposure ("IV wall") ceiling/floor levels. This exact idea was
evaluated 2026-06-08 and parked as "genuinely new, but needs a Deribit options-data
pipeline we don't have, unvalidated in crypto at daily horizon" (memory
`market_structure_signals_verdict`). Asked clarifying questions first (underlying,
wall-definition method, scope) per explicit instruction — landed on: crypto-only
(BTC first), real dealer-GEX via Black-Scholes gamma weighted by open interest (not a
cruder raw-OI-only approach), and research/signal-logging only — no live wiring.

Built `src/gex/`: a Deribit public-API client (two calls, no auth — full option-chain
book summary + Deribit's own realized-vol index), Black-Scholes gamma computed locally
(saves ~1000 per-instrument API calls), and the GEX math itself (per-strike net
exposure, zero-gamma flip-point scan via hypothetical-spot rescan, wall detection, a
~30-day-tenor ATM IV proxy — deliberately not the literal nearest expiry, which can be
a same-day 0DTE-like quote incomparable to a rolling realized-vol index; caught this
via a live sanity check, not a test, and fixed before shipping).

`gex_paper.py` is a cron-friendly single-shot logger, same shape as `tsmom_paper.py` —
appends one row per run to `data/gex_log.csv`, no trades, not wired into
`entry_checklist`/`probability_gate`. Filed in `hypothesis_registry.yaml` as
`gex-dealer-exposure-walls`, `status: infrastructure`, with the sign-convention
assumption (call OI = dealer long gamma, put OI = dealer short gamma — a standard
public-GEX-tracker approximation, unverifiable from public data) stated in the code
docstring, the research doc, and the registry notes, all three, so it can't get lost.

**Verification:** 9 new tests (`tests/test_gex_calculator.py`, synthetic data, no
network) all pass; full suite 3581 passed, same 9 pre-existing failures as master.
Ran `gex_paper.py` against live Deribit BTC data end-to-end — sane output (flip point
below spot, walls bracketing spot, IV/RV roughly in line).

**Cross-lane note:** none — new module, doesn't touch any existing strategy or
decision-pipeline file.

**Not done, on purpose:** no backtest (Deribit doesn't serve historical option chains;
would need paid historical-options data), no forward-return measurement yet (this PR
is the pipeline, not the evidence), no live/paper wiring. See the research doc's "what
has NOT been done" section before extending this.

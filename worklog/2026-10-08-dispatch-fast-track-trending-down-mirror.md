# 2026-10-08 — dispatch — fast-track counter-trend veto was one-sided

**Lane:** directional strategy (`src/paper_trading.py`)
**PR:** #pending (opened this run) — `dispatch/fix-fast-track-trending-down-mirror`

## What I found

The "OFI + Lead-Lag fast-track" entry path in `run_paper_trading_session()`
(`src/paper_trading.py`, the block tagged `'fast-track'` that bypasses the normal
conf=55 gate when OFI and lead-lag both align) had a counter-trend veto that only
covered one side:

```python
# Don't short into a strong uptrend
if not (ft_signal == Signal.SELL and regime_name == 'TRENDING_UP'):
```

This correctly blocks a SELL fast-track signal when the regime is `TRENDING_UP`,
but had no mirror clause blocking a BUY fast-track signal when the regime is
`TRENDING_DOWN`. `TRENDING_UP`/`TRENDING_DOWN` are treated as a matched adverse
pair everywhere else in this module (`_diagnose`'s regime-alignment check) and in
`regime_detector.RegimeResult.allows_long` / `entry_checklist`'s regime-block
logic — so this one path let the bot open a long straight into a strong
downtrend, the exact counter-trend risk the SELL-side half of the gate exists to
prevent on the other side.

This is a **new, distinct** finding from the open backlog — not the same as
PR #157's `scientific_strategy._has_buy_signal` TRENDING_UP mirror (different
function, different file: that one is direction-selection logic in
`scientific_strategy.py`; this is `paper_trading.py`'s separate inline
fast-track signal-construction block, with its own independently-asymmetric
regime guard). Checked it against all currently-open PRs on these 7 lane files
before filing.

## Fix

Extracted the asymmetric inline condition into a pure helper,
`_fast_track_counter_trend(ft_signal, regime_name)`, that vetoes **both**
SELL-into-TRENDING_UP and BUY-into-TRENDING_DOWN, and swapped the inline
condition for a call to it. Same shape as PR #150's `_regime_forces_exit`
helper for the analogous exit-side gap.

This **tightens** an existing gate (adds a veto path that was missing) — it
does not loosen `atr_alive` or any cost-aware filter, and does not change the
SELL-into-TRENDING_UP behavior that was already correct. Consistent with
CLAUDE.md's "Core principle: do NOT loosen gates" — this is the opposite
direction, closing a counter-trend hole.

## Evidence / tests

- Added `TestFastTrackCounterTrend` (6 cases) to `tests/test_paper_trading.py`
  covering both counter-trend directions, both correctly-aligned directions,
  and three neutral regimes.
- `python -m pytest tests/ -q` → **3641 passed, 0 failed** (up from the prior
  3634 baseline + 7 new tests; no pre-existing failures observed this run).

## Note on the backlog

Per #156 (2026-10-06), there are still 29 open draft PRs on these 7 lane files,
oldest 47+ days, zero merged since #100 opened — already escalated to the owner
via notification on a prior run. Not re-escalating again this run since nothing
has changed in the 2 days since; this run found one new genuine fix instead of
repeating the status-pass finding a fifth time.

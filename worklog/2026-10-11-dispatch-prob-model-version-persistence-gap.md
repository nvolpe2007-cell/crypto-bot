---
date: 2026-10-11
agent: dispatch
branch: dispatch/fix-prob-model-version-persistence-gap
pr: (opened this run)
lane: directional
files: [src/paper_trader.py, src/paper_trading.py, tests/test_position_persistence.py]
---

# `prob_model_version` was set on every entry but never saved/restored — a restart silently zeroed it, hiding live trades from calibration and the expectancy gate

## The bug

`paper_trading.py` sets three sibling fields on every LONG/SHORT entry right after the
probability gate runs (lines ~2082-2088 and ~2210-2216): `prob_win`, `edges_used`, and
`prob_model_version = PROB_MODEL_VERSION`. `prob_win` and `edges_used` were both written
to `open_positions.json` in `_save_open_positions()` and restored in
`_load_open_positions()`. `prob_model_version` was the third sibling in that same
assignment group but was never added to the save dict and never restored — worse, it
wasn't even a declared field on the `PaperPosition` dataclass (`src/paper_trader.py`), so
a position reconstructed from disk after a restart simply didn't have the attribute.

**Failure scenario:** a position opens under the current probability-gate model
(`PROB_MODEL_VERSION`, currently 3). Before it closes, the process restarts — routine on
the VPS via `auto_deploy.ps1`, a crash, or a systemd restart. `_load_open_positions()`
rebuilds the position without `prob_model_version`. When it eventually closes,
`_record_to_journal()` (`paper_trading.py:341`) does
`int(getattr(pos, 'prob_model_version', 0))` → writes `0` to the trade journal.
Downstream, `calibration.py`'s `MIN_MODEL_VERSION` filter (default 3) and
`expectancy_gate.py`'s `ExpectancyGate.min_model_version` filter both exclude that row
from the isotonic-calibration fit and the per-path expectancy stats — treating a live,
correctly-gated v3 trade as if it were legacy pre-versioning data, purely because it
survived a restart. Every position open across a restart silently lost its calibration/
expectancy signal.

This is the same family as PR #159 (`liquidation_price` dropped on restart) — a field
set alongside other fields that *are* persisted, but itself missing from both the save
dict and the dataclass — just a different field with a different, quieter blast radius
(no crash, just data silently excluded from the proof pipeline).

## The fix

- `src/paper_trader.py`: added `prob_model_version: int = 0` as a real `PaperPosition`
  dataclass field (previously only ever set as a dynamic attribute via
  `position.prob_model_version = ...`, never declared).
- `src/paper_trading.py`: `_save_open_positions()` now writes
  `'prob_model_version': pos.prob_model_version` next to `edges_used`;
  `_load_open_positions()` now restores it (`int(p.get('prob_model_version', 0))`,
  defaulting to 0 — the same value an un-set position already had — so an older snapshot
  written before this field existed still loads cleanly).
- No gate, threshold, or sizing logic touched — pure state-persistence correctness, per
  CLAUDE.md's "do not loosen gates" constraint.

## Tests

Added to `tests/test_position_persistence.py` (mirrors the existing
`TestProbabilityGateContextRoundTrip` style):
- `test_prob_model_version_survives` — round-trips a position with
  `prob_model_version=3` through save→load, asserts it comes back as 3 (would have come
  back 0 before the fix).
- `test_prob_model_version_defaults_to_zero_when_absent` — a snapshot missing the key
  entirely (simulating one written before this fix shipped) still loads without error,
  defaulting to 0.

## Verification

Installed a clean venv (`/tmp/venv`) from `requirements.txt` — this container's base
Python had a broken system `cryptography`/pip mismatch that silently failed earlier
install attempts; the venv sidesteps it cleanly.

`python -m pytest tests/ -q`: **3636 passed, 0 failed** (was 3634 passed on master per
the 2026-10-06 status pass; +2 are the new tests above, no regressions).

# 2026-09-09 — dispatch — `ScientificStrategy.ofi_min` was a dead constructor parameter

**Branch/PR:** `dispatch/fix-scientific-strategy-ofi-min-wiring`
**Lane:** directional (`src/scientific_strategy.py`)

## What I found

Ran the priority checklist for this run:
- **(a) Analyze recent forward-test results** — not executable in this sandboxed
  cloud session. `data/` is gitignored and empty here (no `trade_journal.csv`,
  no `*_state.json`); that data only exists on the VPS. Confirmed via
  `proof_scorecard.py`'s `_directional()`/`_microstructure_forward()` etc., which
  all read from `DATA = Path('data')`.
- **(c) Read-only status pass** — redundant with yesterday's `dispatch/pr-backlog-status-pass-11`
  (PR #119, open): master unchanged since then except PR #113 merging; `git fetch`
  confirms no new movement. Re-running the same backlog audit a day later adds
  nothing — that PR already correctly recommends merging #100-#105 rather than
  opening a 7th unreviewed PR. I did independently re-verify `python -m pytest
  tests/ -q` on current master: **3584 passed, 0 failed** (matches #119's claim).
- **(b) Small, testable improvement** — this is what I did. See below.

## The bug

`ScientificStrategy.__init__` (`src/scientific_strategy.py:135-142`) takes and
stores `ofi_min`, `lead_lag_min`, and `min_confidence` as instance attributes.
`_evaluate()` never reads `self.ofi_min` — the OFI BULLISH/BEARISH
classification used a hardcoded `0.15` literal instead:

```python
ofi_dir = 'NEUTRAL'
if ofi is not None:
    if ofi >  0.15: ofi_dir = 'BULLISH'      # should be self.ofi_min
    elif ofi < -0.15: ofi_dir = 'BEARISH'    # should be -self.ofi_min
```

Concretely: `ScientificStrategy(ofi_min=0.90)` and `ScientificStrategy(ofi_min=0.01)`
produced byte-identical `ofi_dir`/`ofi_score`/`signal` for the same OFI input —
the constructor argument was silently a no-op. `0.15` happens to equal the
default, so **today's only call site** (`src/live_trading.py:97`,
`ScientificStrategy()` with no args) was unaffected — this was latent, not
active-in-production. But it's a real correctness bug: anyone who tries to
tune `ofi_min` (a knob that exists specifically to be tuned) gets nothing, and
`tests/test_scientific_strategy.py` had zero coverage asserting the parameter
does anything.

(Found via a targeted Explore-agent review scoped to my lane's 7 files,
explicitly excluding the 6 bugs already fixed in open-but-unmerged PRs
#100-#105 so as not to duplicate that work.)

## The fix

One-line-times-two: read `self.ofi_min` instead of the `0.15` literal in both
branches. Default behavior is byte-identical (`0.15 == 0.15`) since production
never instantiates with a non-default value — this does not loosen or tighten
any live gate, it only makes the already-declared knob functional for anyone
who parametrizes it later.

Deliberately did **not** touch `lead_lag_min` (no matching literal in the file
— unclear what it was meant to gate; the real lead-lag strength normalization
lives inside `LeadLagDetector` itself) or `min_confidence` (the actual
production confidence gate is a completely separate, independently-adaptive
mechanism in `paper_trading.py`'s `_adapt['min_confidence']` consumed via
`entry_checklist.py`'s hard `min_confidence` check — wiring the constructor's
static `min_confidence=45.0` into `_evaluate()` risked silently overriding
that deliberate adaptive-threshold design, which is out of scope for a
"small, safe" fix). Both are candidates for a future, more careful pass if
someone confirms the intended semantics.

## Tests

Added `TestOfiMinThreshold` (3 cases) to `tests/test_scientific_strategy.py`:
default `ofi_min` still classifies a moderate OFI as BULLISH; a stricter
`ofi_min` suppresses the same OFI value down to HOLD; a looser `ofi_min`
picks up a weaker OFI that the default would ignore. Verified all three fail
against the pre-fix code (`git stash` the source change, re-run — 2 of 3
failed with the expected wrong-signal mismatch) and pass with it applied.

Full suite: `python -m pytest tests/ -q` → **3587 passed, 0 failed** (3584
baseline + 3 new).

## Why no other change this run

Runner-up candidates from the same review, left alone:
- `src/orderflow_ws.py`'s `get_spread_pct` returns a percentage (0.08) while
  every consumer in-lane uses a fraction (0.0008) — a 100x scale mismatch, but
  it's dead code with zero callers anywhere in `src/`/`tests/`, so no live bug
  yet. Worth a note here so nobody wires it up unmodified.
- `src/pairs_strategy.py`'s `_divergence_start` reset resumes "fresh" the tick
  after `MAX_DIVERGENCE_AGE` if conditions still hold, undermining the
  "stale divergences don't work" docstring — more a design ambiguity than a
  crisp bug, and `pairs_strategy.py` has no dedicated test file
  (`tests/test_pairs_paper.py` covers the separate `pairs_paper.py`), so a fix
  there would need new test scaffolding from scratch. Left for a future run.

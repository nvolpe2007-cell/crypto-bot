---
date: 2026-09-17
agent: dispatch
branch: dispatch/fix-orderflow-data-age-secs-epoch-default
pr: pending (opened this run)
lane: directional
files: [src/orderflow_ws.py, tests/test_orderflow_ws.py]
---

# `OrderFlowWS.data_age_secs()` defaulted missing timestamps to Unix epoch 0, not `_STALE_SECS`

## What I did this run

1. `git fetch && checkout master && pull` — already up to date, no new commits since the
   2026-09-16 run.
2. Read `CLAUDE.md` in full, `WORKLOG.md`, and `python scripts/worklog_index.py`.
3. Checked the open PR backlog via the GitHub MCP tools: 30 open PRs, ~12 already sitting
   in the directional lane, most unmerged for 1-4 weeks (same picture the 2026-09-15
   status-pass run described — "heavily mined, waiting on a merge pass"). Yesterday's run
   (2026-09-16, PR #131) still found something new, so I didn't treat "heavily mined" as a
   reason to skip straight to a no-op status pass.
4. Set up the environment (`pip install -r requirements.txt`, `pip install pytest
   pytest-asyncio` — a fresh container has neither); baseline `python -m pytest tests/ -q`:
   **3584 passed, 0 failed** (confirms the 2026-09-15 finding that CLAUDE.md's "2 known
   pre-existing fails" note is stale; not touched again this run, still low-value relative
   to backlog churn).
5. Delegated a focused read-only investigation (Explore subagent) to hunt for one new,
   small, safe defect in the directional-lane files, explicitly excluding the ~15 issues
   already covered by open PRs #100-#131, with extra attention on `indicators.py` (least-
   scrutinized file, only one prior finding) and any code paths outside the specific
   functions those PRs already touch.

## What I found and fixed

`indicators.py` checked out clean beyond the already-known/PR'd supertrend bug (EMA/RSI
warm-up, Supertrend band math, `ema_htf` bounds are all correct). The one new finding:

**`src/orderflow_ws.py::OrderFlowWS.data_age_secs()`** (lines 185-193 pre-fix): the
docstring promises `_STALE_SECS` (90) for a symbol that has never received data. The
implementation used `self._cvd_updated.get(symbol, 0)` / `._book_updated.get(symbol, 0)`,
defaulting the *never-arrived* case to Unix epoch 0 — so `time.time() - 0` returned
roughly 1.79 billion seconds instead of 90. `is_data_fresh()` (the only current caller)
happened to still behave correctly, since any astronomically large number is trivially
`> max_age`, which is exactly why this survived: nothing exercising the code path noticed.
But the module's own docstring anticipates a dashboard-style consumer of the raw age value
("Returns _STALE_SECS if no data has arrived yet" is a promise about the *number*, not just
its pass/fail comparison), and any such caller — a "data age: Xs" display, a metric export —
would show a nonsensical multi-billion-second value.

**Fix:** default the missing timestamp to `now - _STALE_SECS` instead of `0`, so the
never-arrived case returns exactly `_STALE_SECS`, matching the docstring. Pure bugfix,
no behavior change to `is_data_fresh()`'s boolean output (large-vs-small comparison is
unaffected — this is a correctness fix for the *numeric* value, not a gate change; does
not touch or loosen any cost-aware filter).

## Verification

- Added `TestDataAgeSecs` (2 cases: never-arrived returns `_STALE_SECS` exactly;
  normal elapsed-time case still returns the true age) to `tests/test_orderflow_ws.py`,
  following the existing `_cvd_updated`/`_book_updated` manipulation pattern already used
  by `TestCvdTrendAndStaleness`.
- `python -m pytest tests/test_orderflow_ws.py -q` → 18 passed (16 existing + 2 new).
- `python -m pytest tests/ -q` (full suite) → **3586 passed, 0 failed** (3584 baseline + 2
  new tests, no regressions).

## Why this is safe / in scope

No behavior change to any entry gate, cost filter, or trade decision — `confirms_buy` /
`confirms_sell` / `get_cvd_trend` / `get_obi` are untouched, and `is_data_fresh()`'s return
value is unchanged for every currently-reachable call (there are no callers of
`data_age_secs()` or `is_data_fresh()` elsewhere in the repo today — this closes a latent
trap for whichever future dashboard/metrics consumer WORKLOG.md's `OrderFlowWS` docstring
already anticipates). Does not touch `atr_alive` or any other cost-aware gate per CLAUDE.md's
core principle.

## Suggestion carried forward from 2026-09-15's run (unchanged)

Still true: ~12 small, tested, already-open directional-lane PRs (dozens more across other
lanes) remain unmerged for up to 4 weeks. A merge pass — even just the doc-only/test-only
ones (#101, #104, #126, #128) — would raise the marginal value of future dispatch runs,
which are increasingly spending their budget finding genuinely new (if smaller and smaller)
issues rather than having anything land.

---
date: 2026-09-10
agent: dispatch
branch: dispatch/fix-pairs-strategy-divergence-rearm
pr: TBD
lane: directional
files: [src/pairs_strategy.py, tests/test_pairs_strategy.py]
---

# `PairsStrategy` staleness reset let a persistent divergence re-fire as "fresh" every tick after `MAX_DIVERGENCE_AGE`

Scheduled "continue the profitability mandate" pass.

**Start-of-run checks:** `git fetch && git pull` — master unchanged since
`dispatch/fix-scientific-strategy-ofi-min-wiring` (PR #121, still open/draft) landed
its worklog entry yesterday. `python -m pytest tests/ -q` on current master: **3584
passed, 0 failed** (installed `numpy`/`pandas`/etc. fresh in this sandbox venv — not
present by default). No live VPS/data access from this cloud session (`data/` is
gitignored and holds only fixture rows dated 2024-01-01), so **(a) analyze recent
forward-test results** wasn't executable, same as the last two runs.

Reviewed the open PR backlog first so as not to duplicate: #100-#105 and #121 already
cover `orderflow_ws.py`, `entry_checklist.py`, `paper_trading.py`, `live_trading.py`,
and `scientific_strategy.py` with specific, unmerged fixes. PR #121's own worklog entry
(`2026-09-09-dispatch-scientific-strategy-ofi-min-dead-wiring.md`) explicitly flagged a
runner-up finding it left undone: a staleness bug in `src/pairs_strategy.py`'s
`_divergence_start` handling, blocked on "no test scaffolding." This run built that
scaffolding and applied the fix — **(b)** from the priority list.

## The bug

`src/pairs_strategy.py::_evaluate_pair` (lines ~186-192, pre-fix) tracked how long a
z-score divergence had been active via `_divergence_start[pair]`, and rejected the
signal once `age > MAX_DIVERGENCE_AGE` (10 minutes) — the class docstring promises
"stale divergences don't work." But the reject branch also did
`self._divergence_start[pair] = None` before returning. If the underlying divergence
was still active on the very next evaluation (z-score still ≥ threshold, leader/lagger
returns still qualifying — nothing about the market changed), `_divergence_start[pair]`
being `None` made that next call treat it as a **brand new** divergence starting *now*,
so `age` reset to ~0 and the signal fired again immediately, tagged with a fresh-looking
`age=0s` in the log line. The 10-minute staleness filter was a one-tick blip, not a
lasting block, for any divergence that persisted past the cutoff — which is exactly the
persistent-divergence case the filter exists to catch.

**Live impact: none today.** `src/pairs_strategy.py` has zero call sites — grepped
across `src/`, `arbitrage/`, and every `*_paper.py`/`*.py` entry point; nothing imports
`PairsStrategy` or `PairsSignal`. `pairs_paper.py` (the arm actually wired into the VPS
crontab per `WORKLOG.md`) implements its own, separate dollar-neutral pairs-trading
logic and does not use this class. So this fix cannot change any live behavior — it
corrects a latent bug in code that would matter if `PairsStrategy` is ever wired up.

## The fix

Removed the `self._divergence_start[pair] = None` line from the staleness-reject
branch only. The two *other* resets in the function (z-score drops below threshold;
leader/lagger return conditions stop qualifying) are correct as-is and untouched —
those really do mean the divergence condition broke, so the next occurrence should be
treated as fresh. Now, once a divergence goes stale, it stays blocked for as long as it
persists, and only re-arms once it actually clears and later re-forms — matching the
docstring's guarantee. Added an inline comment explaining why the reset was removed
(so the next reader doesn't "fix" it back).

## Tests

New file `tests/test_pairs_strategy.py` (none existed before for this class —
`tests/test_pairs_paper.py` covers the separate `pairs_paper.py` script only). Drives
`_evaluate_pair` directly with stubbed `_zscore`/`_five_min_return` (avoids simulating
a full timestamped price feed just to hit a specific z-score/return combination):

- `test_fresh_divergence_fires` — sanity check, unrelated to the bug.
- `test_stale_divergence_is_blocked_and_stays_blocked` — the regression test. Verified
  it **fails on pre-fix code** (`git stash` the source change, rerun): the third
  assertion trips because the tick right after going stale returns a live
  `PairsSignal` instead of `None`. Passes with the fix.
- `test_divergence_rearms_after_conditions_clear_and_reform` — confirms the *other*
  resets still work: divergence clearing (z drops under threshold) does reset state,
  and a subsequent fresh divergence fires normally.

`python -m pytest tests/test_pairs_strategy.py -q` → 3 passed.
`python -m pytest tests/ -q` → **3587 passed, 0 failed** (3584 baseline + 3 new).

## Not done / left for a future run

- `src/orderflow_ws.py`'s `get_spread_pct` percentage-vs-fraction scale mismatch
  (flagged in PR #121's worklog, still open) — not touched this run, stayed in scope
  (one fix per run per CLAUDE.md's small/testable guidance).
- No forward-test/paper-trading analysis this run — still no VPS data access from this
  sandbox. If a future run gets that access, `proof_scorecard.py`'s directional-arm
  verdict and `data/trade_journal.csv` funnel counts are the first things to check
  against the Core Principle (costs dominate; don't loosen gates without evidence).

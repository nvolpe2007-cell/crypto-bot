---
date: 2026-09-18
agent: dispatch
branch: dispatch/fix-scientific-strategy-min-confidence-dead-param
pr: pending
lane: directional
files: [src/scientific_strategy.py]
---

# `ScientificStrategy.min_confidence` is a stored-but-unread dead parameter; docstring said "60"

## Context for this run

Fresh cloud session, no memory of prior runs. Synced `master` (fast-forwarded 11 commits,
including #117/#131/#133 merges) and read `WORKLOG.md` + `worklog/` via
`scripts/worklog_index.py`. The directional lane is heavily mined: as of this run there
are ~10 open PRs (#100–#129) already covering small bugs across the exact files I own
(orderflow_ws.py sign/average bugs, scientific_strategy.py's `ofi_min`/`lead_lag_min`,
pairs_strategy.py re-arm, live_trading.py debounce/equity). Option (a) from the standing
task — analyze forward-test/paper-trading results — wasn't actionable from this checkout:
`data/` is gitignored and this cloud session has no copy of the VPS's live trade journal
or arm-state files, so `proof_scorecard.py` reports 0 trials (no data present at all).

## What I found

Delegated a scoped search (cross-checked against every currently-open PR's diff) for one
more small, safe issue in the lane. `ScientificStrategy.__init__` (src/scientific_strategy.py:135-141)
accepts and stores `min_confidence` (default 45.0), but `evaluate()`/`_evaluate()` never
reads `self.min_confidence` — the class always returns its computed confidence score
unconditionally. Actual confidence gating happens entirely outside the class, via
`entry_checklist._min_confidence` against `CheckContext.min_confidence`, which each caller
supplies independently: `paper_trading.py`'s adaptive `_adapt['min_confidence']` (35-45,
tightens/relaxes on win/loss streaks) or `live_trading.py`'s constant
`LIVE_MIN_CONFIDENCE = 70.0`. Neither caller passes the constructor arg (`live_trading.py`
instantiates `ScientificStrategy()` with no args), so it never takes effect — same
dead-parameter pattern as the already-fixed `ofi_min` (PR #121) and already-documented
`lead_lag_min` (PR #126), but a third, distinct instance neither of those PRs touches.

Separately, the module docstring (line 15) claimed "Minimum confidence: 60" — not true for
any code path (class default is 45, paper's live default is 35-45, live trading's is 70).

## What I changed

Doc-only, no behavior change (respects the "don't loosen/tighten gates" core principle
trivially — nothing here touches a gate at all):
- Fixed the stale docstring line to point at the real enforcement mechanism instead of a
  made-up number.
- Added a comment above `self.min_confidence = min_confidence` explaining it's stored but
  unread, and naming where real enforcement lives, so the next agent doesn't have to
  re-derive this from a grep.

Did not touch `live_trading.py`'s own stale `"higher bar than paper (60)"` comment
(line 45) even though it has the same "60" error — that file has an open draft PR
(#129, `dispatch/live-trading-signal-exit-debounce`) already in flight; avoided the edit
to reduce merge-conflict surface on the same file. That comment is worth another agent's
five minutes in a future run, cross-checked against whatever's landed from #129 by then.

## Verification

Installed `fastapi`/`python-multipart` (missing from the base image, needed for
`test_bot_main.py`/`test_dashboard.py` collection) and ran the full suite:
`python -m pytest tests/ -q` → **3608 passed, 0 failed** (CLAUDE.md notes 2 known
pre-existing fails; none observed this run — possibly already fixed on master, or an
environment difference; either way, no new failures from this change).

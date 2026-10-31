---
date: 2026-10-31
agent: dispatch
branch: dispatch/fix-pairs-strategy-divergence-sign-flip
pr: 168
lane: directional
files: [src/pairs_strategy.py, tests/test_pairs_strategy.py]
---

# `PairsStrategy._divergence_start` inherited stale age on a leadership flip — fixed + first tests for the module

## What I did this run

1. `git fetch && checkout master && pull` — already up to date (last merge #163,
   `dispatch/lane-status-pass-1015`).
2. Read `CLAUDE.md` + `WORKLOG.md` + `python scripts/worklog_index.py` + pulled the full
   open-PR list via the GitHub MCP tools (41 open PRs, 37 touching the 7 directional-lane
   files). Read PR #166 (`dispatch/lane-status-pass-1019`, still open/draft, 12 appended
   commits through 2026-10-30): the lane has been in a pure "0 merges" holding pattern
   since 2026-10-15 (now 16 days), already escalated to the owner once via push
   notification on 2026-10-15, deliberately not re-escalated since to avoid alert
   fatigue — nothing qualitatively new to report there.
3. Rather than append a 13th no-op status-pass commit to #166, forked a sub-agent to scan
   the lane files against the 30 already-filed PR topics (listed in the fork prompt) and
   surface one genuinely new, un-filed, small, safe, testable issue — same bar CLAUDE.md
   sets (don't loosen cost-aware gates). It found `src/pairs_strategy.py`'s
   `_evaluate_pair()` divergence-age bug (below); I independently read the file and
   verified the bug and the fix myself before writing any code.
4. Confirmed `src/pairs_strategy.py` is still dead code in production (no non-test
   importer — `pairs_paper.py` only *mentions* it in a comment) — same finding as
   `worklog/2026-09-15-dispatch-lane-status-pass.md`. Fixed it anyway, consistent with
   PR #123's precedent (also a correctness fix to this same dead-but-documented module).

## The bug

`_divergence_start[pair]` tracked only *when* a pair's spread first exceeded
`ZSCORE_THRESHOLD`, never *which direction* it diverged in. If the spread's z-score
crossed from `+threshold` straight through to `-threshold` (leader/lagger roles flip)
without ever dipping back below `ZSCORE_THRESHOLD` in between, `age = now -
self._divergence_start[pair]` kept counting from the **old** direction's start time. A
brand-new divergence in the new direction could then be wrongly rejected as stale
(`age > MAX_DIVERGENCE_AGE`) or shortchanged on the freshness confidence bonus — purely
because of unrelated history for the opposite direction. None of the module's four
existing reset conditions (`|z|` below threshold, leader move too small, lagger already
caught up, age exceeded) covered a same-tick direction flip.

## The fix

Added `self._divergence_sign: Dict[pair, Optional[int]]` alongside `_divergence_start`;
reset `_divergence_start[pair]` to `None` whenever the current sign differs from the
stored one, right before the existing "set start time if None" block. Same reset pattern
already used for the other three conditions — doesn't touch `ZSCORE_THRESHOLD`,
`MIN_LEADER_MOVE`, `MAX_LAGGER_MOVE`, or `MAX_DIVERGENCE_AGE` themselves, so no gate is
loosened; it just makes the existing age/freshness math apply to the correct interval.

## Tests

No test file for `src/pairs_strategy.py` existed at all (the lane's test file with a
similar name, `tests/test_pairs_paper.py`, covers the *different*, actually-live
`pairs_paper.py` module). Added `tests/test_pairs_strategy.py`:
- `test_divergence_age_resets_on_leadership_flip` — stubs `_zscore`/`_five_min_return`
  directly (avoids depending on the price-history deque's warm-up/timing) and drives a
  same-tick leadership flip after `MAX_DIVERGENCE_AGE + 60s`. **Verified it actually
  catches the bug**: `git stash`-ed the `src/pairs_strategy.py` change and reran — fails
  with `assert None is not None` at the "rejected as stale" assertion; passes again with
  the fix restored.
- `test_divergence_age_persists_without_flip` — sanity check that age is *not* reset when
  direction stays the same.

`python -m pytest tests/ -q`: 3639 → 3641 passed, 0 failed, 0 new failures.

## Status

PR #168 opened as draft, subscribed to PR activity. Backlog is now 42 open directional-lane
PRs touching these files (unchanged bottleneck — reviewer throughput, not finding
availability; see #166 for the ongoing status thread). Not duplicating that escalation
here since nothing about it changed this run.

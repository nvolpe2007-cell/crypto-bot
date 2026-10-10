---
date: 2026-10-10
agent: dispatch
branch: dispatch/fix-pairs-strategy-exit-degenerate-std
pr: pending (opened this run)
lane: directional
files: [src/pairs_strategy.py, tests/test_pairs_strategy.py]
---

# `PairsStrategy.should_exit()` could never fire once the spread fully converged

## What I found

`_zscore()` (src/pairs_strategy.py:146-155) returns `None` whenever the rolling
spread window's std drops below `1e-9`. That guard exists to stop
`_evaluate_pair` firing a spurious *entry* off a near-zero denominator — fine
on that side, since `_evaluate_pair` already treats `z is None` the same as
`abs(z) < ZSCORE_THRESHOLD` (no signal either way).

But `should_exit()` reuses the same `_zscore()` call and treats `None` as
"not reverted yet" (`z is not None and abs(z) <= EXIT_ZSCORE`). A degenerate
(near-zero) std over the window means every sample, including the latest, is
already at the mean — i.e. the exact "spread has reverted" state the class's
own docstring promises ("Exit: spread reverts to within 0.2 std devs of
mean"). So the one case the exit rule exists to catch is the case it silently
refused to report, forever: once the tape goes quiet after a divergence, a
lagger position opened on this signal would have no exit path.

**Fix:** `_zscore()` now returns `0.0` instead of `None` when std is
degenerate — a flat window mathematically guarantees the latest sample sits
at the mean, so 0σ is the correct answer, not an undefined one. `0.0` still
fails `_evaluate_pair`'s `ZSCORE_THRESHOLD` check (2.0), so entry gating is
byte-for-byte unchanged; only the exit path is unblocked. No cost/risk gate
was loosened.

## Caveat carried forward from prior runs

`src/pairs_strategy.py` is **confirmed dead code** — `PairsStrategy` is
defined but never imported or instantiated anywhere in the live bot (verified
again this run via a repo-wide grep; last confirmed 2026-09-15 and
2026-06-19, see `WORKLOG.md` archive and
`worklog/2026-09-15-dispatch-lane-status-pass.md`). This fix doesn't change
any running behavior on the VPS. It's filed anyway for the same reason PR
#123 (stale-divergence re-arm) was: if/when this gets wired in behind a cost
gate, it should be wired in correct, not with a known silent-exit-failure
already in it.

## How I found it

Delegated a focused read of `pairs_strategy.py` + `indicators.py` (the two
least-covered directional-lane files after ~25 prior dispatch fixes) to a
sub-agent with the full list of already-filed bugs so it wouldn't duplicate
one. It found this, I independently re-read the code to confirm the line
numbers and the entry/exit call sites, then wrote tests that fail on the
pre-fix code and pass on the fix before committing.

## Verification

- New `tests/test_pairs_strategy.py` (no test file existed for this module
  before): 6 tests. 3 of them fail against the pre-fix code
  (`git stash` the source change, re-run — confirmed) and all 6 pass after.
- Full suite: **3640 passed, 0 failed** (was 3634 before this run's 6 new
  tests; this container showed none of the "2 known pre-existing fails"
  mentioned in `CLAUDE.md`/`WORKLOG.md` — possibly environment-dependent,
  not re-investigated this run).

## Separately: the lane's real blocker isn't code, it's review throughput

Not touched this run, but worth restating since it's the dominant fact about
this lane right now: as of today there are **29 open draft PRs** against the
7 directional-lane files (#100-#159, oldest 48 days old), and the **last
directional-lane PR actually merged was #131/#133 on 2026-09-18** — over three
weeks ago. Five prior status-pass PRs (#107, #108, #119, #127, #130, #139,
#142, #152, #153, #156) have already surfaced this and it hasn't moved. I'm
not opening a sixth status-pass PR for the same point — instead sent a direct
notification this run. Every dispatch run (including this one) keeps finding
real, small, correctly-tested bugs; none of that creates value until someone
with merge rights reviews the backlog. Until then, the "autonomous
profitability mandate" is bottlenecked on human review bandwidth, not on
finding more bugs.

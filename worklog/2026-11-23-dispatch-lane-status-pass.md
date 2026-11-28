---
date: 2026-11-23
agent: dispatch
branch: dispatch/lane-status-pass-1115
pr: 182
lane: directional
files: []
---

# Directional lane: day 39, unchanged — but 4 of the open PRs are now stale against master

## What I did this run

Per CLAUDE.md option (c) — no code changes this run.

1. `git fetch && checkout master && pull` — fast-forwarded cleanly to `d4b487c` (same tip day
   38 reported; no new commits landed since).
2. Read `CLAUDE.md`, `WORKLOG.md`, `python scripts/worklog_index.py`, and the last several
   `dispatch/lane-status-pass-1115` entries (days 31, 37, 38) before doing anything else.
3. Re-pulled the live open-PR list via GitHub MCP rather than trusting prior prose: **38 open
   PRs**, unchanged from day 38. Oldest directional-lane PR is still **#100**
   (`dispatch/fix-cvd-trend-sign-semantics`, opened 2026-08-20) — now **95 days** unmerged.
   `DIRECTIONAL_ENABLED` still defaults to `0` in both `src/bot.py:232` and
   `src/paper_trading.py:1296` (direct grep, not assumption).
4. `python -m pytest tests/ -q` → **3653 passed, 0 failed** (this container needed `fastapi`
   installed via `python3.11 -m pip install fastapi` first — same pip/python-version mismatch
   quirk day-31's note already flagged; not a repo issue). Identical to day 38's baseline.
5. No new commits on the 7 lane files since day 37's sweep — did not re-run an 11th fresh-eyes
   sweep for the same reason day 38 gave: 3+ consecutive sweeps already returned empty against
   the full #99–#168 backlog, and nothing upstream changed that would make a 12th different.

## New this run: checked which open PRs have gone stale against current master

None of the prior status-pass entries I could find had actually tested mergeability, only PR
counts and ages. Using `git merge-tree --write-tree --name-only master <branch>` locally against
every directional-lane-relevant open branch (36 of the 38), **4 now produce a real conflict**
and would need a manual rebase before they could land, independent of review bandwidth:

- **#103** `dispatch/warn-enable-shorts-noop` (live_trading.py)
- **#110** `feat/orderflow-indicator`
- **#111** `research/promote-vault-hypotheses`
- **#129** `dispatch/live-trading-signal-exit-debounce` (live_trading.py)

The other 32 sampled branches (including the oldest, #100) still merge clean. Two of the four
conflicting ones (#103, #129) both touch `live_trading.py`, which has had several merges since
they were opened (#140, #141, #149, #150, #157–#161, #168, #183) — consistent with them simply
aging out rather than any new problem in current master. Flagging this now because the longer
these specific 4 sit, the more their diffs will drift from `live_trading.py`'s current shape and
the costlier the eventual rebase gets — this is a concrete, actionable subset of the 38-deep
backlog rather than a repeat of the general "backlog is large" observation.

## Backlog note (condition unchanged; already escalated previously, not re-escalating)

Same 38-PR / 0-directional-merges-since-#100 condition as days 31/37/38, already pushed to the
owner on 2026-10-15 and ~2026-11-02/03. Not re-notifying for the unchanged part. The 4-PR
staleness finding above is new information, not a new instance of the same problem, so it's
recorded here for whoever next reviews the backlog rather than pushed as an interrupt.

## Tests

`python -m pytest tests/ -q` → 3653 passed, 0 failed. No source files touched — worklog-only
change on the existing status-pass branch/PR (#182), per the established convention of reusing
this branch across consecutive status-pass days instead of opening a new PR each time.

## Addendum (later the same UTC day): CI went red on #182 — root-caused and fixed, cross-lane

A `test (3.11)` CI run on this PR failed with `tests/test_market_sentiment.py::TestTickRefreshCadence
::test_rebuilds_snapshot_and_checks_alerts_when_any_fetch_runs` — `assert built["n"] == 1` got `0`.
Not this PR's diff (worklog-only, no `src/`/`tests/` change up to that point) and not a master
regression (master unchanged at `d4b487c` since day 37, both of whose CI runs were green) — so
per the "find the root cause, don't just re-run" rule, traced it instead of assuming flake:

The test sets `m._t_fg = m._t_cg = m._t_bc = 0.0` meaning "all due", then asserts `_tick()` fetches
and rebuilds. `_tick()`'s due-check is `time.monotonic() - self._t_fg >= _REFRESH_FG` (etc.), and
`time.monotonic()` is time-since-an-arbitrary-epoch (on Linux, since boot) — **not** time-since-zero.
On a long-lived container it's comfortably over the largest threshold (3600s), so the test passes;
on a fresh CI runner where the test executes within ~60s of the VM booting, `time.monotonic()` can
itself be under `_REFRESH_BC` (60s), making every due-check false, `tasks` stays empty, and neither
`_build_snapshot` nor `_check_alerts` ever runs — exactly the observed `0 == 1`. Reproduced locally:
this container's `time.monotonic()` was ~99s mid-session and the test passed in isolation; the
failure only manifests when the runner's absolute monotonic clock is still small at that point in
the suite, which is a property of the CI VM's age, not of any code this PR touches.

**Fix** (`tests/test_market_sentiment.py`, one line): changed the sentinel from `0.0` to `-1e9`, so
the due-check is satisfied regardless of what `time.monotonic()` happens to be on the host — no
behavior change to `src/market_sentiment.py` itself, test-only. Full suite re-run clean: 3653
passed, 0 failed.

This file isn't in the directional lane's file list (`src/paper_trading.py`,
`src/scientific_strategy.py`, `src/entry_checklist.py`, `src/live_trading.py`,
`src/pairs_strategy.py`, `src/orderflow_ws.py`, `src/indicators.py`), so noting the cross-lane
touch here per the coordination rule: `src/market_sentiment.py` / its tests aren't explicitly
owned in the lane map either (not in directional, not in the brain/risk/observability list) —
treating this as a shared-infra test-flakiness fix rather than a lane-owned change, since it's
one line, test-only, and was blocking this PR's CI. Applied on this PR's branch since that's
where the failure surfaced and this run is already watching it for exactly this reason.

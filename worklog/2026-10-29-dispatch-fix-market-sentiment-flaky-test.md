---
date: 2026-10-29
agent: dispatch
branch: dispatch/lane-status-pass-1019
pr: 166
lane: shared (market_sentiment.py/tests — not on the lane map; CI-red fix on an open PR this agent owns)
files: [tests/test_market_sentiment.py]
---

# CI red on PR #166: `TestTickRefreshCadence` used an absolute-clock timestamp instead of a relative delta

## What happened

PR #166 (this agent's own open status-pass PR, docs-only until now) got a `check_run.completed`
failure event: `test (3.11)` red on
`tests/test_market_sentiment.py::TestTickRefreshCadence::test_rebuilds_snapshot_and_checks_alerts_when_any_fetch_runs`
— `assert built["n"] == 1` got `0`. Per the PR-babysitting rules, red CI on an owned PR is work
now regardless of what else is going on, so this run stopped the daily status-pass loop to
investigate instead of letting it ride.

## Why this isn't PR #166's fault

Every commit on this branch before this one is `worklog/*.md` only — no `src/` or `tests/`
change could plausibly touch `market_sentiment.py` or its tests. `master` hasn't moved since
2026-10-15 (`b0bb82e`), so nothing upstream changed either. And this exact test had been part
of the "3639 passed, 0 failed" count on every one of this PR's own CI runs from 2026-10-19
through 2026-10-28 (ten runs) — it only started failing on run #11, with no code change in
between.

## Root cause

`tests/test_market_sentiment.py:317` (pre-fix):
```python
m._t_fg = m._t_cg = m._t_bc = 0.0  # all due
```
`SentimentMonitor._tick()` (`src/market_sentiment.py:134-140`) decides a source is "due" via
`time.monotonic() - self._t_fg >= _REFRESH_FG` (3600s for fear/greed). Setting the timestamp to
the absolute literal `0.0` only forces "due" if the **host's** `time.monotonic()` already
reads ≥ 3600 at the moment the test runs — true on a long-lived CI runner, false on a
freshly-booted one where the monotonic clock's epoch is recent. That's exactly the kind of
host/container-uptime-dependent non-determinism that produces an intermittent, not-code-caused
flake: same code, same test, different outcome depending on how long the CI VM had been up
before this job started. The two sibling tests in the same class
(`test_skips_all_fetches_when_nothing_due`, `test_fetches_only_the_source_past_its_refresh_window`)
already avoid this by computing `now = time.monotonic()` and using **relative** deltas
(`now`, `now - 61`) — this one test was the odd one out.

## Fix

Made the timestamp relative like its siblings, using the actual refresh constant instead of a
magic number:
```python
import time
m._t_fg = m._t_cg = m._t_bc = time.monotonic() - 10 * _REFRESH_FG  # all due
```
(`_REFRESH_FG` now imported from `src.market_sentiment` alongside `SentimentMonitor`/
`SentimentSnapshot`.) This is unconditionally "due" regardless of host uptime, matching the
intent the test's own comment already stated (`# all due`).

## Why this is in scope despite being outside dispatch's lane

`src/market_sentiment.py` / `tests/test_market_sentiment.py` isn't listed under either lane in
`WORKLOG.md`'s lane map (not directional-strategy, not brain/risk/observability) — it's
unowned. This is also a pure test-robustness fix with no behavior change to any gate, signal,
or cost filter; `src/market_sentiment.py` itself is untouched. Per the PR-babysitting rules, CI
red on a PR this agent opened is this agent's to resolve at every event regardless of lane,
and "port a fix / make the flaky test robust" is exactly the prescribed action when a re-run
isn't available (`actions_run_trigger: rerun_failed_jobs` returned `403 Resource not
accessible by integration` — no permission to re-run from this session).

## Verification

```
python -m pytest tests/test_market_sentiment.py -q   # 32 passed
python -m pytest tests/ -q                            # 3639 passed, 0 failed
```

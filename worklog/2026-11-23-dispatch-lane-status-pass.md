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

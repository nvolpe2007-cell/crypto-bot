---
date: 2026-09-19
agent: dispatch
branch: dispatch/live-trading-min-confidence-comment
pr: (opened this run)
lane: directional
files: [src/live_trading.py]
---

# `live_trading.py`'s "paper's 60" comment was stale; also — the directional lane's backlog is now the real bottleneck

## Context for this run

Fresh cloud session. `git fetch && checkout master && pull` fast-forwarded cleanly (through
PR #134). Read `CLAUDE.md`, `WORKLOG.md`, `python scripts/worklog_index.py`, and the last
three dispatch worklog entries in full (2026-09-15 status pass, 2026-09-16 `live_trading.py`
equity fix #131, 2026-09-17 `orderflow_ws.py` fix #133) plus yesterday's still-open PR #135
(`dispatch/fix-scientific-strategy-min-confidence-dead-param`, via `pull_request_read`, since
its worklog file isn't on master yet).

Pulled the live open-PR list via the GitHub MCP tools: **24 open PRs**, of which **16 touch
files in the directional lane** (#99–#135, minus a few already-closed/superseded ones per
#119/#127's own status passes). Every file this lane owns —
`paper_trading.py`, `scientific_strategy.py`, `entry_checklist.py`, `live_trading.py`,
`pairs_strategy.py`, `orderflow_ws.py`, `indicators.py` — has now had at least one full,
close read by a dispatch run in the last 30 days, and each already has 1-3 small findings
sitting as open, unmerged PRs.

## Option (a) — forward-test / journal analysis: still not reachable

Confirmed again (same as 2026-09-15 through 2026-09-18): `data/` is gitignored, this cloud
checkout has no copy of the VPS's real `trade_journal.csv` / `attribution.db` / arm-state
files, and `proof_scorecard.py` has nothing to score without them. Not actionable from here
without VPS access.

## What I found and fixed (option b — small, safe, doc-only)

Yesterday's PR #135 (still open) fixed the module docstring's wrong "60" figure in
`scientific_strategy.py` but explicitly deferred `live_trading.py`'s matching error to avoid
adding a second diff on top of PR #129 (`dispatch/live-trading-signal-exit-debounce`, still
open, doesn't touch these lines). Picked that up:

- `src/live_trading.py` module docstring: `"Min confidence: 70 (higher bar than paper's 60)"`
- `LIVE_MIN_CONFIDENCE = 70.0   # higher bar than paper (60) — real money`

Paper's actual min-confidence is not a constant 60 — it's `paper_trading._adapt['min_confidence']`,
which starts at 35.0 and adapts between 35 and 45 based on win/loss streaks
(`src/paper_trading.py:116,157-166`). Neither comment has matched the real value since the
adaptive mechanism was added. Fixed both to point at the real range and the mechanism name,
instead of a stale hardcoded number. Zero behavior change — `LIVE_MIN_CONFIDENCE` itself is
untouched, this is comment/docstring text only, no gate loosened or tightened.

## Verification

`python -m pytest tests/ -q` → **3608 passed, 0 failed** (comment-only change, no test
changes needed or added).

## The actual finding this run: the lane's bottleneck is now the PR backlog, not missing bugs

This is the fourth consecutive dispatch run (2026-09-15, -16, -17, -18, and now -19) to
report the same shape of result: the directional lane's seven owned files have all been
read closely and repeatedly, and the marginal safe finding has shrunk from real logic bugs
(#131 equity accounting, #133 epoch-age default) to a second stale-comment fix on the same
two lines a prior run explicitly declined to touch. 16 open, unreviewed, unmerged PRs now
sit in this lane alone (some over 4 weeks old), all green, all merge-cleanly against current
master (spot-checked #100, #105, #121, #129, #135 — no conflicts). Continuing to run dispatch
against an ever-growing unmerged backlog has diminishing value: each run either re-confirms
prior findings or shaves a smaller sliver off the same handful of already-flagged files.
Flagging for the owner rather than acting on it myself — merging (even just the doc/test-only
ones: #101, #104, #126, #128, #135, and this one) is outside dispatch's remit (no push to
master, no merge rights), but is very likely the single highest-leverage next step available
for this lane right now.

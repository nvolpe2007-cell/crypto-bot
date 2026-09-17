---
date: 2026-08-25
agent: dispatch
branch: dispatch/warn-enable-shorts-noop
pr: 103
lane: directional
files: [src/live_trading.py, tests/test_live_trading.py]
---

# ENABLE_SHORTS was a dead flag on the live (real-money) path

## What I did this run

Started with a fetch + read of `WORKLOG.md` / `worklog/`. No live paper-trading
data is available in this sandbox (`data/` is gitignored, lives on the VPS only,
and this session has no VPS/SSH access), so option (a) — analyzing recent forward-test
results — wasn't executable this run. There are also three open, unmerged dispatch PRs
already in flight (#100 CVD trend sign fix, #101 OFI veto strictness docs+test, #102
dual-direction probe no-op fix) — all still drafts, none touched by a human yet.

Given that, I spent this run on option (b): auditing the directional lane
(`src/paper_trading.py`, `src/scientific_strategy.py`, `src/entry_checklist.py`,
`src/live_trading.py`, `src/pairs_strategy.py`, `src/orderflow_ws.py`,
`src/indicators.py`) for bug patterns similar to the three already-found ones
(silent no-ops from assigning to read-only properties, sign/semantic mismatches,
doc-vs-code drift), via a dedicated read-only audit subagent, to make sure any new
work wouldn't duplicate what's already sitting in those three PRs.

**Audit result:** no unclaimed instances of those three specific bug patterns
remain in the lane on top of master — the audit independently re-derived the same
three findings already covered by #100/#101/#102 and found nothing new of that
shape. It did surface one concrete, verifiable, low-risk issue: `ENABLE_SHORTS` in
`src/live_trading.py` (the **live**, real-money engine — distinct from
`src/paper_trading.py`) is read from the environment at session start but was never
wired to any short-entry or short-exit logic anywhere in the file — the main loop
only opens/closes longs, and the SL/TP watcher only calls `close_long`. The module
docstring described it as an enable-able feature ("shorts can be enabled via
ENABLE_SHORTS=true"), so an operator who sets that flag on the live path gets
silent zero effect, easy to misread as "no short setups appeared" rather than "the
flag does nothing."

I judged this **not** worth wiring up shorts (that's a real feature addition on the
real-money path, out of scope for a small/testable fix and explicitly gated in the
docstring on "a proven live track record" — not something to add unasked). Instead:
made the inert flag observable and corrected the docstring.

## Change

- `_warn_if_shorts_unimplemented(enable_shorts)` in `src/live_trading.py`, called
  once at session start: logs a warning if `ENABLE_SHORTS=true` but the flag is
  inert. No trading logic, thresholds, or gates touched — this is pure
  observability, doesn't loosen or tighten anything.
- Docstring corrected to state shorts aren't implemented instead of implying a
  working toggle.
- 2 new tests (`TestWarnIfShortsUnimplemented`) in `tests/test_live_trading.py`.

## Verification

`python -m pytest tests/ -q`: 3579 passed, 4 failed. The 4 failures are the
pre-existing `test_bot_main.py` collection issues (`AttributeError: ... has no
attribute '_run_funding_scanner'`) already flagged by open PRs #100/#101 —
unrelated to this change, present on master before this branch.

## For the next agent

- PRs #100, #101, #102, and now #103 are all open dispatch drafts, none merged or
  reviewed by a human yet. Before spending a run re-auditing the same lane for the
  same bug shapes, check whether any of these landed — a fresh pass otherwise just
  re-finds what's already sitting in review.
- Live paper-trading / forward-test data isn't reachable from this sandbox (no VPS
  access). Option (a) of the dispatch prompt (analyze recent forward-test results)
  needs either VPS access wired into the cloud session or someone pasting recent
  `proof_scorecard.py` / `trade_report.py` output into the conversation.

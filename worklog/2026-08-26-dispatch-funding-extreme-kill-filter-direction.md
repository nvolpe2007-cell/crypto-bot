---
date: 2026-08-26
agent: dispatch
branch: dispatch/funding-extreme-kill-filter-direction
pr: TBD
lane: directional
files: [src/paper_trading.py, tests/test_paper_trading.py]
---

# `_kill_filter_skip`'s funding-extreme veto is direction-agnostic — documented + pinned, not changed

## What I found

`src/paper_trading.py`'s `_kill_filter_skip` (a hard veto wired into both `build_long_checklist`
and `build_short_checklist` via `entry_checklist._kill_filter`) blocks entries whenever
`abs(funding_rate) > 0.001` (0.1%/8h), **regardless of `side`**. Its inline comment said
"when paying >0.1% per 8h, longs are very expensive" — implying it exists to protect the
side that's paying the rate.

But `entry_checklist._funding_favorable` (a *soft* check in the same checklists) is
explicitly side-aware: for `side == "buy"` only `fr > 0.0005` is unfavorable; for
`side == "sell"` only `fr < -0.0005` is unfavorable — i.e. it only penalizes the side
actually paying, never the side collecting.

So the two funding checks in the same pipeline disagree on whether an extreme funding
rate should matter to the side that would *collect* it, not pay it. Concretely: with
`fr = +0.0015` (longs paying 0.15%/8h, shorts collecting it), a SELL/short signal passes
`_funding_favorable`'s soft check cleanly but is unconditionally vetoed by the hard
`_kill_filter_skip` — even though the short is the side funding favors here. Symmetric
case for very negative `fr` blocking a favorable long.

`git log -S "FUNDING_EXTREME" -- src/paper_trading.py` shows this line predates the
short-side entry path (`_kill_filter_skip` already took a `side` param used by every
*other* check inside it — book imbalance, CVD divergence, microprice, rejection — just
not the funding check), consistent with it being written longs-only and never revisited
when shorts were wired in.

## Why I didn't change the behavior

Two readings are both defensible:
1. **Bug** — the hard veto should mirror `_funding_favorable`'s side-awareness; it's
   currently blocking favorable trades for no reason.
2. **Intentional** — an extreme funding print in *either* direction is itself a
   crowded/deleveraging-regime signal (see `FUNDING_ARB_REGIME_VETO_FRAC` elsewhere in
   this repo, which treats extreme funding symmetrically as a risk-off trigger), so a
   hard hold-both-sides veto could be deliberate risk policy rather than a mistake.

CLAUDE.md's Core Principle says not to loosen cost/EV-aware gates without forward-test
evidence, and making this side-aware would strictly loosen it (unblocks one side on
every extreme-funding print). I have no VPS/journal access from this sandbox to check
whether the historical record supports either reading, and a scheduled run with no
human in the loop shouldn't make that call unilaterally — same reasoning the prior
`dispatch/document-ofi-veto-strictness-mismatch` run (PR #101) used for an analogous
finding.

## What I did instead

- Extracted the funding-extreme check out of the `_kill_filter_skip` closure into a
  module-level, independently testable `_funding_extreme_blocks(fr, side, threshold=0.001)`
  in `src/paper_trading.py` — same logic, `side` accepted but deliberately unused, with a
  docstring explaining why (pointing here) so the mismatch can't silently drift further.
- Fixed the misleading inline comment at the call site.
- Added `TestFundingExtremeBlocks` (6 tests) in `tests/test_paper_trading.py` pinning
  current behavior, including two tests that explicitly demonstrate the mismatch
  (`test_also_blocks_sell_when_longs_are_paying`, `test_also_blocks_buy_when_shorts_are_paying`)
  so a future deliberate change shows up as an explicit, visible diff to these tests.

**Recommended follow-up for an agent with forward-test/journal access:** check whether
trades skipped for `FUNDING_EXTREME` on the side that would have collected the funding
(look for `[SKIP {BUY,SELL}] ... kill_filter: FUNDING_EXTREME` in logs / funnel stats)
would have been net-positive if allowed through. If so, make `_funding_extreme_blocks`
side-aware (only block the paying side, mirroring `_funding_favorable`); if the record
shows extreme-funding prints predict adverse moves regardless of direction, close this
out as "confirmed intentional" and remove the ambiguity note.

## Verification

- `python -m pytest tests/test_paper_trading.py -q` → 87 passed (was 81).
- `python -m pytest tests/ -q` → 3583 passed, 4 failed. Same 4 pre-existing
  `tests/test_bot_main.py::TestMainSubsystemIsolation::*` failures already flagged by
  PRs #100-#103 (`AttributeError: module 'src.bot' has no attribute '_run_funding_scanner'`,
  `src/bot.py` outside this lane, unrelated to this change).

**Note on the other open dispatch PRs:** #100, #101, #102, #103 are all still open
(draft, unmerged) as of this run — no action needed from me, just flagging that four
directional-lane audit fixes are queued waiting on review/merge.

---
date: 2026-09-29
agent: dispatch
branch: dispatch/entry-checklist-missing-regime-long-block
pr: (opened this run)
lane: directional
files: [src/entry_checklist.py, tests/test_entry_checklist.py]
---

# `build_long_checklist()` had no hard regime veto — the short side has one, the long side didn't

## What I did this run

1. `git fetch && checkout master && pull` — fast-forwarded 11 commits (through PR #146,
   all polymarket/GEX work outside my lane — no directional-lane file changed on master).
2. Read `CLAUDE.md`, `WORKLOG.md`, `python scripts/worklog_index.py`.
3. Checked the open PR backlog via GitHub MCP: **29 open PRs, 19+ of them directional-lane
   `dispatch/*` drafts**, oldest (#100) now 40 days old, none merged since #134 (~09-17).
   Confirmed with a fresh listing that nothing changed since the 2026-09-25 status-pass
   worklog (`worklog/2026-09-25-dispatch-backlog-status-pass.md`, on branch
   `dispatch/pr-backlog-status-pass-0925`, PR #142) which already reported this exact
   backlog and already sent the owner a notification about it — situation is unchanged
   (not worse in any way that would justify repeating that notification), so I did not
   re-notify and proceeded to find genuinely new work instead, per that run's own
   conclusion that dispatch should keep producing verified fixes regardless of merge
   velocity.
4. Delegated a focused read-only hunt (Explore subagent) across all 7 lane files
   (`paper_trading.py`, `scientific_strategy.py`, `entry_checklist.py`, `live_trading.py`,
   `pairs_strategy.py`, `orderflow_ws.py`, `indicators.py`), explicitly excluding the 21
   issues already covered by PRs #100–#148, with extra attention on `paper_trading.py`'s
   equity/sizing arithmetic (the P&L-accounting bug family from #131/#140/#148) and any
   long/short asymmetry.

## What I found

`src/entry_checklist.py`'s `build_short_checklist()` has a hard veto,
`Check("regime_short_block", "hard", _regime_short_block)`, that blocks a new short while
`regime_name == "TRENDING_UP"` (adverse for shorts). `build_long_checklist()` had **no
symmetric veto at all** — nothing referencing `regime_name` in the long checklist.

This is a real, if currently dormant, gap:

- `src/regime_detector.py`'s `RegimeResult.allows_long` (line 53-54) already encodes
  exactly the missing predicate: `self.regime not in ('CRASH', 'TRENDING_DOWN')`. Grepped
  the whole repo — this property is read only by its own `to_dict()`; no gate anywhere
  consults it. Same "computed but never wired in" pattern as #14 (`ofi_min`), a different
  instance, different file.
- `RegimeResult.strategy_hint` for `CRASH` is `'Stay flat — wait for stabilisation'`, and
  `PersistentRegime` treats `CRASH` as urgent enough to bypass its own whipsaw filter and
  engage immediately — the codebase already treats CRASH as the most dangerous regime, yet
  nothing stopped a **new long** entry from opening mid-crash as long as it cleared
  `min_confidence` and the soft-score threshold.
- Confirmed via `git log --follow -p` that a long-side counterpart to
  `_regime_short_block` was never added, not previously tried and reverted, and not one of
  the "investigated and concluded intentional" items (unlike #6, the funding-kill-filter
  side asymmetry).
- Confirmed `long_ctx` in `paper_trading.py` (~line 1966-1989) already carries
  `regime_name` into `CheckContext` for every long entry — the data was already there,
  the checklist factory just never used it.
- Confirmed this is the directional entry path (`long_checklist.run(long_ctx)` at
  `paper_trading.py:1990`), gated by `DIRECTIONAL_ENABLED` (default `0`) — no live
  behavioral impact today, but a real correctness gap that will matter the moment that
  path (or the microstructure re-test that shares this gate module) is re-enabled.
- Checked `live_trading.py` for a similar wiring gap: it does **not** import
  `entry_checklist.py` at all, so this fix has no live-money surface.
- Checked `paper_trading.py`'s equity/sizing math per the P&L-bug pattern from
  #131/#140/#148: `compute_position_size()` reads the same freshly-summed
  `total_equity` (all positions' unrealized P&L included) on both the long and short
  paths — no sibling bug found there this run.

## Fix

Added `_regime_long_block()`, mirroring `_regime_short_block()`:
```python
def _regime_long_block(ctx: CheckContext):
    if ctx.regime_name in ("CRASH", "TRENDING_DOWN"):
        return False, f"{ctx.regime_name} blocks longs"
    return True, ctx.regime_name
```
and registered it as `Check("regime_long_block", "hard", _regime_long_block)` in
`build_long_checklist()`, in the same checklist position `regime_short_block` occupies in
`build_short_checklist()` (right after `ofi_aligned`).

This **tightens** an existing gate family (adds the missing half of a veto pattern that
already exists on the other side) rather than loosening anything — consistent with
CLAUDE.md's core principle. It does not touch `atr_alive`, sizing, or any cost filter, and
does not change short-side behavior (`regime_short_block` and `_regime_short_block` are
untouched; a new test asserts `build_short_checklist()` still does *not* carry
`regime_long_block`).

## Tests

Added to `tests/test_entry_checklist.py`:
- `TestRegimeLongBlock` (5 cases): blocks CRASH, blocks TRENDING_DOWN, allows RANGING /
  TRENDING_UP / VOLATILE — mirrors the existing `TestRegimeShortBlock` structure exactly.
- `TestBuildLongChecklist`: `test_has_regime_long_block`, `test_regime_long_block_is_hard`,
  `test_blocks_crash_long` (checklist-level, asserts `"regime_long_block" in
  result.failed_hard`), `test_allows_long_in_ranging`; added `regime_long_block` to the
  existing `test_all_hard_checks_are_hard` hard-names set.
- `TestBuildShortChecklist`: `test_does_not_have_regime_long_block`, symmetric with the
  pre-existing `test_does_not_have_regime_short_block` on the long-checklist test class.

`python -m pytest tests/test_entry_checklist.py -q` → **159 passed** (was 149).
`python -m pytest tests/ -q` (full suite) → **3644 passed, 0 failed** (baseline 3634 + 10
new tests, no regressions — confirms no existing test relied on a long entry passing while
`regime_name` was `CRASH`/`TRENDING_DOWN`).

## Backlog note (unchanged from #142 / 2026-09-25)

Still ~19-20 directional-lane PRs open as unconverted GitHub drafts, oldest 40 days,
nothing merged into master since #134. That run already notified the owner directly about
this; not repeating the notification here since nothing has changed. Continuing to produce
verified, tested, narrowly-scoped fixes per that run's conclusion — the value of doing so
doesn't depend on merge velocity, and if/when the backlog does get triaged, having more
correct small fixes ready is strictly better than fewer.

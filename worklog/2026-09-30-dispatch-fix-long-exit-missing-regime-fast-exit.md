---
date: 2026-09-30
agent: dispatch
branch: dispatch/fix-long-exit-missing-regime-fast-exit
pr: (opened this run)
lane: directional
files: [src/paper_trading.py, tests/test_paper_trading.py]
---

# EXIT LONG had no regime fast-exit; EXIT SHORT did — the pair was asymmetric

## What I did this run

1. `git fetch && checkout master && pull` — fast-forwarded 11 commits (through PR #146,
   all polymarket/GEX work outside my lane; no directional-lane file touched on master
   since the 2026-09-29 run).
2. Read `CLAUDE.md`, `WORKLOG.md`, `python scripts/worklog_index.py`, and the last three
   dispatch worklog entries (2026-09-15, 09-16, 09-17) plus fetched the still-open
   `dispatch/entry-checklist-missing-regime-long-block` branch (PR #149, 2026-09-29) to see
   what the immediately-prior run already covered.
3. Checked the open PR backlog via GitHub MCP: 30 open PRs, ~22 of them directional-lane
   `dispatch/*` drafts (#100-#149), oldest now 41 days unmerged. The last "please merge the
   backlog" notice was already sent on 2026-09-25 (PR #142) and re-confirmed unchanged but
   *not* re-sent on 2026-09-29 (PR #149) since nothing had changed since. Same call this
   run — situation is unchanged (a batch of unrelated PRs, #143-#147, did merge on
   2026-09-28, so the owner is merging periodically; the directional-lane backlog
   specifically just hasn't been in that batch yet). Not re-notifying; proceeding straight
   to finding new work, per the 2026-09-29 run's own reasoning.
4. Delegated a focused read-only hunt (Explore subagent) across all 7 lane files, explicitly
   excluding the 22 issues already covered by PRs #100-#149 (full list given to the
   subagent), with extra attention on `paper_trading.py` (2891 lines — the actual live
   loop, least recently mined of the lane files despite being the most important one) and
   any long/short asymmetry, since three of the last five accepted fixes (#131, #140, #148,
   #149) were exactly that pattern: a check or accounting rule wired for one side of a
   position but not its mirror image.

## What I found

`run_paper_trading_session`'s signal-driven exit branch (`src/paper_trading.py`, EXIT
SHORT, pre-fix ~line 2260) fast-exits a short immediately — bypassing the
`SIGNAL_EXIT_STREAK` debounce — whenever `regime_name == 'TRENDING_UP'`, independent of
`sig.signal`:

```python
elif pos_side == 'short' and (sig.signal == Signal.BUY or regime_name == 'TRENDING_UP'):
    fast_exit = regime_name == 'TRENDING_UP'
    ...
```

The EXIT LONG branch immediately above it had no equivalent clause — only an explicit SELL
signal (still gated by the debounce) could close a long; a regime flip to `TRENDING_DOWN`
while `sig.signal` stayed HOLD never triggered an exit there at all, fast or debounced.

Confirmed this is a real gap, not an intentional asymmetry, by cross-checking every other
place this codebase treats `TRENDING_UP`/`TRENDING_DOWN` as a matched adverse pair:
- `regime_detector.RegimeResult.allows_long` → `regime not in ('CRASH', 'TRENDING_DOWN')`
  (TRENDING_DOWN is explicitly "don't hold a long").
- `entry_checklist._regime_short_block` blocks new shorts on `TRENDING_UP`; its sibling
  `_regime_long_block` (added by the immediately-prior run, PR #149, still unmerged) blocks
  new longs on `CRASH`/`TRENDING_DOWN` — the entry side of this exact pairing was just
  fixed one day before this run, on the *entry* gate; this fix is the same pairing gap on
  the *exit* side, in a different code path the 2026-09-29 run didn't touch (it only edited
  `entry_checklist.py`).
- `paper_trading.py`'s own `_diagnose()` (line ~210) treats `(TRENDING_UP, buy)` and
  `(TRENDING_DOWN, short/sell)` as the matched "regime aligned" pair.
- No comment anywhere explains why the exit fast-path singles out TRENDING_UP for shorts
  only; reads as the short-exit branch being written first and the long-exit mirror clause
  never added. Confirmed via `git branch -a` / diff that none of the ~30 open `dispatch/*`
  branches touch this code.

Reachability: dormant today, not silently live. This whole block sits behind `if not
DIRECTIONAL_ENABLED: continue` (`DIRECTIONAL_ENABLED` defaults to `0` — the directional
engine is shelved per CLAUDE.md, t=-8.82). Production open-position risk management
currently comes only from the always-on `_sltp_watcher` (fixed SL/TP + `trailing_stop.py`,
confirmed symmetric) and the microstructure `check_exit()` (also symmetric) — neither of
which is affected by this bug. This gap would only matter the moment `DIRECTIONAL_ENABLED=1`
is set to re-run this shelved engine as a forward test: longs would silently ride out a
TRENDING_DOWN flip that shorts already escape, understating long-side downside risk in any
such test.

## Fix

Added `_regime_forces_exit(pos_side, regime_name) -> bool`, a pure helper mirroring the
short side's existing hard-coded condition:

```python
def _regime_forces_exit(pos_side: str, regime_name: str) -> bool:
    if pos_side == 'buy':
        return regime_name == 'TRENDING_DOWN'
    if pos_side == 'short':
        return regime_name == 'TRENDING_UP'
    return False
```

Both EXIT LONG and EXIT SHORT now call it symmetrically (`_regime_forces_exit('buy', ...)`
/ `_regime_forces_exit('short', ...)`) to decide both whether to trigger and whether the
trigger is a `fast_exit` (bypasses the debounce). This **tightens** the exit side of a gate
pair that already existed for shorts — it doesn't loosen any entry gate, doesn't touch
`atr_alive`, sizing, or cost accounting, and doesn't change short-side exit timing at all
(same condition, same `fast_exit` boolean, just routed through the shared helper instead of
being hard-coded twice). Consistent with CLAUDE.md's core principle and with the pattern
PR #149 used one day earlier for the entry-side version of this same pairing.

## Tests

New `TestRegimeForcesExit` class in `tests/test_paper_trading.py` (8 cases via
parametrize): long forced out on TRENDING_DOWN, short forced out on TRENDING_UP, long *not*
forced out on TRENDING_UP, short *not* forced out on TRENDING_DOWN, neither side forced out
on RANGING/VOLATILE/CRASH/UNKNOWN, and an unknown `pos_side` never forces an exit.

`python -m pytest tests/test_paper_trading.py -q` → 90 passed (was 84 on a clean master).
`python -m pytest tests/ -q` (full suite) → **3643 passed, 0 failed** — no regressions, and
no pre-existing-failure count to reconcile (master is fully green, as prior runs already
found; CLAUDE.md's "2 known pre-existing fails" note remains stale, not re-touched here).

## Backlog note (unchanged in substance from 2026-09-25 / 2026-09-29)

~22 directional-lane PRs are open and unmerged (now 23 with this one), oldest 41 days.
#143-#147 (a different lane) did merge on 2026-09-28, so merges are happening in batches —
just not this lane's batch yet. Not re-flagging as a fresh notice since nothing material
changed since the last one; noting only for continuity so a future run doesn't have to
re-derive it.

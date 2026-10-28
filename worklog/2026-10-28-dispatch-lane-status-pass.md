---
date: 2026-10-28
agent: dispatch
branch: dispatch/lane-status-pass-1019
pr: 166 (reused)
lane: directional
files: []
---

# Directional lane: 13 days / 0 merges continues; chased a suspected regression in an open PR to ground, found it's actually correct

## What I did this run

Per CLAUDE.md option (c): read-only status/health pass. No `src/` or `tests/` changes.

1. `git fetch && checkout master && pull` — up to date, head still `b0bb82e` (last merge into
   master still #163/#164 from 2026-10-15 — **now 13 days / 0 merges**). PR #100 (oldest open,
   `dispatch/fix-cvd-trend-sign-semantics`) is now **69 days old**. One new PR appeared since
   yesterday — `research/kalshi-15m-paper` (#167, opened today by the account owner, not
   dispatch) — outside this lane, not reviewed here.
2. Rather than re-read the same files again, picked the open PR this lane hasn't independently
   re-verified yet — `dispatch/fix-prob-model-version-persistence-gap` (#161) — and tried to
   break it. First pass looked like a real find: `_load_open_positions` passes
   `prob_model_version=int(p.get(...))` straight into `PaperPosition(...)`'s constructor, and
   every *other* place the codebase reads this attribute (`calibration.py`, `expectancy_gate.py`)
   does it defensively via `getattr(r, 'prob_model_version', 0)` — which only makes sense if the
   attribute ISN'T a declared dataclass field. Reproduced the exact `TypeError` with a minimal
   dataclass missing that field, and traced that `position.prob_model_version = PROB_MODEL_VERSION`
   in `paper_trading.py`'s entry path only fires `if reasoning:` (i.e. **not** when the
   probability gate is disabled) — so if this field weren't declared, both save and load would
   break for every open position the next time the bot restarts with the gate off.
   **Checked the full diff, not just the lane files, before filing anything**: PR #161 *does*
   add `prob_model_version: int = 0` as a real field on `PaperPosition` in `src/paper_trader.py`
   (a one-line, correctly-noted cross-lane edit) — so the constructor call is valid and the
   getattr-defensiveness elsewhere is just unrelated legacy caution, not a tell. Checked out the
   branch and ran `pytest tests/test_position_persistence.py` directly: 50/50 pass, including
   the two new tests for this exact field. **No bug — PR #161 is correct as written.** Logged
   here instead of silently dropping it, since "I suspected X, checked, X doesn't hold" is worth
   one sentence for whoever reviews #161 next (don't re-litigate the getattr pattern, it was
   already considered and isn't needed here).
3. `python -m pytest tests/ -q` → **3639 passed, 0 failed.** Unchanged since 2026-10-16.

## Why no re-escalation this run

Same reasoning as every entry since 2026-10-19: no merge, no review comment, no backlog shape
change since the 2026-10-15 push notification. One day staler, nothing qualitatively new.
Reusing PR #166 rather than opening #168.

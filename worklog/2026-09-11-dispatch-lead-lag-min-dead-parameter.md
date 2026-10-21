---
date: 2026-09-11
agent: dispatch
branch: dispatch/document-lead-lag-min-dead-parameter
pr: TBD
lane: directional
files: [src/scientific_strategy.py, tests/test_scientific_strategy.py]
---

# `ScientificStrategy.lead_lag_min` is a dead parameter — documented, not fixed, and why

## Start-of-run status check

`git fetch && git pull` — master had moved 11 commits since this routine's last
recorded activity (PRs #125, #124, #122, #113, #118 landed; none touch the
directional lane). Read `CLAUDE.md` and `WORKLOG.md`, then
`python scripts/worklog_index.py` for recent entries.

`python -m pytest tests/ -q` on master (after `pip install fastapi` — not preinstalled
in this sandbox's base image, unrelated to the repo) → **3584 passed, 0 failed**,
matching PR #119's last-reported baseline.

Checked the full directional-lane PR backlog (#100–#105, #109, #121, #123 — 8 small,
independently tested fixes, all still open, zero human review) via `git merge-tree`
against current master: **all 8 still merge cleanly**, no rebases needed this run
(unlike day 11 / PR #119, which needed one). No new PR comments or reviews on any of
them — the two PRs whose `updated_at` looked fresher (#102, #121) turned out to be
automated "merge master in" commits keeping them mergeable, not human engagement.
Given that plus the day-11 status pass three days ago already reported the
CI-now-green / #106-#118 duplication finding, there was nothing new to escalate via
notification this run — reporting "still nothing, still waiting" isn't useful signal.

## What this run did instead: option (b), carefully bounded

Forward-test/paper-trading analysis (option a) isn't reachable from this sandbox —
`data/` (trade journal, arm state JSON) lives only on the VPS and is gitignored, no
SSH access here. So: looked for one more small, provably-correct wiring bug in the
lane files, in the style of the already-fixed `ofi_min` bug (#121) and its siblings.

Delegated a focused audit (Explore subagent) across all 7 lane files, explicitly
given the list of bugs already fixed by open PRs so it wouldn't re-flag them. It
surfaced `ScientificStrategy.lead_lag_min`: accepted by `__init__`, stored on
`self`, never read again — the lead-lag score/direction logic
(`_has_buy_signal`/`_has_sell_signal`, the `lead_lag_score` block) uses `lead_dir`
and `lead_strength` unconditionally, at any strength, with no comparison against
`self.lead_lag_min` anywhere. Structurally identical shape to the `ofi_min` bug.

**But it is not the same bug, and I did not fix it the same way.** I checked git
history (`git log --all -S lead_lag_min -- src/scientific_strategy.py`) back to the
parameter's introduction in `3bf3444`. Unlike `ofi_min` — which was verifiably
*working* until a later commit hardcoded a `0.15` literal that shadowed it — the
`git show <every-relevant-commit>:src/scientific_strategy.py | grep lead_lag_min`
trail shows `lead_lag_min` has **only ever been stored, never read**, at every point
in this file's history, including the pre-simplification version before the
2026-06-15 rewrite (`54675f4`) that explicitly marked it `# ignored, kept for
compat`. There is no prior-good state to restore to, so this isn't a regression a
mechanical one-line fix (mirroring `self.ofi_min` gating `ofi_dir`) can honestly
claim to "restore."

It also isn't a same-shape fix on the merits: `LeadLagDetector.get_strength()`
returns a `0.0–1.0` time/size-decay factor (`src/lead_lag_detector.py`), not a raw
percentage. `lead_lag_min`'s default (`0.003` = 0.30%) reads like a raw
BTC-move-percentage threshold — the same 0.30% figure the module's own docstring
uses for its original (now-loosened-to-0.20%) move threshold. Gating
`lead_strength >= self.lead_lag_min` would be **almost always true** in practice
(size_factor saturates at 1.0 above the detector's own 0.20% threshold; only the
last ~0.3% of the 3-minute decay window would ever fail it), so that comparison
wouldn't actually implement what the parameter's name and default value suggest it
was meant to do. A correct fix needs either a new `LeadLagDetector` accessor
exposing the raw move magnitude, or a deliberate redefinition of what
`lead_lag_min` gates — a design decision, not a bugfix.

## What I did instead

Added a code comment at both the constructor and the read site in
`src/scientific_strategy.py`, explaining the above so the next agent (or a future
run of this one) doesn't either (a) waste time re-discovering this, or (b) "fix" it
with the wrong-units comparison and silently start suppressing lead-lag signals in a
way nobody reviewed. Added `TestLeadLagMinHasNoEffect` (2 tests) to
`tests/test_scientific_strategy.py` pinning the current behavior — a signal fires at
`lead_strength=0.0001` regardless of `lead_lag_min`, and raising `lead_lag_min` to
`0.99` changes nothing — so any future change to wire it in shows up as a failing
test requiring a deliberate update, not a silent behavior change.

No gate was loosened or tightened. No trading logic changed.

## Verification

- `python -m pytest tests/test_scientific_strategy.py -k LeadLagMin -v` → 2 passed.
- `python -m pytest tests/ -q` → **3586 passed, 0 failed** (3584 + 2 new tests).

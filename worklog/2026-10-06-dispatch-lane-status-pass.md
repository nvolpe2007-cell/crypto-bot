---
date: 2026-10-06
agent: dispatch
branch: (none — no code change)
pr: n/a
lane: directional
files: []
---

# Status pass — backlog now 29 PRs touching the lane, zero merged in 47 days, draft state is the likely mechanism

Read-only run. No code change, no new fix PR opened.

## What I checked

- `git fetch` + `master` pulled clean, nothing new since PR #146 (#147/#148.../#155 are
  all still open, none merged).
- Installed deps fresh (`numpy`/`pandas`/`scipy`/`fastapi`/pytest weren't preinstalled in
  this container) and ran the full suite: **3634 passed, 0 failed**, clean collection —
  same green state #153 reported three days ago.
- Counted open PRs touching the 7 directional-lane files
  (`paper_trading.py`, `scientific_strategy.py`, `entry_checklist.py`, `live_trading.py`,
  `pairs_strategy.py`, `orderflow_ws.py`, `indicators.py`): **29 open PRs** (#100–#155,
  all but a couple still `draft: true`), oldest (#100) now **47 days** unmerged.
- `git log --all --since=2026-08-20 -- <7 lane files> | grep Merge` → **zero** merge
  commits land any lane-file change into `master`. In the same window, 9 other PRs
  *did* merge to master (#122, #124, #125, #132, #134, #144, #145, #146, #147) — all
  polymarket/VPS-migration/docs work outside this lane. The owner is actively merging;
  the directional lane specifically has not had a merge since before #100 opened.

## New observation this run (not in #139/#142/#152/#153)

#139 (2026-09-22) already named the proximate cause — "dispatch PRs sit as unconverted
drafts" — but didn't trace *why* they stay draft. Checked this run: every PR a cloud
dispatch session opens is created as **draft by standing harness instruction**, not by
repo or lane policy — dispatch has no way to open a non-draft PR. That means the backlog
won't self-clear by dispatch changing behavior; it only clears when a human marks PRs
"ready for review" (or reviews drafts directly) and merges. Two weeks after #139 flagged
this, the mechanism is confirmed and the backlog has grown (19 → 29 lane-touching PRs),
not shrunk.

## Why no 30th fix PR this run

Same reasoning as #152/#153, now with three consecutive confirmations: every directional-
lane bug a status pass can find by reading is already filed as an open, correctly-
diagnosed PR. The binding constraint is PR review/merge throughput, not supply — adding
another candidate fix lowers backlog signal-to-noise rather than raising it. This entry
still goes up as a docs-only PR (coordination requires it, so the next agent doesn't
repeat this same pass), but since #139/#142/#152/#153 making the same point as draft
PRs hasn't changed owner behavior, this run also pushes a direct notification to the
owner rather than relying on a 5th unreviewed draft PR to be noticed.

**Verification:** `python -m pytest tests/ -q` — 3634 passed, 0 failed. No source files
touched this run.

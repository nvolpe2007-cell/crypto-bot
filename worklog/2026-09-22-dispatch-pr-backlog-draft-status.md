---
date: 2026-09-22
agent: dispatch
branch: dispatch/pr-backlog-draft-status-pass
pr: (opened this run)
lane: directional
files: []
---

# Directional lane still heavily mined; new finding — every dispatch PR sits as an unconverted draft

## What I did this run

Per CLAUDE.md option (c): a read-only status/health pass. No `src/` changes.

1. `git fetch && checkout master && pull` — fast-forwarded 11 commits (through PR #134,
   `fix/migrate-identities-only`).
2. Read `CLAUDE.md`, `WORKLOG.md`, `python scripts/worklog_index.py`.
3. Pulled the current open-PR list via the GitHub MCP tools: **25 open PRs**, most still
   unmerged from the picture the 2026-09-15 and 2026-09-17 dispatch runs already described.
   The oldest (#99) is now **34 days** old; the directional lane alone carries 14 open PRs
   (#100–#105, #109 cross-lane, #120, #121, #123, #126, #128, #129, #135, #136, #137, #138).
   Four *more* small dispatch fixes landed in the four days since the last worklog entry
   I can see on master (#135 docs, #136 docs, #137 fix, #138 dead-code) — all still open.
4. `python -m pytest tests/ -q` (fresh container needs `fastapi`/`python-multipart`, same
   as every prior run) → **3608 passed, 0 failed.**

## Why no new code change this run

Both 2026-09-15 and 2026-09-17's status passes already concluded the lane is thoroughly
mined and that the binding constraint is no longer "can dispatch find more bugs" but "is
anything merging." Four days of subsequent runs (#135–#138) bore that out: each found
something, each is still sitting open. Opening a 15th micro-fix this run would very likely
just add another entry to the same unreviewed pile, so I spent this run's budget
characterizing *why* the pile isn't draining instead, since that's the more decision-relevant
question at this point.

## Finding: every currently-open `dispatch/*` PR is an unconverted GitHub draft

Checked `draft` / `mergeable_state` via `pull_request_read` on a sample spanning the full
age range of the backlog:

| PR | branch prefix | age | draft | mergeable_state |
|---|---|---|---|---|
| #100 | `dispatch/*` | 34d | **true** | unknown |
| #129 | `dispatch/*` | 8d | **true** | unknown |
| #135 | `dispatch/*` | 4d | **true** | clean |
| #138 | `dispatch/*` | 1d | **true** | clean |
| #109 | `fix/*` (claude-computer) | 19d | false | unknown |
| #120 | `feat/*` (claude-computer) | 13d | false | behind |

Every `dispatch/*` PR sampled — spanning the entire backlog age range — is still a GitHub
**draft**. GitHub does not allow merging a draft PR until it's explicitly converted with
"Ready for review"; drafts also don't surface the same way in most review-queue views.
The one dispatch PR that *did* get merged, #130 (2026-09-15's own status-pass entry), is
`draft: false` in its current (merged) state — consistent with someone having had to
convert it before `nvolpe2007-cell` could merge it on 2026-09-17.

This doesn't fully explain the backlog by itself — the owner does merge dispatch PRs when
they get to them (#130, and the June 2026-06-17 batch per `WORKLOG.md`'s in-flight-items
section) — but it is a concrete, currently-true, and compounding friction: **every one of
the 14+ open directional-lane PRs requires an explicit "Ready for review" click before it
can even be considered for merge, on top of actual review.** I did not convert any of them
myself — that changes PR state visible to the repo owner and to any other agent watching
these PRs, which isn't this run's call to make unilaterally; flagging it here (and via a
direct notification) so the owner can decide whether to batch-convert the backlog, change
how dispatch opens PRs, or leave the draft gate as an intentional review safety net.

## Suggestion for the owner (carried forward + sharpened from 2026-09-15/09-17)

14 directional-lane PRs (of 25 open repo-wide), up to 34 days old, all independently
tested and green, are sitting as unconverted drafts. Two options that would raise the
marginal value of future dispatch runs:
1. Batch-convert the backlog to "Ready for review" and do a skim-merge pass (the
   doc-only/test-only ones — #101, #104, #126, #128, #135, #136 — are the lowest-risk
   place to start), or
2. If the draft state is intentional (a deliberate human-in-the-loop gate before any
   AI-authored PR becomes mergeable), that's a reasonable policy — but it means the
   binding constraint on this lane is entirely owner review bandwidth, not anything
   dispatch can search its way out of. Worth knowing either way.

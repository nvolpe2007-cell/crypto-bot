---
date: 2026-11-02
agent: dispatch
branch: (none — read-only run, no PR)
pr: n/a
lane: directional
files: []
---

# Directional lane: suite fully green (3639/3639), but the review bottleneck is the real story now — only 1 dispatch fix PR has ever merged

## What I did this run

Per CLAUDE.md option (c): a read-only status/health pass. No code changes.

1. `git fetch && checkout master && pull` — up to date, last merge is #163
   (`dispatch/lane-status-pass-1015`, itself a prior no-op status pass) plus #164
   (`feat/dashboard-greg-paper-book`, out of lane).
2. Read `WORKLOG.md` + `worklog/` + `scripts/worklog_index.py` output — no dispatch fix
   PR has landed on master since the 2026-09-17 `orderflow-data-age-secs` entry, even
   though many more `dispatch/fix-*` PRs exist.
3. Pulled the full open-PR list (GitHub MCP, 42 open PRs total as of today):
   - **32 of 42** open PRs touch the 7 directional-lane files by title/branch
     (`dispatch/fix-*`, `dispatch/document-*`, `dispatch/live-trading-*`, `dispatch/entry-checklist-*`).
   - They span **PR #100 (2026-08-20) through #168 (2026-10-31)** — 73 days, growing from
     29 (2026-10-06, PR #156's count) → 36 (2026-10-15) → 42 today. ~6 new PRs every 2 weeks,
     0 merged in that window.
   - PR #100 (the oldest *fix* PR; #99 `stockbot-orb-alerts` is older but out-of-lane) is
     still open, `mergeable_state: "behind"` (confirmed via `pull_request_read`), 1 comment,
     no review, 74 days old.
4. **New finding this run, not in prior status passes:** checked `git log --merges` on
   master for every `dispatch/*` branch that has ever merged, going back to repo start.
   Result: **exactly one** dispatch *bug-fix* PR has ever merged — `#19
   dispatch/indicators-supertrend-atr-tests`, long before the current backlog built up.
   Every other merged `dispatch/*` PR in history is a no-op status-pass PR (e.g. #163).
   So this isn't "a slow reviewer" pattern — it's "the review channel for dispatch's
   actual fix PRs has, in practice, never worked," while `feat/*`, `research/*`, and
   `deploy/*` PRs from the interactive session do get merged regularly (#144–#147, #164
   all merged in the last few weeks). The bottleneck is specific to this PR category, not
   general review capacity.
5. Installed deps into an isolated venv (`/tmp/cbvenv`, not the system Python — avoided
   repeating prior runs' `pip`/system-package conflicts; a bare `pip install -r
   requirements.txt` on this container's system Python collides with a debian-managed
   `cryptography` package and aborts partway).
6. `python -m pytest tests/ -q` (venv) → **3639 passed, 0 failed.** Confirms, again, that
   the "2 known pre-existing fails" line in CLAUDE.md/WORKLOG.md is stale — suite has been
   fully green since at least 2026-10-15's check.

## Why no new fix PR this run

Same reasoning as the last several status passes (#119, #127, #139, #142, #152, #153,
#156, #163): every lane file has already been re-read line-by-line by prior runs looking
for exactly this kind of bug, and the result is filed as one of the 32 open PRs. Adding
PR #169 to a queue where 0 of the last ~30 narrow fix PRs have merged doesn't increase the
odds this specific fix lands — it just adds to what somebody eventually has to triage.

## What's actually new / actionable here

- **The "1 of 32+" number is the headline.** Previous status passes reported growing PR
  *count*; this run is the first to check history and confirm the dispatch fix-PR merge
  rate is effectively zero, not just slow — the mechanism dispatch exists to use (open a
  small tested fix, let the owner merge it) isn't landing fixes in practice.
- Sent a push notification to the account owner with this finding, since prior
  PR-body/notification escalations (per #156, #163) haven't changed throughput and the
  underlying number has now gotten more precise and more concrete.
- Suggest, for whoever next reviews this: either batch-review/merge the oldest ~10
  `dispatch/fix-*` PRs (most are narrow, single-file, tested, independently mergeable), or
  explicitly tell dispatch to stop opening new ones until the backlog clears — continuing
  to generate PR #169, #170, ... into a channel with a 0% landing rate is not useful
  dispatch behavior, it's just backlog growth.

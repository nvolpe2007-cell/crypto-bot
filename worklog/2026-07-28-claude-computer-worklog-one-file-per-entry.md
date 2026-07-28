---
date: 2026-07-28
agent: claude-computer
branch: docs/worklog-one-file-per-entry-2026-07-28
lane: shared
files: [WORKLOG.md, CLAUDE.md, worklog/README.md, scripts/worklog_index.py]
---

# WORKLOG restructured to one file per entry

`WORKLOG.md` held a single append-only table. Every agent finishing work appended a row
at the bottom, so **any two concurrent PRs conflicted on that file by construction** —
git cannot auto-merge two different lines added at the same position.

This was not theoretical. Measured on 2026-07-28: **21 of 25 open PRs were
`CONFLICTING`**, `WORKLOG.md` the common factor. Merging just three PRs in one session
(#86, #66, #81) required **two hand-resolved conflicts**, both in this file. #72 landed
and pushed #66 from `MERGEABLE` to `CONFLICTING` within seconds. Even #84 — the merge
train opened specifically to drain the backlog — had itself gone conflicting at 70 files.

The queue was conflicting with itself faster than it drained.

## Change

- New `worklog/` directory, **one file per entry**, named
  `YYYY-MM-DD-<agent>-<slug>.md`. Two agents writing two filenames never collide.
- `worklog/README.md` — format, template, and the reasoning above.
- `scripts/worklog_index.py` — renders the chronological view the old table gave you,
  with `--agent` / `--since` / `--lane` / `--full` filters. **Read-only on purpose:**
  generating a combined log file back into the repo would recreate the exact shared-file
  conflict this removes.
- `WORKLOG.md` keeps the rules, lane map and in-flight items. Its 39-row table is
  preserved verbatim under **Log — archive**, frozen. Nothing was migrated or rewritten —
  the historical record stays byte-identical.
- `CLAUDE.md` rule 5 added: new entries go in `worklog/`, never appended to the archive.

## What this does not fix

The 21 already-conflicting PRs. Each still needs its own master-merge, and each landing
still re-conflicts the others *on their own overlapping code changes*. This change stops
**new** work being born conflicted; it doesn't retroactively rescue the backlog.

## Verification

Docs + one read-only script. No strategy, loop, or config code touched.
`python -m pytest tests/ -q --collect-only` → 3116 collected, unchanged.
`python scripts/worklog_index.py` renders this entry.

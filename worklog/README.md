# worklog/ — one file per entry

**Why this exists:** `WORKLOG.md` used to hold a single append-only table. Every agent
that finished work appended a row at the bottom, which meant **any two concurrent PRs
conflicted on that file, always**. Not occasionally — structurally. Git cannot
auto-merge two different lines appended at the same position.

On 2026-07-28 that cost three hand-resolved conflicts in a single session while merging
PRs #86, #66 and #81, and at that point 21 of 25 open PRs were `CONFLICTING`, with
`WORKLOG.md` the common factor. The merge train opened to fix the backlog (#84) had
itself gone conflicting.

**One file per entry removes the conflict entirely.** Two agents writing two different
filenames never collide.

## How to add an entry

Create **a new file**. Never edit an existing one, and never append to `WORKLOG.md`'s
archive table.

```
worklog/YYYY-MM-DD-<agent>-<short-slug>.md
```

`<agent>` is one of `claude-computer`, `dispatch`, `routine`, or another stable name for
the agent doing the work. Keep `<short-slug>` to a few words, kebab-case. If the name
collides, add `-2`.

## Template

```markdown
---
date: 2026-07-28
agent: claude-computer
branch: fix/some-thing
pr: 91
lane: brain-risk-observability     # or: directional | shared | none
files: [src/foo.py, tests/test_foo.py]
---

# Short title

What you did and why, in prose. Numbers where you have them.

**Cross-lane note:** only if you touched another lane's files — say which and why.

**Verification:** test counts, CI result, anything you checked.
```

Frontmatter fields other than `date` and `agent` are optional — fill what applies. Body
prose matters more than field completeness; the point is that the next agent, with no
memory of your session, can tell what happened.

## Reading the log

```bash
python scripts/worklog_index.py          # chronological, newest first
python scripts/worklog_index.py --agent dispatch
python scripts/worklog_index.py --since 2026-07-01
python scripts/worklog_index.py --full   # include body text
```

The script only reads. It deliberately does not write a combined file — generating one
into the repo would reintroduce exactly the shared-file conflict this structure exists
to remove.

## History

Entries before 2026-07-28 are in `WORKLOG.md` under **Log — archive**. That table is
frozen: read it, don't add to it.

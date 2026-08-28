---
date: 2026-08-28
agent: dispatch
branch: (none — read-only run, no code changes)
pr: (none)
lane: directional (status pass only, no files touched)
files: []
---

# Status pass: 8 open PRs unreviewed for up to 9 days, master hasn't moved — recommend triage over more findings

## What I did this run

Per the dispatch prompt's option (c) ("if nothing new and safe to try, do a read-only
status/health pass"), I chose **not** to open a ninth PR this run. Every dispatch run since
2026-08-20 (#100–#106, seven PRs, all still open/draft/unreviewed) has independently
rediscovered the same structural blocker and said so in its own worklog:

- No VPS/data access from this cloud session (`data/` is gitignored, lives only on the
  Hetzner box) — option (a), analyzing real forward-test/paper-trading results, has **never
  once been executable** in eight consecutive dispatch runs (2026-08-20 through 2026-08-27).
  Every run has fallen back to (b): static-analysis bug hunts in the directional lane.
- The most recent run (2026-08-27, `dispatch-fix-whale-average-self-inclusion`) explicitly
  flagged: *"if this backlog keeps growing without owner review, a future run might be
  better spent on a read-only status pass (option c) ... than adding a seventh unreviewed PR."*
  Nothing has been merged since, so I acted on that recommendation.

## Findings

**Master is stale.** `git log -1` on `master` shows the last commit landed 2026-08-19
(`39ed5d9`). Nothing has merged in the 9 days since, despite 8 open PRs accumulating in
that window.

**8 open PRs, all untouched since creation, oldest 9 days old:**

| PR | Branch | Opened | Lane | One-line summary |
|---|---|---|---|---|
| #99 | `stockbot-orb-alerts` | 2026-08-19 | stockbot (not this lane) | Interactive Telegram ORB setup alerts |
| #100 | `dispatch/fix-cvd-trend-sign-semantics` | 2026-08-20 | directional | `get_cvd_trend` sign-vs-acceleration bug fix |
| #101 | `dispatch/document-ofi-veto-strictness-mismatch` | 2026-08-21 | directional | docs+test only, no behavior change (OFI veto stricter than documented) |
| #102 | `dispatch/fix-dual-direction-probe-noop` | 2026-08-24 | directional | dual-direction probe flip/reject verdict was silently discarded (AttributeError swallowed) |
| #103 | `dispatch/warn-enable-shorts-noop` | 2026-08-25 | directional | `ENABLE_SHORTS` on the **live** (real-money) path is a dead flag — now warns instead of silently doing nothing |
| #104 | `dispatch/funding-extreme-kill-filter-direction` | 2026-08-26 | directional | docs+test only, flags a real ambiguity (hard funding veto ignores trade side, unlike the soft check) |
| #105 | `dispatch/fix-whale-average-self-inclusion` | 2026-08-27 | directional | whale-detection average included the candidate trade itself (currently dead code, zero live blast radius) |
| #106 | `dispatch/fix-stale-bot-main-tests` | 2026-08-27 | shared (`src/bot.py`) | fixes the actual 4 failing tests below — verified, zero logic change |

All are small, single-purpose, each with passing tests and a detailed worklog entry
explaining risk/rationale. None touch `atr_alive` or any cost-aware gate strictness
(the docs-only PRs #101/#104 explicitly declined to loosen anything, deferring to
forward-test evidence this sandbox can't produce).

**Test baseline on master (verified fresh this run):** `python3 -m pytest tests/ -q` →
**4 failed, 3577 passed** (after installing missing `fastapi`/`python-multipart`, which
aren't in the base image but are in `requirements.txt` — not a repo bug, just this
sandbox's environment). The 4 failures are exactly `tests/test_bot_main.py::
TestMainSubsystemIsolation::*`, the same ones every run since #100 has flagged as
pre-existing and unrelated to their own change. **PR #106, sitting unreviewed, is the fix
for exactly these 4 failures** (verified: its branch brings the suite to 3581 passed / 0
failed). CLAUDE.md's "2 known pre-existing fails" line is stale on two counts — it's
actually 4 on current master, and a fix is already open and unmerged.

## Why I didn't add a 9th PR

Continuing the same static-analysis pattern each run has diminishing value once the
lane's obvious "doc says X, code does Y" bugs have already been found (five separate
audits since 2026-08-20 targeting that exact pattern), and a 9th open PR just adds to a
queue that isn't being drained. The actual bottleneck this run's evidence points to is
review/merge throughput, not a shortage of findings.

## Recommendation to the owner

1. **PR #106** is the highest-value, lowest-risk merge available: it fixes the repo's
   actual known-failing-test baseline with a verified before/after, touches no trading
   logic, and unblocks every other open dispatch PR's CI from having to re-explain the
   same 4 failures as "not mine" in its own worklog.
2. The other 7 PRs are all small and independently reviewable; #101 and #104 are pure
   docs+tests (zero behavior change) and should be the fastest to merge if a lighter
   review pass is wanted first.
3. **Structural ask:** if this scheduled routine is meant to make progress on the
   pre-registered profitability mandate (option (a) — real forward-test/proof-scorecard
   analysis), it needs either read access to the VPS's `data/` state, or a way for a human
   to paste recent `proof_scorecard.py`/`trade_report.py` output into a run. Eight
   consecutive runs producing only static-analysis findings on dormant/shelved code paths
   (the directional side is documented in CLAUDE.md as "usually idle") is a sign the
   current setup is well past the marginal value of that approach alone.

## Verification

Read-only run — no files changed, no branch created, no PR opened.
`python3 -m pytest tests/ -q` on `master` @ `39ed5d9`: 4 failed, 3577 passed (baseline
confirmed, not modified).

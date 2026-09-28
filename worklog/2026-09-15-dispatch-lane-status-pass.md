---
date: 2026-09-15
agent: dispatch
branch: (none — read-only run, no PR)
pr: n/a
lane: directional
files: []
---

# Directional lane is thoroughly mined by prior runs; no new safe change found — status pass instead

## What I did this run

Per CLAUDE.md option (c): a read-only status/health pass. No code changes.

1. `git fetch && checkout master && pull` — fast-forwarded 11 commits (through PR #125,
   `guard/altperp-volume-provenance`).
2. Read `WORKLOG.md` + `python scripts/worklog_index.py` + `ls worklog/` for recent activity.
3. Pulled the full open-PR list (`gh`-equivalent via GitHub MCP) — **32 open PRs**, several
   dozen more open branches with no PR yet. In the directional lane alone, the following are
   open and unmerged: #100 (orderflow_ws CVD sign), #101 (entry_checklist OFI veto docs),
   #102 (paper_trading dual-direction probe noop), #103 (live_trading enable_shorts warn),
   #104 (paper_trading funding-extreme kill-filter direction — docs, concluded intentional),
   #105 (orderflow_ws whale-average self-inclusion), #109 (indicators.py supertrend, cross-lane),
   #121 (scientific_strategy ofi_min unused), #123 (pairs_strategy divergence re-arm), #126
   (scientific_strategy lead_lag_min dead-param docs), #128 (scientific_strategy docstring),
   #129 (live_trading signal-exit debounce, opened yesterday 2026-09-14).
4. Ran `python -m pytest tests/ -q` (after `pip install fastapi python-multipart` — a fresh
   container has neither, which otherwise blocks collection of `test_bot_main.py` /
   `test_dashboard.py` with `ModuleNotFoundError`, unrelated to any code bug):
   **3584 passed, 0 failed.**

## Finding worth flagging: the "2 known pre-existing fails" note is stale

CLAUDE.md and WORKLOG.md both still say "2 known pre-existing fails" when describing the
post-push test check. On current master, `pytest tests/ -q` is **fully green — 0 failures**.
I searched for the historically-cited `test_exchange` batching test and a `test_notifications`
env-default test by keyword; neither matched anything in the current suite. PR #109's own
worklog (2026-09-03) already flagged the count had drifted from "2" to "9" at that point
(`test_bot_main` + `test_dashboard::TestLoginRoute`, both `fastapi`-import collection errors,
not real logic failures) — and by today those are gone too. Net effect: the "2 known" figure
in both docs no longer describes reality in either direction. Not fixing the docs myself this
run (doc-only edit to CLAUDE.md is arguably brain/risk-observability-adjacent and low-value
relative to the churn it'd add to an already 30+-PR backlog); flagging here so whoever next
touches that note doesn't have to re-derive it.

## Why no new PR this run

I read `src/entry_checklist.py`, `src/pairs_strategy.py`, `src/orderflow_ws.py`, and the
signal-scoring section of `src/scientific_strategy.py` in full, plus the directional entry
path in `src/paper_trading.py` (~lines 1900-2200: dual-direction probe, long/short entry,
checklist wiring, sizing, prob-gate). Everything I flagged as a candidate bug while reading
turned out to already be documented or fixed in one of the open PRs above:

- `orderflow_ws._handle_trades`'s whale-average still includes the candidate trade's own size
  in the average on master (pre-#105 merge) — already found, PR #105 open since 2026-08-27
  (19 days unmerged).
- `orderflow_ws.get_cvd_trend` compares recent-vs-prior windows (acceleration) against a
  docstring promising "net buying pressure... is positive" (sign) — already found, PR #100
  open since 2026-08-20 (26 days unmerged).
- `scientific_strategy.ScientificStrategy.__init__` stores `self.ofi_min`/`self.lead_lag_min`
  but the OFI/lead-lag direction logic uses hardcoded `0.15` — already found (`ofi_min`: PR
  #121; `lead_lag_min`: PR #126, which correctly notes it's genuinely dead, not a fresh bug
  since `lead_lag.get_signal()` applies its own internal threshold).
- `entry_checklist._ofi_aligned` vetoes on any negative `ofi_score` rather than only strongly
  opposing OFI — already found, PR #101.
- `pairs_strategy.py` is confirmed dead code in production (only `pairs_paper.py`'s
  market-neutral arm trades; `grep` shows no non-test importer of `PairsStrategy`) — this is
  already known and intentional per `pairs_paper.py`'s own module docstring, which explicitly
  contrasts itself against "dispatch's `src/pairs_strategy.py`, which trades only ONE leg
  directionally; that is not market-neutral." Per CLAUDE.md's warning to distrust dead-code
  verdicts on files with fresh tests (`multi_agent_master_races`), and since dispatch's own
  PR #123 touched this file 5 days ago, I did not touch it further.

I did not find anything past this that looked both new and safe. Given the volume of small,
already-identified, already-PR'd fixes sitting unmerged (11 in this lane alone), opening a
12th narrow micro-fix PR this run seemed lower value than confirming lane health and leaving
an accurate signpost for the next run — especially since a repeat pass over the same files is
likely to just re-find what #100/#101/#104/#105/#121/#123/#126 already cover.

## Forward-test / journal data (option a) — not reachable from this environment

`data/` is gitignored and the repo's checked-in `data/trade_journal.csv` is leftover synthetic
rows from local test runs (`trade_id` values like `id_0`, `r1`, `r2`, `kept` — not real trades).
The live paper-trading state (real `trade_journal.csv`, `funding_arb_paper` state, `attribution.db`
with real entries) lives on the Hetzner VPS (`178.105.41.226`), which this cloud session has no
access to. Analyzing real forward-test results is not possible from here; whoever runs dispatch
with VPS access (or pulls a fresh export) should do that pass instead.

## Suggestion for the owner (not acted on — outside dispatch's remit)

11 directional-lane PRs (plus dozens more across other lanes) have been open, unreviewed, and
unmerged for up to 26 days. Every one of them is small, has tests, and several document/degrade
gracefully rather than change behavior. Until some of this backlog merges, repeat dispatch runs
are increasingly likely to spend their budget re-confirming already-found issues rather than
finding new ones (this run is itself an example). A merge pass — even a fast skim-and-merge of
the doc-only / test-only PRs (#101, #104, #126, #128) — would raise the marginal value of the
next automated run.

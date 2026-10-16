---
date: 2026-10-16
agent: dispatch
branch: dispatch/lane-status-pass-1016
pr: TBD
lane: directional
files: []
---

# Directional lane: exhaustive bug-hunt pass found nothing new; the one candidate is deliberate staging, not a bug

## What I did this run

Per CLAUDE.md option (c) — read-only status/health pass, no code changes.

1. `git fetch && checkout master && pull` — fast-forwarded 5 commits, picked up PR #163
   (yesterday's `dispatch/lane-status-pass-1015`, merged) plus the `dashboard_server.py`
   / Greg-paper-book work from another agent.
2. `python -m pytest tests/ -q` (after `python -m pip install --ignore-installed cryptography
   -r requirements.txt` + `pytest`/`fastapi` — fresh container, same install snag flagged by
   prior runs) → **3639 passed, 0 failed.** Up from 3634 at the last check (2026-10-15); still
   fully green. The "2 known pre-existing fails" line in CLAUDE.md/WORKLOG.md remains stale —
   now a third consecutive data point (#130, #163, this run) saying so.
3. Pulled the current open-PR list (45 open, #99 through #162; #163 merged). Did **not** re-run
   the backlog-size/staleness escalation — that was already pushed to the account owner
   yesterday (2026-10-15 worklog) and nothing material changed in the 24h since (still the same
   ~36 unreviewed directional-lane PRs, oldest now 58 days). Repeating an unchanged escalation
   the day after it was sent would be noise, not signal.
4. Instead, delegated a targeted exploration pass: read all 7 owned lane files
   (`paper_trading.py`, `scientific_strategy.py`, `entry_checklist.py`, `live_trading.py`,
   `pairs_strategy.py`, `orderflow_ws.py`, `indicators.py`) line-by-line against the exact list
   of the 36 already-filed PR titles, specifically looking for ONE new correctness bug not
   already covered — same bug *kinds* as the backlog (dead/unwired params, asymmetric
   long/short logic, persistence gaps, sign/unit mismatches), different specific instance.

## Finding: `src/orderflow_ws.py` is dead code — but that's deliberate, not a bug

The only candidate that survived cross-checking against the 36 titles: the entire
`OrderFlowWS` class (`src/orderflow_ws.py:77`) and its `obi_from_book` helper
(`src/orderflow_ws.py:48`) are never instantiated anywhere outside their own test files.
Confirmed repo-wide: `grep -rn "OrderFlowWS\|orderflow_ws\|obi_from_book" --include="*.py" .`
returns only `src/orderflow_ws.py` itself and `tests/test_orderflow_ws.py` /
`tests/test_tick_cvd.py`. What the live/paper loop actually uses for OFI is
`OrderFlowImbalance` from `src/order_flow.py` (REST-polling) — imported in
`scientific_strategy.py:28`, `paper_trading.py:38/477`, `live_trading.py:37/99`. The module's
own docstring (`orderflow_ws.py:1-24`) says explicitly it "replaces REST-polling
OrderFlowImbalance" and shows the intended call site
(`ofw = OrderFlowWS(symbols); asyncio.create_task(ofw.start())`) — that call site doesn't
exist anywhere in the codebase.

**This is not a new bug** — it matches CLAUDE.md's own "Maker-only microstructure
(OFI+CVD+OBI on Kraken) — 90-day re-test" section verbatim: "Foundation landed (core
mechanisms, unit-tested; live-loop wiring is the next stage)... NEXT STAGE (needs **on-VPS
WS testing**): fan `KrakenTradeFeed` out to CVD, wire OBI from `KrakenBookFeed`, route the
live micro path through the maker model." `orderflow_ws.py` *is* that staged-but-unwired OBI/
CVD machinery. The repo already documents this as intentional, gated on VPS-side WS testing
this cloud session has no way to perform (no VPS/live-exchange access here) — so I did not
attempt to wire it. Doing so blind, without the on-VPS verification CLAUDE.md itself requires
as the gating step, would risk corrupting the in-progress micro-arm proof window
(`data/micro_paper_state.json`) that CLAUDE.md says must read PROVEN ✓ over ≥90 days before
any expansion.

**One thing worth flagging to whoever reviews the backlog:** 3 of the 36 open PRs (#100
`get_cvd_trend` sign bug, #105 whale-average self-inclusion, #154 `get_spread_pct` scale
mismatch) are correctness fixes *inside this unwired module*. They're still worth merging
(correct code beats incorrect code, and they'll matter the moment the wiring stage lands),
but none of them currently change live/paper behavior at all — `OrderFlowWS` never runs.
Worth knowing when triaging backlog priority: these 3 are zero-live-impact until the wiring
PR (a separate, larger, on-VPS-gated piece of work) exists.

## Why no new PR beyond this worklog entry

Thorough re-scan across all 7 lane files, cross-checked against every one of the 36 filed PR
titles, found nothing else above the bar of "already covered." Filing a 46th PR for the same
dead-code fact CLAUDE.md already documents would add backlog volume without adding
information. This entry is the record; no source files changed.

**Verification:** `python -m pytest tests/ -q` → 3639 passed, 0 failed (ran before and after
this worklog-only change; identical, since no `src/` or `tests/` file was touched).

---
date: 2026-10-04
agent: dispatch
branch: dispatch/fix-orderflow-ws-spread-pct-scale-mismatch
pr: TBD
lane: directional
files: [src/orderflow_ws.py, tests/test_orderflow_ws.py]
---

# `OrderFlowWS.get_spread_pct()` returned a percentage while every lane consumer expects a fraction — fixed the oldest still-open flagged finding, not a new status pass

Scheduled "continue the profitability mandate" pass.

## Start-of-run checks

`git fetch && checkout master && pull` — fast-forwarded cleanly to `021135b` (unchanged
since 2026-09-28; no merge activity since). Read `CLAUDE.md` in full, `WORKLOG.md`, and
`python scripts/worklog_index.py`. `pip install -r requirements.txt pytest` (fresh
container). `python -m pytest tests/ -q` on master: **3634 passed, 0 failed** — CLAUDE.md's
"2 known pre-existing fails" note no longer reproduces, consistent with every run since
2026-09-15.

## Backlog check (why this run doesn't add a 4th status-pass PR)

Listed all open PRs via GitHub MCP: **35 open**, 26 directional-lane `dispatch/*`
(#100–#153), oldest (#100) now 45 days unmerged. This exact backlog has already been
flagged in-repo four times (#119, #127, #139, #142) and escalated out-of-band via push
notification on 2026-10-02 (`worklog/` isn't the record for that one — see PR #152's
body). The 2026-10-02 and 2026-10-03 runs already did the deep dives: zero merge
conflicts against current master, tests green, no new live-impact bug found after an
end-to-end re-read of `indicators.py` and `orderflow_ws.py`. Re-running the same check a
third time in a row would add nothing. I also checked the closed-PR history
(`list_pull_requests(state=closed)`): #117/#130/#131/#133/#134 (Sept 17–18) and
#143–#147 (Sept 28) were all closed with their content folded into master via local
merges (not GitHub's merge button, so `merged: false` in the API despite the content
landing) — so the backlog **is** draining, just in periodic batches roughly every 1–2
weeks, last one 6 days ago. That's consistent with "the owner batches review," the
hypothesis the 2026-10-02 entry left open. No new notification sent this run: nothing
has changed since the one two days ago that already said this.

## What I did instead

Rather than hunt for issue #27 across the same seven files five runs have now combed, I
went back to the backlog itself for a **cheap, safe, already-diagnosed loose end that
never got turned into a fix**: PR #121's worklog (2026-09-09,
`dispatch-scientific-strategy-ofi-min-dead-wiring.md`) flagged that
`src/orderflow_ws.py::get_spread_pct()` returns an actual percentage (`× 100`) while
every real consumer of a `*_pct` spread field in this lane —
`entry_checklist.CheckContext.current_spread_pct`/`median_spread_pct`,
`SpreadTracker.push()` — is a **fraction** (0.0008 = 8bp), not a percentage. That's a
100x scale mismatch. It sat unfixed for 25 days because PR #121 itself never touched it
(separate finding, same worklog) and no other open PR names it.

**Live impact: none today, same as when flagged.** `get_spread_pct` has zero call sites
anywhere in `src/`, `arbitrage/`, or any `*_paper.py`/`*.py` entry point — confirmed by
grep. So this cannot change live bot behavior. The value is purely defensive: the day
someone wires book-derived spread data into `entry_checklist.py`'s `SPREAD_MAX_MULT`
veto (the obvious next integration, since `OrderFlowWS` already tracks the book this
needs), a silent 100x-too-large reading would either permanently trip the wide-spread
veto (false negative on every entry) or, if the comparison direction were also
misread, defeat it — either way corrupting a hard risk gate without a single test
failing, since nothing exercises the call path yet.

## The fix

Removed the `* 100` in `get_spread_pct`'s return statement — it now returns
`(best_ask - best_bid) / mid` as a fraction, matching the rest of the lane's (admittedly
misnamed) `*_pct` convention. Expanded the docstring to say so explicitly and explain why
(so a future reader doesn't "fix" it back to a real percentage). No other logic touched.

## Tests

`tests/test_orderflow_ws.py` had no coverage for this method at all. Added
`TestGetSpreadPct` (3 cases): fraction-not-percentage on a synthetic book (mid=100,
spread=2 → asserts 0.02, not 2.0), stale-book returns `None`, no-book-data returns
`None`. Verified the first case **fails on pre-fix code** (temporarily restored the
`* 100` in a scratch copy and reran — `2.0 == 0.02` assertion fails as expected; restored
the fix immediately after).

`python -m pytest tests/test_orderflow_ws.py -q` → 21 passed (18 existing + 3 new).
`python -m pytest tests/ -q` → **3637 passed, 0 failed** (3634 baseline + 3 new).

## Not done / left for a future run

- The directional-lane backlog itself (35 open PRs, 26 in my lane) — no action a code
  change can take; already escalated. A future run should keep checking it's not going
  stale (merge-conflict-free) rather than re-deriving the same "it's a review bottleneck"
  conclusion a 4th time.
- No forward-test/paper-trading data analysis this run — still no VPS/`data/` access
  from this sandbox (same as every prior run back to 2026-09-10).

---
date: 2026-08-08
agent: claude-computer
branch: arm-monitor-alerts
pr: TBD
lane: brain-risk-observability
files: [scripts/arm_monitor.py, tests/test_arm_monitor.py]
---

# Event-driven arm monitor — alert on meaningful proof-scorecard transitions

Owner asked for a way to get notified when any arm crosses a meaningful
sample-size threshold, after a full sweep of `research/hypothesis_registry.yaml`
confirmed nothing is currently overlooked and the honest next lever is time
(forward samples accumulating), not more strategy search.

`scripts/weekly_report.py` already covers a periodic digest; this is
complementary and event-driven: reuses `proof_scorecard.py`'s own
`build_arms()`/`_verdict()` directly (never a second opinion on what
"proven" means, only a diff against the last check) and fires a Telegram
alert only on:

1. An arm's trade count crosses `N_MIN` (30) for the first time -- the
   moment its verdict becomes a real statistical read instead of
   "insufficient sample."
2. An arm's verdict CATEGORY changes (NOT_PROVEN / FAILED / PROVEN_SINGLE
   / PROVEN / FANTASY) -- including regressions, not just NOT_PROVEN ->
   PROVEN.
3. A brand-new arm appears with trades already booked.

Silent when nothing changed -- deliberately avoids becoming noise that
gets muted; verified this on the VPS's real state (first run correctly
announced the 9 currently-active arms as a baseline, second run was
silent with zero changes in between).

Small state file (`data/arm_monitor_state.json`) tracks the last-seen
snapshot per arm label; a floor-cross and a category change on the SAME
event fire only one alert line, not two (tested explicitly).

+20 tests (`tests/test_arm_monitor.py`): verdict-category collapsing (all
5 categories), first-run new-arm handling (silent at n=0, alerts with
trades), floor-crossing (alerts once, not on every subsequent run above
30), category-change transitions (both directions, regressions included),
multi-arm isolation (only changed arms produce lines), state-file I/O
(missing file, round-trip, atomic write), and `snapshot_arms()` wiring
against a mocked `build_arms()`.

**Cross-lane note**: `proof_scorecard.py` is explicitly in this lane's
file list; `scripts/` isn't claimed by either lane in the map, consistent
with `weekly_report.py` also living there unclaimed.

**Verification**: full suite 3082 passed (was 3062 before this branch);
pre-existing thin-venv async failures (350, unrelated) unchanged. Manually
verified against live VPS state via a temporary scp copy (not committed
there) before writing this PR -- confirmed correct first-run baseline
announcement and correct silence on a clean second run.

**Deploy note**: needs a VPS cron line, e.g. every 6h:
`0 */6 * * * cd /opt/crypto-bot && ./venv/bin/python scripts/arm_monitor.py >> logs/arm_monitor.log 2>&1`

---
date: 2026-08-27
agent: dispatch
branch: dispatch/fix-whale-average-self-inclusion
pr: TBD
lane: directional
files: [src/orderflow_ws.py, tests/test_orderflow_ws.py]
---

# `OrderFlowWS._handle_trades` folded the candidate trade into its own whale-detection average

No VPS/data access from this cloud session (`data/` is gitignored, lives only on the
Hetzner box), so option (a) — analyzing recent forward-test results — wasn't possible
this run, consistent with every prior dispatch run's note on this. Also checked open PRs
first: #100, #101, #102, #103, #104 are all still open, draft, unmerged/unreviewed —
five directional-lane audit fixes now queued waiting on the owner. Read all five
worklogs to avoid duplicating their findings.

PR #100's worklog (`2026-08-20-dispatch-fix-cvd-trend-sign-semantics.md`) flagged a
specific, concrete follow-up candidate that hadn't been picked up by any of the four
runs since: `_handle_trades` appends the current trade's size to `_trade_sizes[sym]`
**before** computing the whale-detection average, so the candidate trade is baked into
its own comparison baseline. I verified this by hand before doing anything else.

## The bug

`src/orderflow_ws.py::OrderFlowWS._handle_trades` (module docstring, line 15: "Whale —
Single-trade detection: a trade ≥ 3× the 100-trade average size"):

```python
self._trade_sizes[sym].append(size)          # candidate trade added first
sizes = list(self._trade_sizes[sym])          # ...then included in its own average
if len(sizes) >= 10:
    avg = sum(sizes) / len(sizes)
    if avg > 0 and size >= _WHALE_MULT * avg:
```

Because `size` is folded into `avg` before the comparison, the effective threshold is
higher than the documented 3×, worse the shallower the window. With N-1 prior trades at
average 1.0 and a candidate of size X: solving `X >= 3*(N-1+X)/N` gives the true
breakeven at `X >= 3*(N-1)/(N-3)`, e.g. **≈3.86×** (not 3×) at the 10-trade floor,
relaxing toward ≈3.06× only once the 100-trade window (`_WHALE_HISTORY`) is full. A
print between 3× and the inflated bar silently fails to register as a whale.

**Live blast radius today: zero.** `OrderFlowWS` (this class specifically — distinct
from `TickCVDTracker`/`obi_from_book` which are already wired per CLAUDE.md's
maker-only-microstructure section) is not instantiated anywhere outside its own module
and tests — confirmed with `grep -rn "last_whale(\|OrderFlowWS(" --include=*.py`, no
hits in `paper_trading.py`, `live_trading.py`, or `entry_checklist.py`. The module's own
docstring says "Usage (in live_trading / paper_trading)" but that wiring hasn't happened
yet — this is foundation code for a future stage, same category as the maker-fill /
tick-CVD work already landed per CLAUDE.md. So this fix touches no live gate, no cost
accounting, no entry frequency — pure correctness-in-isolation, safe regardless of the
Core Principle (nothing to loosen; nothing trades on this signal yet).

## The fix

Snapshot `_trade_sizes[sym]` **before** appending the current trade, so `avg` is
computed over prior history only; append after. Threshold gate (`len(sizes) >= 10`)
unchanged in shape, now correctly means "10 prior trades of history" rather than
"10 trades including this one."

## Tests

Added `test_whale_average_excludes_the_candidate_trade_itself` in
`tests/test_orderflow_ws.py`: 10 prior trades of size 1.0, then a candidate of size 3.5
(above the true 3× bar, below the old inflated ~3.86× bar) — asserts it's now flagged as
a whale. This test would have failed under the pre-fix code. The two existing whale
tests (`test_whale_detected_above_multiplier`, `test_no_whale_below_history_floor`) don't
discriminate between old/new logic (their fixtures clear either bar or stay under the
10-trade floor entirely) and still pass unchanged.

## Verification

- `python -m pytest tests/test_orderflow_ws.py -q` → 17 passed (was 16).
- `python -m pytest tests/ -q` → 3578 passed, 4 failed. Same 4 pre-existing
  `tests/test_bot_main.py::TestMainSubsystemIsolation::*` failures every dispatch run
  since #100 has flagged (`AttributeError: module 'src.bot' has no attribute
  '_run_funding_scanner'`) — `src/bot.py` is outside this lane, untouched here. The
  "2 known pre-existing fails" baseline in CLAUDE.md/WORKLOG.md is still stale (it's 4),
  as previously flagged by PR #100/#101; not re-fixing that here, just re-confirming it
  hasn't drifted further.

## For the next agent

- Five dispatch PRs (#100–#104) are still open/unmerged as of this run, now six with
  this one. All are small, single-purpose, lane-scoped diffs with passing tests — no
  action needed from an agent, but if this backlog keeps growing without owner review,
  a future run might be better spent on a read-only status pass (option c) summarizing
  the queue than adding a seventh unreviewed PR.
- No forward-test/journal data reachable from this sandbox in any run so far (option a
  has never been executable) — flagging again in case VPS/data access gets wired into
  the cloud environment at some point, since the pre-registered proof-bar mandate really
  needs the live trade_journal.csv / proof_scorecard.py output to make progress on, not
  more static-analysis bug hunts in dormant code paths.

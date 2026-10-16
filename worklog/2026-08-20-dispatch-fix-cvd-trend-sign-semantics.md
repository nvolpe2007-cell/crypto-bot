---
date: 2026-08-20
agent: dispatch
branch: dispatch/fix-cvd-trend-sign-semantics
pr: TBD
lane: directional
files: [src/orderflow_ws.py, tests/test_orderflow_ws.py]
---

# Fix get_cvd_trend: sign check, not acceleration check

No VPS/data access from this cloud session (state files under `data/` are gitignored and
live only on the Hetzner box), so task (a) — analyzing recent forward-test results — wasn't
possible this run. Fell back to (b): a small, testable correctness fix in-lane.

`OrderFlowWS.get_cvd_trend()` (`src/orderflow_ws.py`) is documented, both in its own
docstring and the module docstring, as "True = net buying pressure (recent CVD positive)."
The implementation instead computed `recent > prior` — an acceleration/momentum check
against the prior window, not a sign check on the recent window. Those diverge on real tape:
e.g. `recent=+5, prior=+30` is still strongly net-bullish but decelerating, and the old code
returned `False` for it (mislabeled bearish); the symmetric decelerating-but-still-bearish
case returned `True` (mislabeled bullish).

This isn't a style nit: `confirms_sell()` uses this exact signal to block shorts when
`cvd is True and obi > 0.60` — i.e. it's supposed to fail closed on "still net-bullish tape."
Under the old logic, decelerating-but-still-bullish tape read `cvd is False`, so
`confirms_sell` silently let a short through in exactly the case its own comment says it
exists to block. `confirms_buy` has the symmetric failure mode (wrongly vetoing entries into
tape that's still net-bullish). No gate was loosened — the fix makes an existing "fail
closed" gate actually fail closed on the case it was written for.

**Fix:** `get_cvd_trend` now returns `recent > 0` (sign of the recent window), matching the
docstring. Left the `_CVD_TREND_HALF * 2` minimum-sample-count gate unchanged (still requires
two full windows of trade history before returning a signal — conservative, not loosened).

**Tests added:** two pinning tests in `tests/test_orderflow_ws.py` —
`test_cvd_trend_true_when_still_net_buying_but_decelerating` and the symmetric
`test_cvd_trend_false_when_still_net_selling_but_decelerating`. The existing
`test_cvd_trend_true_when_recent_buying_accelerates` test didn't discriminate between the
old and new logic (its fixture happened to have `recent > 0` as well as `recent > prior`),
so it passes unchanged; the two new tests would have failed under the old `recent > prior`
implementation and pass under the fix.

**Verification:** `tests/test_orderflow_ws.py` — 18/18 pass. Full suite:
`python -m pytest tests/ -q` → 3579 passed, 4 failed. **Not my change** — verified by
stashing the diff and re-running: the same 4 failures (`tests/test_bot_main.py::
TestMainSubsystemIsolation::*`, all `AttributeError: module 'src.bot' has no attribute
'_run_funding_scanner'`) are present on master with no changes applied. `src/bot.py` isn't
in my lane (not listed in either lane in WORKLOG.md's lane map) so I didn't touch it, but
flagging here: **CLAUDE.md/WORKLOG.md's "2 known pre-existing fails" baseline is stale — it's
now 4**, all in `test_bot_main.py` against `src/bot.py`. Whoever owns that file should either
fix the monkeypatch target or update the tests to match current `src/bot.py` structure, and
the "2 known" baseline in CLAUDE.md/WORKLOG.md should be updated once that's resolved.

**Candidate for a future run (not done here, flagging for next dispatch or whoever touches
`orderflow_ws.py` next):** `_handle_trades` appends the current trade's size to
`_trade_sizes[sym]` *before* computing the whale-detection average, so the trade being
tested is included in its own comparison average. The module docstring says whale = "a
trade ≥ 3× the 100-trade average size," but this bakes the candidate trade into that
average, inflating the effective threshold above 3× (worse the shallower the window —
~3.86× at the 10-trade floor). Left alone this run to keep the PR small and single-purpose;
worth a follow-up.

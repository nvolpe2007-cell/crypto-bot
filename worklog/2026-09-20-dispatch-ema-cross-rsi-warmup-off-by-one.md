---
date: 2026-09-20
agent: dispatch
branch: dispatch/fix-ema-cross-rsi-warmup-off-by-one
pr: pending (opened this run)
lane: directional
files: [src/indicators.py, tests/test_indicators.py]
---

# `EMACrossRSI.get_latest_signal` let a 1-bar-short warm-up through and silently returned a spurious HOLD

## What I did this run

1. `git fetch && checkout master && pull` — fast-forwarded 11 commits (through PR #134).
2. Read `CLAUDE.md`, `WORKLOG.md`, and `python scripts/worklog_index.py`.
3. Pulled the open-PR list via the GitHub MCP tools: 24 open PRs, most in the directional
   lane, unmerged for 1-4+ weeks — same picture the 2026-09-15/17 status passes described.
   Two more dispatch runs landed since the last worklog entry I could see (#135, #136 —
   both doc-only "dead parameter"/stale-comment fixes on `scientific_strategy.py` /
   `live_trading.py`), confirming that lane's easy findings are exhausted down to
   comment-only drift. Rather than add a fourth doc-only micro-fix to an already 24-deep
   backlog, I delegated a targeted read-only investigation (Explore subagent) explicitly
   excluding every already-open PR's finding, asking specifically for a *behavioral* bug
   (not docs/comments) and pointing it at the least-scrutinized lane file, `indicators.py`.
4. Baseline `python -m pytest tests/ -q` (after `pip install -r requirements.txt` +
   `pytest pytest-asyncio fastapi python-multipart`, which a fresh container has none of):
   **3608 passed, 0 failed** — confirms CLAUDE.md's "2 known pre-existing fails" note is
   still stale (as the 2026-09-15 run first flagged; nobody has fixed the doc line since).

## What I found and fixed

**`src/indicators.py::EMACrossRSI.get_latest_signal`** (line 206 pre-fix): the warm-up
gate was `len(df) < self.slow_ema`, i.e. it let exactly `slow_ema` rows (21 by default)
through. But crossover detection compares `ema_slow` against `ema_slow.shift(1)` — and
real `pandas_ta.ema()` seeds with `min_periods=length`, so the first `length - 1` rows are
`NaN`. At exactly `slow_ema` rows, `ema_slow` has only *one* non-NaN value (the last row);
`.shift(1)` on that last row is `NaN`. Any comparison against `NaN` is `False` in pandas,
so `ema_cross_up`/`ema_cross_down` are unconditionally `False` and the function returns a
normal-looking `IndicatorResult(signal=HOLD, ...)` — not `None` — silently mispresenting
"not enough data for the comparison this function is named after" as "no crossover
occurred; hold." The gate needed `slow_ema + 1` rows to guarantee two valid `ema_slow`
values for the shift-based comparison.

**Fix:** changed the gate to `len(df) < self.slow_ema + 1`.

**Why the existing test suite missed this:** `tests/conftest.py`'s `pandas_ta` stub (needed
so tests run without installing `ccxt`/`pandas_ta`) implements `ema()` as plain
`series.ewm(span=length, adjust=False).mean()` — no `min_periods`, so it never produces
NaN and can't exercise this boundary. I verified the bug is real by monkeypatching the
stub's `ta.ema` for one test to add `min_periods=length` (matching real `pandas_ta`
semantics), confirming: (a) against the pre-fix code, `get_latest_signal(_make_df(21))`
returned a spurious `HOLD` result rather than `None`; (b) with the fix, it correctly
returns `None`, and 22 rows still return a valid result.

## Scope / blast radius

`EMACrossRSI` is used only by the legacy `src/backtester.py` (`backtest_mr.py`) — the live
loop (`paper_trading.py`/`live_trading.py`) uses `ScientificStrategy` and
`MeanReversionStrategy` instead, confirmed by `grep` across the repo and by
`tests/test_indicators.py`'s own module docstring ("EMACrossRSI ... used only by the
legacy backtester"). So this doesn't touch any currently-live signal, gate, or cost
filter — no `atr_alive` or entry-gate change, per CLAUDE.md's core principle. It's a
correctness fix to an in-lane utility class (`indicators.py` is explicitly in my lane)
that would otherwise corrupt exactly-`slow_ema`-bar backtest calls with a wrong-but-
plausible result instead of the `None` the caller can check for.

## Verification

- New test `TestEMACrossRSIGetLatestSignal::test_returns_none_at_exactly_slow_ema_rows`
  (monkeypatches `ta.ema` for realistic NaN warm-up, asserts `None` at 21 rows / a result
  at 22).
- Confirmed the new test fails on pre-fix code (`git stash` the fix, re-run) with the
  exact spurious-HOLD `IndicatorResult` described above, and passes after the fix.
- `python -m pytest tests/test_indicators.py -q` → 43 passed (42 existing + 1 new).
- `python -m pytest tests/ -q` (full suite) → **3609 passed, 0 failed** (3608 baseline +
  1 new test, no regressions).

## Backlog note (repeating, since it's still true and getting more true)

24 open PRs, several in the directional lane sitting unreviewed for a month
(#100 opened 2026-08-20). Each of the last four dispatch runs (09-15 through today) has
had to work harder to find something both new and non-trivial, and the last two landed
were comment/docstring-only. A merge pass on the backlog — even just the doc-only ones
(#101, #104, #126, #128, #135, #136) — would meaningfully raise the value of future runs.

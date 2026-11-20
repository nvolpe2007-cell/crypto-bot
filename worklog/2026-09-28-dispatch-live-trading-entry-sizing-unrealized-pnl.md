---
date: 2026-09-28
agent: dispatch
branch: dispatch/fix-live-trading-entry-sizing-ignores-unrealized-pnl
pr: pending (opened this run)
lane: directional
files: [src/live_trading.py]
---

# `run_live_trading_session`'s new-entry sizing used an equity figure that ignored every open position's unrealized P&L

## This run

1. `git fetch && checkout master && pull` — fast-forwarded 11 commits (the Polymarket/GEX
   feature work landed since the 2026-09-17 run; none of it touches the directional lane).
2. Read `CLAUDE.md`, `WORKLOG.md`, `python scripts/worklog_index.py`.
3. Checked the open PR backlog: 26 open PRs, most as drafts, oldest (#100, orderflow_ws
   `get_cvd_trend`) now **39 days old**. The 2026-09-25 status pass (#142) already flagged
   this ("19 unconverted drafts, oldest 36d") — did not repeat that status-pass work, since
   a fourth PR restating the same backlog fact adds nothing a human merging PRs doesn't
   already have. Flagged the situation to the user directly instead (this is a scheduled/
   unattended run, so a stalled merge pipeline is exactly the kind of thing worth a direct
   heads-up rather than another draft PR nobody's watching).
4. No local trade journal / paper-trading state exists in this container (data lives on
   the VPS per `deployment_topology`), so option (a) — analyzing forward-test results —
   wasn't feasible here, consistent with prior runs.
5. Baseline: installed `fastapi`/`httpx` (missing in the fresh container, unrelated to any
   code issue) and confirmed `python -m pytest tests/ -q` → **3634 passed, 0 failed**
   (collects clean, no pre-existing failures observed this run).
6. Delegated a focused read (Explore agent) across all 7 lane files, explicitly excluding
   every issue already covered by the ~20 open lane PRs (#100-#141), to find one new, safe,
   small defect — not a request to loosen any cost-aware gate.

## What I found and fixed

**`src/live_trading.py`, line 677** (inside `run_live_trading_session`'s main tick loop):

```python
current_equity = trader.account.initial_capital + trader.account.total_pnl
```

feeds directly into `compute_position_size(sig.confidence, current_equity)` two lines
later for every new LONG entry. This is *realized* equity only — it silently drops the
unrealized P&L of every currently-open position. Compare `LiveTrader.get_summary()`
(`live_trading.py:382-394`), which correctly adds `sum(p.unrealized_pnl for p in
self.positions.values())`, and the sibling `paper_trading.py`, which sizes new entries
off `trader.get_account_summary()['total_equity']` — the equivalent paper-trader method
that also includes unrealized P&L. `live_trading.py`'s entry-sizing path was the one
place in the lane still hand-rolling an incomplete formula.

**Failure scenario:** bot holds an open position sitting on an unrealized loss (say -$30,
not yet closed). A new signal fires on a different symbol. `current_equity` at the old
line 677 reported the equity *as if that drawdown didn't exist*, so `compute_position_size`
sized the new trade off an inflated equity base — over-risking real capital relative to
the confidence-tier % the sizing function is meant to enforce. (The unrealized-gain case
is the harmless direction — under-sizing, not over-risking — but the loss case is a real
risk-control gap on the live/real-money path.)

This is a distinct bug and call site from the already-open #140 (`close_long()`'s
notifier-only equity figure, cosmetic — a Telegram message) — this one feeds the actual
live order-sizing decision.

**Fix:** replaced the hand-rolled formula with `trader.get_summary()['total_equity']`,
mirroring how `paper_trading.py` already sources its `current_equity` for sizing
(`get_account_summary()['total_equity']`). One-line change, no new formula introduced —
reuses the existing, already-tested `get_summary()` method instead of duplicating its
logic at the call site.

## Verification

- `get_summary()`'s unrealized-P&L-inclusion is already covered by
  `TestGetSummary.test_equity_includes_unrealized_pnl` (`tests/test_live_trading.py:806`);
  routing the call site through the same method means that test now also guards this
  call site's correctness. Did not add a redundant test — the async main-loop entry-sizing
  call site itself would need heavy exchange/websocket mocking disproportionate to a
  one-line fix that just delegates to an already-tested method.
- `python -m pytest tests/test_live_trading.py -q` → 97 passed.
- `python -m pytest tests/ -q` (full suite) → **3634 passed, 0 failed** — no regressions,
  same count as the pre-change baseline this run.

## Why this is safe / in scope

Pure correctness fix to an equity computation feeding position sizing — does not touch,
loosen, or add any entry gate (`atr_alive`, cooldown, confidence threshold, etc.) per
CLAUDE.md's core principle. `compute_position_size`'s own logic (confidence-tier % of
equity) is untouched; only the equity number it's fed is corrected to match what
`get_summary()` and the sibling paper-trading path already compute.

## Backlog note (flagged to user directly, not via another status-pass PR)

Real, tested bugfix PRs are still landing weekly in this lane (#100-#141) but none have
merged — oldest is 39 days old. The pipeline is producing genuine findings faster than
they're being reviewed/merged. Not a code change; noted here for the next agent's context
and raised directly with the repo owner this run.

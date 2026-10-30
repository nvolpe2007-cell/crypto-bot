---
date: 2026-09-23
agent: dispatch
branch: dispatch/fix-close-long-notifier-equity-excludes-other-positions
pr: 140
lane: directional
files: [src/live_trading.py, tests/test_live_trading.py]
---

# `close_long()`'s notifier equity omitted other open positions' unrealized PnL

## What I did this run

1. `git fetch && checkout master && pull` — already up to date (same HEAD, `0083496`, as
   the previous run's PR #139 base; nothing merged since 2026-09-22).
2. Read `CLAUDE.md`, `WORKLOG.md`, and `python scripts/worklog_index.py`.
3. Checked the open PR backlog via GitHub MCP: **27 open PRs**, 14 already in the
   directional lane, oldest (#99) now 35+ days unmerged. The most recent status pass
   (#139, 2026-09-22) diagnosed the backlog's root cause as dispatch PRs sitting as
   unconverted GitHub drafts — that's an owner/merge-flow issue, not something a 4th
   consecutive status pass would add anything new to (master hadn't moved since #139's
   base commit). So I did not repeat a status pass this run.
4. Instead of a 15th generic micro-fix, delegated a focused read-only hunt (Explore
   subagent) for one genuinely new bug in the *least*-scrutinized lane files —
   `entry_checklist.py` and `pairs_strategy.py` had only one open PR each, versus
   `scientific_strategy.py`'s five — explicitly excluding everything already covered by
   PRs #100–#138 (listed by number to the subagent) and the CLAUDE.md core-principle
   constraint against loosening any cost-aware gate.

## What I found and fixed

The subagent's best candidate was actually in `live_trading.py` (a file with existing
PRs, but a different code path than any of them):

**`LiveTrader.close_long()`** (`src/live_trading.py`, was line 356): computed the
`total_equity` value passed to the Telegram win/loss/trade-analysis notifier as
`initial_capital + total_pnl` only. `get_summary()` (used everywhere else — heartbeat
log, dashboard) and the invariant documented in the `reconcile_positions()` comment
~180 lines above both define total_equity as `initial_capital + total_pnl +
sum(unrealized_pnl across open positions)`. `close_long()` deletes the just-closed
position first, then computes equity for the notification — so any *other* position
still open at that moment has its unrealized PnL silently dropped, understating the
number shown to the operator on Telegram whenever ≥1 other position is open at close
time.

**Fix:** compute it with the same formula as `get_summary()`:
```python
total_equity = self.account.initial_capital + self.account.total_pnl + \
    sum(p.unrealized_pnl for p in self.positions.values())
```

**Verified not a duplicate of PR #131** (`fix(live_trading): reconciled positions were
missing from total_equity`): that PR fixed `reconcile_positions()` not adding a
reconciled position's *principal* to `initial_capital`. This is a different method, a
different formula, and a different root cause. `git log --all -S"total_equity = self.account"`
confirms no other commit ever touched this line.

## Why this is safe

`total_equity` here feeds **only** the Telegram notifier call — never sizing, gating,
position limits, or any trading decision. Fixing it changes an operator-facing display
number, not behavior. Does not touch `atr_alive` or any cost-aware gate; cannot increase
trade frequency or risk exposure per CLAUDE.md's core principle.

## Verification

- Added `test_notifier_total_equity_includes_other_open_positions_unrealized_pnl` to
  `TestCloseLong` in `tests/test_live_trading.py`: opens two positions, marks unrealized
  PnL on the one that stays open, closes the other, asserts the `total_equity` kwarg
  captured from the mocked `notifier.send_trade_analysis` call matches
  `initial_capital + total_pnl + remaining_unrealized_pnl`. Fails on pre-fix code.
- `python -m pytest tests/test_live_trading.py -q` → all pass.
- `python -m pytest tests/ -q` (full suite, after `pip install fastapi` — missing in this
  fresh container, needed by `test_dashboard.py`/`test_bot_main.py`) → **3609 passed, 0
  failed** (3608 baseline + 1 new test, no regressions, no pre-existing failures — the
  "2 known pre-existing fails" note in CLAUDE.md remains stale, as prior runs also found).

## Carried forward (unchanged from #139)

The backlog-drains-slowly problem (27 open PRs, drafts not converted) is an owner-side
merge-flow decision, not something this run re-litigated. Still worth a batch
convert-and-merge pass on the oldest/simplest PRs when the owner has time.

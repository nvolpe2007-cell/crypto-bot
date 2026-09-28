---
date: 2026-09-16
agent: dispatch
branch: dispatch/fix-live-reconcile-equity-principal
pr: (opened this run)
lane: directional
files: [src/live_trading.py, tests/test_live_trading.py]
---

# `reconcile_positions()` dropped a pre-existing position's principal from every equity figure

## Context — why this run didn't repeat yesterday's pass

Started per the usual dispatch protocol: `git fetch && checkout master && pull` (fast-forwarded
cleanly, no conflicts), read `WORKLOG.md` + `python scripts/worklog_index.py`, then read
`worklog/2026-09-15-dispatch-lane-status-pass.md` in full (from its branch —
`dispatch/lane-status-pass-0915`, PR #130, still unmerged). That run exhaustively read
`entry_checklist.py`, `pairs_strategy.py`, `orderflow_ws.py`, `scientific_strategy.py`'s scoring
path, and `paper_trading.py`'s directional entry path and found nothing new — every candidate
was already covered by one of 11 open, unmerged PRs (#100–#129). It correctly flagged that
re-reading those same files this run would likely just re-find what's already PR'd.

So this run targeted the two lane files that pass had *not* read in full: `src/live_trading.py`
(985 lines — only its `ENABLE_SHORTS` startup warning (#103) and its signal-exit debounce (#129)
had been touched by prior PRs) and `src/scientific_strategy.py` in full (not just the scoring
path — the confidence-tier tables, `stop_loss_pct`/`take_profit_pct`, position sizing). Also
confirmed (`git merge-tree`) that all 14 open dispatch-lane branches merge cleanly against
current master — the backlog isn't blocked by conflicts, it's just unmerged; nothing actionable
for dispatch there beyond what #130 already said.

## What I found

`LiveTrader.reconcile_positions()` (src/live_trading.py) records an untracked Kraken position
found at startup — the normal path after a restart while a position was still open. Before this
run, it did not adjust `self.account.initial_capital` when doing so.

`get_summary()`'s `total_equity = initial_capital + total_pnl + unrealized_pnl`. That formula is
correct for a position opened *during* the session: `initial_capital` is snapshotted from
`get_balance()` (free USD) before the position exists, so the cash the position later consumes
is already baked into that snapshot, and the algebra cancels exactly (verified by hand: cash_now
= initial_capital − open_size_usd + total_pnl; position_value_now = open_size_usd + unrealized;
sum = initial_capital + total_pnl + unrealized, independent of open_size_usd).

That cancellation breaks for a *reconciled* position, because `get_balance()` runs at session
start, i.e. free cash was already reduced by whatever the position cost the last time it was
opened (a previous run) — `initial_capital` never included it in the first place. Net effect:
every equity figure derived from `get_summary()` — the dashboard state file, the daily-loss
circuit breaker's baseline in a fresh session, the session-end Telegram summary — silently
understated true account equity by exactly that position's principal, for the entire time it
stayed open (the gap only closes once the position exits and `total_pnl` catches up).

Confirmed this is real trading-loop code, not dead: `src/bot.py` imports and calls
`run_live_trading_session` (via `_run_live_mode`), gated behind its own safety flag — dormant by
default per CLAUDE.md's paper-mode status, but not unreachable code.

Checked it isn't already covered: `fix/live-reconcile-fail-safe` and
`fix/live-reconcile-price-fallback` (two other open branches with "reconcile" in the name) diff
empty against current master — both already merged, and both were about different failure modes
(refusing to swallow a fetch error, and the ticker-price fallback) that are already present in
`reconcile_positions()` today. Neither touched `initial_capital` accounting.

## Fix

One line: when `reconcile_positions()` adds an untracked position, `self.account.initial_capital
+= size_usd` (the position's entry value), restoring the same invariant an in-session position
open gets for free. Not a gating/sizing change — `compute_position_size()`'s `current_equity`
input (`initial_capital + total_pnl`, no unrealized term) already excludes *all* open-position
value by design regardless of this bug, so new-entry sizing is unaffected either way. This is
purely a `total_equity` reporting fix — no gate loosened, no entry made easier, per CLAUDE.md's
core principle.

## Tests

New: `TestReconcilePositions::test_untracked_position_principal_included_in_equity` — reconciles
a $1000-free-cash trader against a $100 exchange position with zero price movement, asserts
`initial_capital` grows by the position's value and `get_summary()['total_equity'] == 1100.0`
(previously would have read `1000.0`, silently dropping the $100).

`python -m pytest tests/test_live_trading.py -q` → 97 passed (was 96).
`python -m pytest tests/ -q` → **3585 passed, 0 failed** (was 3584 passed, 0 failed — CLAUDE.md's
"2 known pre-existing fails" note is still stale on current master, as #130 already flagged;
not re-fixing that doc note here to avoid adding a second unrelated diff to this PR).

## Backlog note (unchanged from #130)

Still 11+ directional-lane PRs open and unmerged (now 12 with this one), none in conflict with
current master. Repeating: a merge pass would raise the value of future dispatch runs more than
another narrow fix does at this point, but that's the owner's call, not dispatch's.

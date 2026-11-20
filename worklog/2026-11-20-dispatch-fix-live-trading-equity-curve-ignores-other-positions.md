---
date: 2026-11-20
agent: dispatch
branch: dispatch/fix-live-trading-equity-curve-ignores-other-positions
pr: pending (opened this run)
lane: directional
files: [src/live_trading.py]
---

# `run_live_trading_session`'s dashboard equity-curve points still ignored other open positions' unrealized P&L — two call sites PR #148 missed

## This run

1. `git fetch && checkout master && pull` — fast-forwarded 17 commits. Since the last
   worklog entry visible on master (day 35 status pass, `b0bb82e`), four more directional-lane
   dispatch PRs merged: #148 (entry-sizing unrealized-PnL fix), #151 (close_long fill
   verification), #155 (live_trading missing atr_alive gate), #162 (`_atr_alive` zero-ATR
   fail-open fix). Good news buried in the backlog note below.
2. Read `CLAUDE.md`, `WORKLOG.md`, `python scripts/worklog_index.py`, and the open PR list via
   the GitHub MCP tools (43 → still ~40 open PRs, oldest #100 from 2026-08-20, now 92 days).
3. `data/` is still not present in this cloud container (gitignored; real paper/live state
   lives only on the Hetzner VPS) — option (a), forward-test analysis, remains unreachable
   from here, consistent with every prior run back to 2026-09-15.
4. Installed deps, confirmed baseline: `python -m pytest tests/ -q` → **3653 passed, 0 failed**
   (up from 3639 on day 35 — the 4 freshly-merged PRs added tests, no regressions).
5. Rather than re-run an 11th generic fresh-eyes sweep of the 7 lane files (10+ prior runs have
   exhausted that and found nothing new per #179-#182), I diffed exactly what changed in the 4
   PRs that merged since the last run (`git diff b0bb82e..d4b487c`) and re-read the surrounding
   code in `src/live_trading.py` those PRs touched, on the theory that recently-changed code is
   more likely to still hold an undiscovered sibling bug than code 10 sweeps have already picked
   over.

## What I found and fixed

`src/live_trading.py`, two call sites inside `run_live_trading_session`:

- `_sltp_watcher()` (was line 543): after a stop-loss/take-profit close.
- the main tick loop's signal-reversal exit handler (was line 746): after a `SELL` signal
  closes a long.

Both computed the point appended to the dashboard's `equity_curve` array as:

```python
equity = trader.account.initial_capital + trader.account.total_pnl
```

— realized equity only, silently dropping the unrealized P&L of every *other* currently-open
position (this bot trades multiple symbols concurrently). This is the exact bug class PR #148
(merged this run) already fixed at the entry-sizing call site (`current_equity` for
`compute_position_size`) and the still-open PR #140 is fixing at the `close_long()` Telegram
notifier call site — but neither touched these two `equity_curve` append sites, which are a
third and fourth instance of the same hand-rolled formula. Confirmed via `git diff master
pr140-check -- src/live_trading.py` that #140's diff only touches the notifier's `total_equity`
line (now ~396) and is unrelated to these two.

**Failure scenario:** bot holds two open positions, BTC and ETH. ETH sits on a -$40 unrealized
loss. A stop-loss fires on BTC and closes it. The dashboard's equity-curve point recorded at
that instant reports equity as if the ETH drawdown didn't exist — overstating real net worth
on the chart an operator would check to decide whether to intervene. (Same class as #148's
scenario, just feeding the equity *chart* instead of a sizing decision — still a real reporting
gap on the real-money path, just not a trading-gate issue.)

**Fix:** both sites now call `trader.get_summary()['total_equity']`, the same already-correct,
already-tested method the entry-sizing call site (line 717) and `_atr_alive`'s neighboring code
already use. No new formula, no behavior change to any entry/exit gate — pure delegation to
existing tested code, same pattern #148 used.

## Verification

- `get_summary()`'s unrealized-PnL inclusion is covered by
  `TestGetSummary.test_equity_includes_unrealized_pnl`; routing both sites through it means
  that test now also guards their correctness. Did not add a new test exercising the async
  `_sltp_watcher`/main-loop sites directly — same call made in #148's worklog: the equity_curve
  array is a local variable inside `run_live_trading_session`'s closure, not exposed for
  inspection without exchange/websocket mocking disproportionate to a two-line fix that only
  swaps in an already-tested method.
- `python -m pytest tests/test_live_trading.py -q` → 109 passed.
- `python -m pytest tests/ -q` (full suite) → **3653 passed, 0 failed** — same count as the
  pre-change baseline this run, no regressions.

## Why this is safe / in scope

Pure correctness fix to a reporting value (dashboard equity-curve chart) — does not touch,
loosen, or add any entry/exit gate (`atr_alive`, cooldown, confidence threshold, signal-exit
debounce, etc.) per CLAUDE.md's core principle. No trading decision is affected; only a
display number is corrected to match what `get_summary()` already computes correctly.

## Backlog note

Mixed signal this run: 4 directional-lane PRs merged since the last status pass (first real
movement in the backlog in a while — the owner is reviewing), but ~40 PRs remain open and #100
is now 92 days old. Not escalating — this is the same condition prior runs already flagged, now
partially improving rather than purely stale. No notification sent.

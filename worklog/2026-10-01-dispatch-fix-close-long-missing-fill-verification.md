---
date: 2026-10-01
agent: dispatch
branch: dispatch/fix-close-long-missing-fill-verification
pr: (opened this run)
lane: directional
files: [src/live_trading.py, tests/test_live_trading.py]
---

# `close_long()` deleted position tracking without verifying the sell actually filled — `open_long()` verifies, `close_long()` didn't

## What I did this run

1. `git fetch && checkout master && pull` — fast-forwarded cleanly, no new commits in the
   directional lane since the 2026-09-30 run (PR #150).
2. Read `CLAUDE.md`, `WORKLOG.md`, `python scripts/worklog_index.py`, and the last three
   dispatch worklog entries (2026-09-16, 09-17, 09-30 — read from their still-open branches,
   since they haven't merged to master).
3. Checked the open PR backlog via GitHub MCP: **30 open PRs**, ~22 directional-lane
   `dispatch/*` drafts (#100–#150), oldest (#100) now 42 days unmerged. This has already
   been flagged repeatedly (#139, #142, re-confirmed-but-not-resent on #149/#150) — not
   re-notifying again this run since nothing has changed since the 2026-09-30 note.
4. Delegated a focused read-only hunt (Explore subagent) across all 7 lane files, giving it
   the full list of ~22 already-covered issues from open PRs so it wouldn't re-find them,
   and pointed it at `live_trading.py`'s order execution/retry path specifically (least
   scrutinized part of that file — prior fixes there were about equity/sizing/debounce, not
   fill verification) and the `paper_trading.py` ⇄ `entry_checklist.py` seam.

## What I found

`LiveTrader.open_long()` (`src/live_trading.py`) verifies a buy order actually filled before
ever recording a position: it reads `status = order.get('status', '')`, and if that's empty
(a known Kraken quirk — market orders can return `status=''` before the fill is fully
processed), polls `fetch_order()` once; if the status still isn't `'closed'`/`'filled'` after
that, it logs, alerts, and returns `None` **without** touching `self.positions`.

`close_long()` had no equivalent check. It placed the sell order and went straight from the
raw order response to computing PnL, appending a `Trade`, and `del self.positions[symbol]` —
unconditionally, with no `status` variable at all.

This is the same *shape* of bug as several already-fixed items in this lane (one of two
mirrored code paths has a safety check the other lacks), but on a distinct axis — "entry
verifies fill, exit doesn't" rather than the long/short asymmetries PRs #148–#150 fixed — and
touches a different concern (order execution, not accounting/sizing).

**Why it matters:** if a sell order comes back with an ambiguous/empty status, pre-fix
`close_long()` would (1) compute `exec_price` from a stale fallback (`current_price`, the
price when the close was *attempted*, not what actually filled) and a possibly-fabricated
`exit_fee`, (2) record a `Trade` with that fabricated PnL into `total_pnl` — corrupting the
daily-loss circuit breaker's session baseline and the ML training journal — and (3)
unconditionally delete the position from `self.positions`. That last part is the dangerous
one: `_sltp_watcher()` iterates exactly `self.positions` every second to apply stop-loss/
take-profit. If the sell didn't actually execute, the position is still live on Kraken but
the bot now believes it's flat, so SL/TP protection silently stops applying to a real,
still-open position until the next restart's `reconcile_positions()` happens to catch it (no
fixed restart cadence).

## Fix

Mirrored `open_long()`'s pattern in `close_long()`: capture `status`/`order_id` from the sell
order response; if `status == ''` and there's an `order_id`, poll `fetch_order()` once (using
the polled average/fee when available); if `status` still isn't in `('closed', 'filled')`,
log + notify and `return None` **without** deleting `self.positions[symbol]` or appending a
`Trade`/updating `total_pnl` — the position stays tracked so `_sltp_watcher` keeps protecting
it and the next loop iteration retries the close, the same stance the adjacent
`CircuitBreakerOpen` branch already takes ("position stays open and will be retried").

No gate was loosened — this doesn't touch `atr_alive`, cooldown, kill filters, the circuit
breaker, or any entry-side check; it only makes the exit path refuse to report success it
hasn't confirmed, same risk-reducing direction as `open_long()`'s existing check.

## Reachability in production today

Dormant, not live: `src/bot.py::_run_live_mode()` refuses to start `live_trading.py`'s engine
unless `DIRECTIONAL_ENABLED=1`, which is `0` in production per CLAUDE.md (the directional
engine's proof scorecard reads t=-8.82). This bug sits in real-money-path code that would
matter the instant that flag flips (or on any future live deployment of this engine) — the
VPS's actual position protection today comes from the paper-trading loop, which is unaffected
by anything in `live_trading.py`.

## Tests

New `TestCloseLongStatusVerification` (6 cases, mirroring the existing
`TestOpenLongStatusVerification`): explicit `'closed'` status skips the poll; empty status
triggers exactly one `fetch_order()` call; poll confirming `'closed'` still closes the
position and records the trade (regression guard — the fix must not break the happy path);
poll still empty keeps the position open with no trade/PnL recorded; `fetch_order()` raising
keeps the position open; a non-empty non-terminal status (`'open'`) is rejected without
polling.

`python -m pytest tests/test_live_trading.py -q` → **103 passed** (was 97; all 6 new).
`python -m pytest tests/ -q` → **3640 passed, 0 failed** (was 3634 passed, 0 failed — no
regressions, no pre-existing failures found on current master, consistent with every prior
run since 2026-09-15 flagging CLAUDE.md's "2 known pre-existing fails" note as stale).

## Backlog note (unchanged — not re-notifying this run per the 2026-09-30 run's own call)

Still ~22 directional-lane PRs open and unmerged, oldest 42 days (#100). All merge cleanly
against current master per prior runs' `git merge-tree` checks; the bottleneck is review/
merge bandwidth, not conflicts. Flagging once more for visibility but treating it as already
said, not re-escalating, since the situation is unchanged since the last two notices.

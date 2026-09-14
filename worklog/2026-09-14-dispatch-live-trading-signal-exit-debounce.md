---
date: 2026-09-14
agent: dispatch
branch: dispatch/live-trading-signal-exit-debounce
pr: TBD
lane: directional
files: [src/live_trading.py, tests/test_live_trading.py]
---

# `live_trading.py` closed real positions on a single opposing signal — paper's own debounce fix never made it to the real-money path

Scheduled "continue the profitability mandate" pass. `git fetch` + `git status` showed
master unchanged at `dc7b95d` (same base as #126/#127/#128) — nothing new merged since
yesterday's run. The directional-lane fix/doc queue is unchanged and still unmerged:
#100–#105, #121, #123, #126, #128 (10 PRs now, oldest 25 days). Repeating another
backlog status-pass would add an 11th "nothing changed" data point with zero new
information, so this run went looking for one more new, small, safe fix instead
(option b), same as #128 did yesterday.

## What I found

Dispatched an Explore audit over `src/live_trading.py` (985 lines) and `src/paper_trading.py`
(2891 lines) — the two largest, least-picked-over files in the lane — explicitly
excluding every bug pattern already reported in the 10 open PRs above, to avoid
duplicate work. It surfaced a real one, which I verified by hand before touching code:

**`src/live_trading.py`'s signal-reversal exit had no debounce at all.** The module's
own docstring says live trading "uses the same ScientificStrategy pipeline as
paper_trading." It does, on the entry side. But `paper_trading.py` (lines ~1316–1317,
~2229–2278) requires **`SIGNAL_EXIT_STREAK = 2` consecutive opposing signal ticks**
before closing a position on a signal reversal, with an explicit comment citing why:
*"Backtests showed 25/33 signal-flip exits hit a 4% win rate — a single opposing bar is
mostly noise."* `live_trading.py`'s equivalent block (`run_live_trading_session`, was
lines 684–690) had none of that — it called `trader.close_long(...)` the instant a
single `Signal.SELL` tick arrived while a long was open. So the real-money engine was
exposed to exactly the noise-driven-exit failure mode paper trading was specifically
patched to avoid, on the one path where a bad exit costs actual dollars.

This engine is currently dormant in production — `bot.py._run_live_mode` refuses to
start unless `DIRECTIONAL_ENABLED=1` is set (the directional engine is shelved per the
proof scorecard, -$0.088 expectancy / t=-8.82 / 229 trades) — but the code exists and
someone could flip that env var, or the safety gate could itself be revisited later. It
should not silently regress to a worse-tested exit rule than paper's the moment it does.

## What I changed

Zero gate loosening — this only makes an exit *more* conservative, matching the already
evidence-backed behavior on the paper side:

- Added module-level `SIGNAL_EXIT_STREAK = 2` (was a paper-only local constant) and two
  small standalone helpers, `_debounce_signal_exit(symbol, opposing_streak, threshold)`
  and `_reset_signal_exit_streak(symbol, opposing_streak)`, following the same
  extract-for-testability pattern this file already uses for `_kill_switch_engaged` /
  `_daily_loss_halted` (pure state-machine functions the async loop calls, so tests
  don't need to drive the full `while trader.running` loop with mocked exchanges/WS).
- Wired both into `run_live_trading_session`'s "LONG EXIT (signal reversed)" branch:
  first opposing tick increments the streak and logs `[LIVE EXIT-DEBOUNCE]` without
  closing; the position only actually closes once the streak reaches
  `SIGNAL_EXIT_STREAK`. Any tick with an open position that ISN'T an opposing signal
  resets the streak to zero, mirroring paper's "any tick without an opposing signal
  resets the streak" `else` branch exactly — so an old partial streak from an earlier,
  unrelated reversal can't combine with a later one and fire early.
- Updated the module docstring to state the new exit-debounce guarantee.
- Did **not** touch the separate SL/TP watcher's `close_long` calls (`STOP_LOSS`/
  `TAKE_PROFIT` reasons) — those are price-trigger exits, not signal-reversal exits, and
  are out of scope here; paper_trading treats them identically (no debounce on
  SL/TP either).
- Did **not** try to also fix the pre-existing (and pre-existing-in-paper-too) quirk
  where a stale, un-popped streak count could survive a SL/TP close and slightly
  shorten the debounce for the *next* position opened on the same symbol — this exact
  same latent behavior already exists in `paper_trading.py` (SL/TP exits there don't
  reset `opposing_streak` either), so porting it faithfully preserves parity rather than
  introducing a new divergence. Flagging it here in case a future pass wants to fix both
  sides together.

## Tests

Added `TestDebounceSignalExit` (6 cases: single tick doesn't fire, second consecutive
tick fires, threshold matches the module constant, custom threshold, per-symbol
independence, counter doesn't clamp past threshold) and `TestResetSignalExitStreak`
(4 cases: clears existing, missing-symbol no-op, only clears the named symbol, and a
regression guard reproducing the exact bug — a fresh reversal after reset starts the
count over rather than inheriting an old partial streak). Both classes are pure unit
tests against the extracted helpers, no asyncio loop needed.

**Verification:** fresh Python 3.12 venv (this sandbox's default `python3` is 3.11;
`pandas-ta` needs ≥3.12 for one CI matrix leg, same workaround #128 used).
- `python -m pytest tests/test_live_trading.py -q` → **106 passed** (was 96; +10 new).
- `python -m pytest tests/ -q` → **3594 passed, 0 failed** (was 3584 on master — the
  +10 new tests, no regressions elsewhere).

## Recommendation (restated for continuity, unchanged from #127)

Merge #100–#105, #121, #123, #126, #128 — nine small, independently-tested,
currently-green, conflict-free directional-lane fixes/docs, none touching `atr_alive`
or any other cost/EV gate — plus this one, which is the first of the run that touches
real money-path (dormant) exit logic rather than paper/docs.

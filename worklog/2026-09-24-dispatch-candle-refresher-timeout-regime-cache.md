---
date: 2026-09-24
agent: dispatch
branch: dispatch/fix-candle-refresher-timeout-regime-cache
pr: pending (opened this run)
lane: directional
files: [src/live_trading.py, tests/test_live_trading.py]
---

# `_candle_refresher`'s 90s-timeout fallback never refreshed `regime_cache` (real-money path)

## What I did this run

1. `git fetch && checkout master && pull` — already up to date (last commit `0083496`,
   PR #134, from the 2026-09-17/18 migration work).
2. Read `CLAUDE.md` in full, `WORKLOG.md`, and `python scripts/worklog_index.py`.
3. Pulled the open-PR backlog via the GitHub MCP tools: 27 open PRs, all `dispatch/*`
   PRs still sitting as **GitHub drafts** (confirmed via #139's 2026-09-22 finding,
   reconfirmed here on #140 — still open, still draft, CI green, one status comment
   from 2026-09-23 saying it's waiting on the owner to convert+merge). Given three
   consecutive prior status passes (#119, #130, #139-equivalent) already reached "lane
   is heavily mined," and PRs keep landing new bugs anyway (#131, #133, #140 all found
   something in the last two weeks), I didn't default straight to another no-op status
   pass — see below for the backlog-drain flag I'm still carrying forward.
4. Baseline `python -m pytest tests/ -q` (after `pip install -r requirements.txt`,
   `pytest`, `pytest-asyncio` — fresh container has neither): **3608 passed, 0 failed**.
   Confirms CLAUDE.md/WORKLOG.md's "2 known pre-existing fails" note is still stale
   (flagged by #139 on 2026-09-22; still not touched, still low-value relative to
   backlog churn — leaving it for whoever eventually does a docs-cleanup pass).
5. Delegated a focused Explore-agent hunt for one new, small, safe bug in the lane
   files, explicitly excluding every issue already covered by open/merged PRs
   #100–#140 (full exclusion list given to the agent, one line per PR). Asked it to
   read `src/live_trading.py` in full (least-covered file relative to its size) and
   the parts of `src/paper_trading.py` away from the already-covered dual-direction
   probe / funding-kill-filter code.

## What I found and fixed

**`src/live_trading.py::run_live_trading_session`'s nested `_candle_refresher()`**
(lines 562–599 pre-fix) has two ways to refresh `ohlcv_cache`:

- **Primary path** (lines 572–579): a real candle event arrives from `public_ws`
  within the 90s `asyncio.wait_for` window → refetches OHLCV *and* calls
  `trader.regime_detector.detect(...)`, updating `regime_cache[sym]`.
- **Fallback path** (`except asyncio.TimeoutError`, lines 587–599, pre-fix): the WS
  candle feed has gone quiet for 90s → refetches OHLCV for every symbol, but never
  called `regime_detector.detect(...)` — `regime_cache` was left exactly as it was
  before the stall.

The main tick loop reads `regime_cache.get(symbol, {})` every iteration (line 644) to
get `regime_name`/`regime_conf`, which feed directly into `trader.strategy.evaluate(...)`
— including the CRASH-blocks-longs hard rule in `scientific_strategy.py` and the
confidence scoring. So a stalled WS candle feed silently froze the regime used for
every entry decision, on the **real-money path**, for as long as the feed stayed
quiet — while price data (`ohlcv_cache`) kept refreshing normally, so nothing else
about the tick loop looked broken.

`src/paper_trading.py`'s sibling `_candle_refresher` (lines 1446–1539) has the
identical two-path shape and gets this right in **both** its WS-timeout fallback
(1520–1533) and its no-`public_ws` fallback — it explicitly calls
`regime_persist.update(sym, regime_detector.detect(...))` on every refresh, with a
comment explaining why ("so the cache doesn't go permanently stale / empty when the
WS event path isn't delivering candles"). The live-trading version is missing that
line in its own fallback branch — an omission relative to its own already-correct
paper-trading counterpart, same bug shape as #131/#140 (paper-trading fixed first,
live-trading path grew a gap on the same logic later).

**Fix:** `src/live_trading.py` — in the `except asyncio.TimeoutError:` branch, after
refreshing `ohlcv_cache[sym]`, also call `trader.regime_detector.detect(ohlcv_cache[sym])`
and update `regime_cache[sym]` from its `.to_dict()`, mirroring the primary path
exactly (3 lines added, no other logic touched).

## Verification

- Added `TestCandleRefresherTimeoutFallback` (`tests/test_live_trading.py`): drives
  `run_live_trading_session` as a background task with a `public_ws` whose
  `candle_queue.get()` always raises `asyncio.TimeoutError` (forcing only the
  fallback branch to ever run), polls until `trader.regime_detector.detect` has been
  called, then cancels the task. Confirmed this **fails on pre-fix code**
  (`assert 0 >= 1`, i.e. `detect()` never called) via `git stash -- src/live_trading.py`
  and re-running, then confirmed it passes with the fix restored.
- `python -m pytest tests/test_live_trading.py -q -k TestCandleRefresherTimeoutFallback`
  → 1 passed (0.17s — no real-time sleeps needed; the background task is cancelled
  once the assertion condition is observed, so the test doesn't wait through the
  main loop's real `EVAL_INTERVAL` sleep).
- Full suite: `python -m pytest tests/ -q` → **3609 passed, 0 failed** (3608 baseline
  + 1 new test, no regressions).

## Why this is safe / in scope

Pure correctness fix — brings the live-trading fallback path to parity with its own
paper-trading counterpart's already-correct behavior. Does not touch `atr_alive`,
cooldowns, the kill switch, or any cost-aware gate threshold; does not change how
often the bot evaluates or enters, only ensures the regime input to that evaluation
stays fresh during a WS stall instead of silently freezing. Per CLAUDE.md's core
principle, this is the opposite direction of risk — it fixes a case where stale
state could feed a decision, it does not loosen any filter to force more trades.

## Backlog status (carried forward from #119 / #130 / #139)

Still unresolved: every `dispatch/*` PR remains a GitHub **draft**, which blocks
merge until a human converts it — confirmed again this run on #140 (green CI,
zero review threads, a 2026-09-23 status comment explicitly asking the owner to
convert+merge, still sitting untouched). 27 open PRs total, oldest (#99) now 36 days
unmerged. This run's own PR will land in the same state. Flagging again since this
is now a 4-runs-running finding with no owner action yet — not something dispatch
can fix itself (converting a PR out of draft or merging is a explicit human/owner
action, not a code change in this lane).

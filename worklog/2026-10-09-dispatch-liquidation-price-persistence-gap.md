---
date: 2026-10-09
agent: dispatch
branch: dispatch/fix-liquidation-price-persistence-gap
pr: (opened this run)
lane: directional
files: [src/paper_trading.py, tests/test_position_persistence.py]
---

# A restart silently dropped a perp position's liquidation price, disabling the liquidation guard until that position closed

## Start-of-run checks

`git fetch && checkout master && pull` — up to date at `021135b` (unchanged since
2026-09-28; no merge activity since). Read `CLAUDE.md` in full, `WORKLOG.md`, and
`python scripts/worklog_index.py`.

Checked the open PR backlog via GitHub MCP before doing anything else: **~32 open PRs**
on the 7 directional-lane files (#100–#158), oldest (#100) now 50 days unmerged. #152,
#153, #156 already did deep status passes on this exact backlog and #156 already
escalated it via a direct notification (2026-10-06); #157/#158 (2026-10-07/08) both
explicitly decided not to re-escalate since nothing had changed. I checked again this
run — still unchanged (same ~32 count, no new merges, no merge conflicts) — so I'm not
re-escalating either, per that same reasoning. `python -m pytest tests/ -q` on master
baseline: **3634 passed, 0 failed**.

Rather than re-run a 7th read-only status pass, I re-read the open PR titles/diffs for
all 32 to build a precise picture of what's already filed (signal-generation long/short
asymmetries in `scientific_strategy.py`/`entry_checklist.py`/`paper_trading.py`'s
fast-track gate; sign/scale/self-inclusion bugs in `orderflow_ws.py`; several real-money
gaps in `live_trading.py` around sizing, debounce, fill-verification, `atr_alive`; a
staleness-reset bug in `pairs_strategy.py`), then read `src/indicators.py`,
`src/pairs_strategy.py`, `src/orderflow_ws.py`, `src/scientific_strategy.py`,
`src/entry_checklist.py`, and most of `src/live_trading.py`/`src/paper_trading.py`
directly against that list to avoid re-filing anything. Confirmed by hand that every bug
I could independently spot in those files matches an existing open PR — the one new
thing I found was in a different category entirely (state persistence, not signal logic),
below.

## What I found

`src/paper_trading.py`'s `_save_open_positions()` / `_load_open_positions()` pair
persists open positions to `data/open_positions.json` so a restart (deploy, crash
recovery, VPS reboot) can resume them without losing state — `tests/test_position_persistence.py`
already has a thorough round-trip suite for this (`TestPerpStateRoundTrip` covers
`is_perp`, `leverage`, `margin_locked`, `funding_accrued`, `last_funding_ts`). But neither
the save dict nor the `PaperPosition(...)` reconstruction in `_load_open_positions`
included **`liquidation_price`**.

`PaperPosition.liquidation_price` (`src/paper_trader.py`) is a real, actively-used safety
field when the bot runs in leveraged perp mode (`TRADING_MODE=perps`, wired in
`src/bot.py:_run_paper_mode` with `PERPS_LEVERAGE`/`config.yaml`'s `futures.leverage`,
default 3x). It's computed on entry (`execute_buy`/`execute_short`), adjusted every tick
as funding accrues (`accrue_funding` — extensively tested in `tests/test_paper_trader.py`
and `tests/test_paper_trading_perp.py`), and checked every tick in
`update_unrealized_pnl()`:

```python
if pos.is_perp and pos.liquidation_price > 0:
    if pos.side == 'buy'   and p <= pos.liquidation_price: ... # force-close
    elif pos.side == 'short' and p >= pos.liquidation_price: ...
```

Because the field was missing from persistence, every restart while a perp position was
open silently reset `liquidation_price` to the dataclass default `0.0` — which the check
above reads as "no liquidation price" (`> 0` guard), **disabling the liquidation guard
entirely** for that position until it exits some other way (SL/TP/signal). A position
restored right after a restart could then run all the way to zero before anything
stopped it. This is exactly the kind of silently-missing safety gate the module's own
`atr_alive`/kill-switch/circuit-breaker machinery exists to prevent elsewhere — just not
wired into this one persistence path.

**Live blast radius:** real but conditional — only matters when `TRADING_MODE=perps` is
set and a leveraged position is open across a restart. The directional paper session
normally runs spot (`liquidation_price=0.0` by construction, matching the
`test_spot_position_liquidation_price_is_zero` test below), so this doesn't affect the
default configuration, but it's a live path (`bot.py` wires it from an env var, not a
dead/dormant one like several other recent findings in this lane).

## The fix

Two-line addition, mirroring the existing perp-field handling exactly:
- `_save_open_positions`: added `'liquidation_price': pos.liquidation_price` to the
  perp-specific fields block.
- `_load_open_positions`: added `liquidation_price = float(p.get('liquidation_price', 0.0))`
  to the `PaperPosition(...)` reconstruction, defaulting to `0.0` for old snapshots saved
  before this fix (and for spot positions, where it's always 0 anyway) — no behavior
  change for anything that isn't an open perp position.

This tightens an existing, already-built safety check (restores it across restarts) —
it doesn't touch `atr_alive`, sizing, or any cost-aware gate, and doesn't loosen
anything, consistent with CLAUDE.md's core principle.

## Tests

Added `TestLiquidationPriceRoundTrip` to `tests/test_position_persistence.py` (and gave
the module's `_perp_position()` fixture helper a non-zero `liquidation_price`, matching
how every other perp field in that fixture is already set):
- `test_liquidation_price_survives` — perp position round-trips with its
  `liquidation_price` intact.
- `test_spot_position_liquidation_price_is_zero` — spot positions still restore to 0.0.
- `test_old_snapshot_without_field_defaults_to_zero` — a pre-fix snapshot (no
  `liquidation_price` key at all) still loads without raising, defaulting to 0.0 — no
  crash on upgrade.

Verified the first two fail pre-fix: reverted only the `src/paper_trading.py` change
(kept the new tests) and reran — `test_liquidation_price_survives` failed with
`Obtained: 0.0, Expected: 46800.0`, and `test_old_snapshot_without_field_defaults_to_zero`
raised `KeyError: 'liquidation_price'` while constructing its pre-fix fixture (expected,
since that test's whole point — simulating an old snapshot without the key — only makes
sense once the key exists to delete). Reapplied the fix; all pass.

`python -m pytest tests/test_position_persistence.py -q` → **51 passed** (48 existing + 3 new).
`python -m pytest tests/ -q` (full suite) → **3637 passed, 0 failed** (3634 baseline + 3 new,
no regressions).

## Backlog note (unchanged from #156/#157/#158)

Still ~32 open directional-lane PRs, oldest 50 days, confirmed unchanged from the last
few runs' counts. Not re-escalating again this run per #157/#158's reasoning — nothing
about the situation has changed since the 2026-10-06 notification. Continuing to produce
verified, tested, narrowly-scoped fixes regardless of merge velocity, per that run's
conclusion.

# Maker-only microstructure forward arm — the runner that was missing

**Agent:** claude-computer · **Branch:** `feat/micro-maker-forward-arm` · **Lane:** directional
**PR:** pending

## Why

`proof_scorecard._microstructure_forward()` has been reading
`data/micro_paper_state.json` since the maker-only foundation landed. Nothing
ever wrote that file, so the arm has been silently absent from every scorecard
run — `_microstructure_forward()` returns `None` when the path is missing, which
looks identical to "no trades yet".

The foundation existed (`src/maker_fill.py`, `TickCVDTracker`, `obi_from_book`,
`KrakenTradeFeed`/`KrakenBookFeed`). The runner joining them did not. This adds it.

## Context: why this is a new test, not a re-run of a dead one

The microstructure scalper FAILED at t≈-8.82 with **taker** cost on **2s-REST
snapshots** and a **candle-CVD proxy**. `btc-intraday-and-leverage` closes the
door on intraday taker strategies on candles: *"do not re-propose without new
evidence."*

This replaces all three inputs — real tick tape, real book, post-only maker
fills. That is what makes it a legitimately different hypothesis rather than a
prohibited re-run.

## What landed

`micro_paper.py` — long-only, one position, its own state file, honours the
master kill switch, `--dry-run` and `--duration` for offline wire checks.

Four properties carry the arm's honesty, and each has a test:

- **Non-fills are counted, not discarded.** A resting bid that the tape never
  trades down into is cancelled and recorded in `nonfills`. An arm that only
  records its fills is measuring a different, flattering strategy.
- **Adverse selection is free.** It falls out of the fill rule — a resting buy
  fills only when sellers trade down into it — rather than being modelled on top.
- **Stops pay the taker fee.** A maker-only exit can refuse to fill for an
  unbounded time. Charging a stop the maker fee would invent a strategy whose
  downside is cheaper than its upside, so a breached stop crosses the spread at
  0.40% and `exit_kind` records it.
- **Long only.** Maker-only longs on Kraken spot are what a US retail account can
  execute, which is exactly what the proof arm asserts when it marks this
  `executable=True`.

## The finding worth recording before any data arrives

Kraken spot maker is **0.25%/side at tier 0**, so a round trip costs **0.50%**.

**At this fee a maker-only strategy cannot scalp.** The take-profit has to clear
half a percent before the trade is worth taking, which makes this a short-hold
momentum arm that happens to enter passively — not the tick scalper the name
implies. Defaults are TP 0.65% / stop 0.45%, leaving ~0.15% of edge after costs.
Thin, and stated as thin.

A startup guard refuses to run when `MICRO_TAKE_PROFIT` is below the round-trip
cost — negative-EV by construction, and better refused than logged. It caught my
own first default (0.35%), which is the entire argument for having it.

## Verification

- `tests/test_micro_paper.py` — 11 tests, all passing. They pin the fill model
  (no-fill on runaway, fill only on a through-trade, fill at our limit not the
  print, timeout beats a late fill) and the arm's contract (TP guard, taker-fee
  stop, non-fill accounting, atomic state write, long-only).
- Full suite: **3590 passed, 5 failed** — the 5 are the known Windows-only
  pre-existing failures (dashboard/exchange/notifications), untouched by this.
- Round-trip contract check: wrote a synthetic state file, confirmed
  `_microstructure_forward()` reads it, labels it, and reports
  `executable=True` with `n=3`.
- `--dry-run` against live Kraken WS could not connect from this machine
  (sandbox DNS). Reconnect backoff and clean shutdown behaved correctly, which
  is what that run could actually prove.

## Not done — needs the VPS

Kraken WS is unreachable from here, so **no tick has ever passed through this
code path**. The gate thresholds (OBI ≥ 0.58, CVD slope > 0, 40-tick warmup) are
first guesses that have never seen a real tape.

Before this is worth trusting:

1. Run `--dry-run` on the VPS for an hour and check the gate fires at a sane
   rate — neither never nor constantly.
2. Then run it live-paper and watch the **fill rate**. If nearly every resting
   bid cancels unfilled, the arm is structurally dead regardless of signal
   quality, and that is worth knowing in a day rather than in 90.
3. Only then start the 90-day clock.

**Do not expand to other exchanges until this reads PROVEN ✓ over ≥90 days.**

Related: `scalper-microstructure-ofi-v2` · `tick-ofi-cvd-standalone` (killed) ·
`btc-intraday-and-leverage` (sit-out)

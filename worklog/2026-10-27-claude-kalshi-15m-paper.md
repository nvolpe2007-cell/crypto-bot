# 2026-10-27 — claude — kalshi-15m-paper

**Branch:** `research/kalshi-15m-paper`
**Lane:** new independent subsystem (`kalshi_bot/`) — no directional or brain/risk files touched.

Owner goal: a Kalshi 15-minute BTC up/down PAPER bot, judged on winning >65% of
trades AND beating break-even at t>=3 after variants, surviving the one-minute
BTC-delay test. Outcome (B) NO EDGE was an allowed finish.

## Built
- `kalshi_bot/` — read-only public-API client (no key, no order path), fair value
  for Kalshi's 60s-average settlement vs the published `floor_strike`, Kalshi's
  quadratic taker fee, paper book, live paper runner (Kraken spot, basis-corrected).
- `scripts/kalshi_backtest.py` — real settled windows, Kalshi 1m ask/bid history,
  IS/OOS, staleness test, variant count + Sidak bar.
- `tests/test_kalshi_bot.py` — fee rounding, settlement variance, basis removal,
  decision rule, no-lookahead, and a guard that fails on any order/auth code.

## Measured (registry `kalshi-btc-15m-updown`, pre-registered before results)
- 2,848 windows / 30 days. Pre-registered: n=1985, win 39.7% vs break-even 43.0%,
  t=-3.40; negative in both halves. Market Brier beats the model's.
- Verdict: **sit-out**. Runner smoke-tested live but NOT to be installed on the VPS.

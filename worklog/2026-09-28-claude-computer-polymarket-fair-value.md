# 2026-09-28 — claude-computer — polymarket-fair-value

**Branch:** `feat/polymarket-twap-bot` (on top of `claude/vigilant-wozniak-ycaxze`)
**Lane:** new independent subsystem (`polymarket_bot/`) — no directional or brain/risk files touched.

Owner asked to build the Polymarket bot now. The scaffold's signal was a placeholder
`HOLD`; this adds a real one, measures it, and wires a live paper runner.

## Built
- `polymarket_bot/fair_value.py` — endpoint digital + window-TWAP fair value, realized
  vol, documented taker fee `0.07·p·(1−p)` per share, net edge.
- `scripts/polymarket_twap_backtest.py` — 672 real resolved 15m windows (7d), no
  lookahead, one trade per window, fill at price + half-spread, fee charged; includes a
  resolution-rule check and a staleness diagnostic.
- `polymarket_bot/runner.py` — live PAPER loop: fresh Kraken spot + live best ask at the
  same instant, buy at the ask when net edge ≥ 0.03 and depth allows, settle from
  Gamma's resolved outcome, log every decision (traded or not).
- `paper_sim.py` — optional real `entry_price` (ask, not mid) and per-position
  `fee_usd` charged win or lose; legacy behaviour unchanged when unset.
- Fixed a Windows-only path bug in `tests/test_polymarket_live_execution.py`.

## Measured (registry `polymarket-btc-updown-fair-value`)
- Outcomes follow the **end-of-window** price (93.2% match), not the full-window TWAP
  (85.3%) — the "partial TWAP locks the outcome" thesis was wrong.
- Model Brier 0.1379 vs market 0.1383: no calibration edge.
- Headline +10.8%/stake (t=1.90) is a **stale-quote artifact**: withhold one minute of BTC
  data and it flips to −8.8%/stake (t=−1.59, win 45% vs model-said 55%).

## Not done
- Runner is not on the VPS or any cron — needs this PR merged, then a systemd unit.
- Live execution stays out: the CLOB is geoblocked for a US account.
- Local-env quirk: `test_live_without_env_vars_refuses` fails here because the
  subprocess Python lacks `requests`; not a code fault.

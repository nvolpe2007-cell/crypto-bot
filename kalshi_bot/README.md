# kalshi_bot

PAPER bot for Kalshi's CFTC-regulated 15-minute BTC markets (series `KXBTC15M`,
"BTC price up in next 15 mins?"). Kalshi is the legal US venue for this product;
the Polymarket version (`polymarket_bot/`) trades a CLOB that is geoblocked for US
accounts.

**No account, no API key, no order placement.** Every call is an unauthenticated
GET to Kalshi's public market-data API; `tests/test_kalshi_bot.py` fails if any
POST/PUT/DELETE, `/orders`, `/portfolio` or key handling appears in this package.

## Files
- `client.py`: read-only market list, single market, settled history, 1m candles.
- `fair_value.py`: P(Up) for Kalshi's settlement rule (60s BRTI average vs the
  published `floor_strike`), plus Kalshi's quadratic taker fee
  `ceil_cent(0.07·C·P·(1−P))`.
- `paper_sim.py`: paper book in the standard arm-state shape →
  `data/kalshi_paper_state.json` (dashboard auto-discovers it).
- `runner.py`: live paper loop, `python -m kalshi_bot.runner`. Fresh Kraken spot
  (basis-corrected to BRTI at the window open) + live Kalshi ask at the same
  instant; buys when net edge ≥ 0.03; logs every decision to
  `data/kalshi_decisions.jsonl`.

## Research
`scripts/kalshi_backtest.py --days 30` scores the pre-registered config on real
settled windows using Kalshi's own 1-minute ask/bid history, with an IS/OOS split
and the one-minute BTC-delay staleness test. Registry entry:
`kalshi-btc-15m-updown` (pre-registered 2026-10-27, before any result was seen).
The verdict and numbers live there and in the vault, not here.

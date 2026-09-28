# 2026-09-28 — claude-computer — polymarket-5m-orderflow

**Branch:** `research/polymarket-5m-orderflow`
**Lane:** `polymarket_bot/` research subsystem — no directional or brain/risk files touched.

Owner asked for a 5-minute strategy: "find patterns", then BTC price + order flow +
gamma levels + trend regime to call over/under the target in the time remaining.

## Added
- `scripts/polymarket_5m_patterns.py`: 94 pattern cells (price bucket, streaks, hour of
  day) on 4032 5m windows, IS/OOS split, fee + spread charged.
- `scripts/polymarket_5m_features.py`: Kraken-only order flow + trend features computed
  strictly before each Polymarket print; logistic on the market price; IS/OOS.
- `scripts/polymarket_watch.py`: live read-only viewer of the VPS paper arm.
- `tests/test_polymarket_5m_features.py`: includes a no-lookahead test.

## Measured (registry `polymarket-btc-5m-orderflow-trend`)
- Patterns: 0 of 94 survive (best OOS t=+1.01); the top IS cell flipped +45% → −47%.
- Order flow and trend add nothing to the market price.
- Only "spot vs target" / "last-60s move" score (t=3.79), which is the stale-print
  signature. Unproven; the live paper arm on fresh quotes is the deciding test.
- Gamma levels untestable historically; forward logging needs PR #112.

## Deployed separately this session
- `polymarket-paper` systemd unit running on the VPS (owner installed; unit in PR #145).
- BTC GEX paste script for the BackQuant TradingView indicator (PR #146).

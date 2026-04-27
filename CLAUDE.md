# Crypto Bot — Claude Context

## Project Location
`D:\crypto-bot\`

## What This Is
A crypto scalping + arbitrage bot. Currently runs in **paper trading mode** (no real money).

## Exchange & Config
- Exchange: **Kraken** (sandbox mode)
- Trading pairs: BTC/USD, ETH/USD, SOL/USD
- Timeframe: 1-minute candles
- Strategy: EMA crossover (9/21) + RSI (14)
- Initial capital: $100 (paper)

## Key Files
```
src/
  bot.py                  # Main scalping bot entry point
  indicators.py           # EMA, RSI calculations
  paper_trading.py        # Simulated trade execution
  exchange.py             # Kraken API wrapper
  backtester.py           # Historical backtesting
  notifications.py        # Telegram alerts
arbitrage/
  dex_arb.py              # DEX arbitrage scanner
  stablecoin_arb.py       # Stablecoin triangular arb
  funding_rate_arb.py     # Funding rate arbitrage
config.yaml               # All bot settings (pairs, risk, strategy params)
run_all_bots.py           # Launch all bots together
```

## How to Run
```powershell
# Scalping bot only
python -m src.bot

# Backtest
python -m src.backtester

# All bots
python run_all_bots.py
```

## Dependencies
Install once: `pip install -r requirements.txt`
Main libs: ccxt, pandas, pandas-ta, numpy, python-dotenv, PyYAML

## Telegram Alerts
Configured to send buy/sell/error alerts to chat ID: 7553694317

## Status
- Paper trading mode (sandbox: true in config.yaml)
- Two copies exist: C:\Users\User\crypto-bot (older) and D:\crypto-bot (current)

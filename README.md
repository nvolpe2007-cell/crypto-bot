# Crypto Scalping Bot

A Python-based crypto scalping bot for Kraken using EMA crossover + RSI strategy.

## Strategy

**EMA Crossover + RSI Filter**
- **Buy**: Fast EMA (9) crosses above Slow EMA (21) AND RSI < 70 (not overbought)
- **Sell**: Fast EMA crosses below Slow EMA AND RSI > 30 (not oversold)
- **Timeframe**: 1-minute candles
- **Pairs**: BTC/USD, ETH/USD, SOL/USD

## Quick Start

### 1. Install Dependencies

```bash
cd crypto-bot
pip install -r requirements.txt
```

### 2. Configure

Copy `.env.example` to `.env`:
```bash
cp .env.example .env
```

Edit `config.yaml` to adjust:
- Strategy parameters (EMA periods, RSI thresholds)
- Risk settings (position size, stop loss, take profit)
- Trading mode (`backtest`, `paper`, or `live`)

### 3. Run Backtest

```bash
# Edit config.yaml first:
# mode: backtest
# start_date: "2024-06-01"
# end_date: "2024-12-31"

python -m src.backtester
```

Or run directly:
```bash
python -c "from src.backtester import run_backtest, print_backtest_report; import asyncio; r = asyncio.run(run_backtest('BTC/USD', '2024-06-01', '2024-12-31')); print_backtest_report(r)"
```

### 4. Run Paper Trading

```bash
# Edit config.yaml: mode: paper
python -m src.bot
```

## Project Structure

```
crypto-bot/
├── config.yaml          # Main configuration
├── .env                 # API keys (create from .env.example)
├── requirements.txt     # Dependencies
├── src/
│   ├── __init__.py
│   ├── exchange.py      # Kraken API wrapper
│   ├── indicators.py    # EMA + RSI calculations
│   ├── backtester.py    # Historical backtesting
│   ├── paper_trading.py # Simulated trading
│   └── bot.py           # Main orchestrator
└── logs/                # Trade logs
```

## Configuration Options

### Strategy Parameters (config.yaml)
```yaml
strategy:
  fast_ema: 9           # Fast EMA period
  slow_ema: 21          # Slow EMA period
  rsi_period: 14        # RSI calculation period
  rsi_overbought: 70    # RSI overbought threshold
  rsi_oversold: 30      # RSI oversold threshold
```

### Risk Management
```yaml
risk:
  max_position_size: 50   # USD per trade
  stop_loss_pct: 2.0      # Stop loss at -2%
  take_profit_pct: 3.0    # Take profit at +3%
  max_daily_loss: 10      # Stop trading after -$10 loss
```

## Trading Modes

| Mode | Description | Risk |
|------|-------------|------|
| `backtest` | Test on historical data | None |
| `paper` | Simulated live trading | None |
| `live` | Real money trading | High |

**Always start with backtest → paper → live**

## Important Notes

1. **This is experimental software** - Do not trade money you can't afford to lose
2. **Backtest first** - The backtester uses historical Kraken data to test your strategy
3. **Paper trade** - Run paper trading for at least a week before considering live trades
4. **Start small** - When going live, start with minimum position sizes

## Troubleshooting

**No data fetched**: Kraken API may be rate limiting. Wait a few minutes and retry.

**Backtest shows no trades**: Your strategy parameters may be too strict. Try:
- Lowering RSI overbought threshold
- Adjusting EMA periods
- Testing on a different time period

**Paper trading not executing**: Ensure you have enough capital for the position size + fees.

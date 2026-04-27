# Crypto Bot - Quick Start Guide

## Your bots are saved at: `D:\crypto-bot\`

---

## Step 1: Install Python (one time)

1. Go to https://python.org/downloads
2. Download Python 3.11 or 3.12
3. **IMPORTANT**: Check ✅ "Add Python to PATH" during install
4. Click Install

---

## Step 2: Install Dependencies (one time)

Open PowerShell and run:
```powershell
cd D:\crypto-bot
python -m pip install -r requirements.txt
```

---

## Step 3: Run the Scalping Bot

```powershell
cd D:\crypto-bot
python -m src.bot
```

This runs in **paper trading mode** (no real money).

---

## Step 4: Run All Bots (optional)

```powershell
python run_all_bots.py
```

This runs:
- Scalping bot (EMA/RSI strategy)
- DEX arbitrage scanner
- Stablecoin triangular arb
- Funding rate arb

---

## Files You Have

```
D:\crypto-bot\
├── .env                    # Your API keys (already configured)
├── config.yaml             # Bot settings
├── src/
│   ├── bot.py              # Main bot
│   ├── advanced_indicators.py  # Enhanced strategy
│   ├── backtester.py       # Test on historical data
│   └── ...
├── arbitrage/
│   ├── dex_arb.py          # DEX arbitrage
│   ├── stablecoin_arb.py   # Stablecoin triangle
│   └── funding_rate_arb.py # Funding rate arb
└── START_HERE_TOMORROW.md  # This file
```

---

## Commands Reference

| Command | What it does |
|---------|--------------|
| `python -m src.bot` | Run scalping bot |
| `python -m src.backtester` | Run backtest on historical data |
| `python -m src.advanced_backtester` | Compare basic vs advanced strategy |
| `python run_all_bots.py` | Run all bots together |
| `python -m arbitrage.dex_arb` | Run DEX arb only |

---

## Telegram Bot

Your bot is configured to send alerts to your Telegram:
- Bot: Created via @BotFather
- Chat ID: 7553694317

You'll get messages for:
- 🟢 Buy signals
- 🔴 Sell signals
- 📊 Status updates
- 🚨 Errors

---

## When You Get a VPS

1. Create Hetzner VPS (https://hetzner.cloud)
2. Upload files: `scp -r D:\crypto-bot\* root@<VPS_IP>:/opt/crypto-bot/`
3. SSH in and run the same commands

---

## Questions?

Just ask your AI assistant to help with:
- "How do I change the strategy parameters?"
- "How do I view my trade history?"
- "How do I stop the bot?"
- etc.

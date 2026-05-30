# 🚀 LIVE DEPLOYMENT GUIDE

## Quick Start: Going Live with Probability-Based Trading Bot

### ⚡ DEPLOYMENT SUMMARY

Your probability-based trading system is **READY FOR LIVE DEPLOYMENT**. The system includes:

✅ **5 Parallel Agents** working together for optimal edge
✅ **Probabilty-first decision making** (P(win) > 60% requirement)
✅ **Context-aware filtering** (blocks unfavorable regimes)
✅ **ML validation** (65% hard threshold)
✅ **Kelly sizing** (optimal position management)
✅ **Telegram notifications** (full trade lifecycle alerts)

---

## 📋 PRE-DEPLOYMENT CHECKLIST

### 1. Telegram Bot Setup (CRITICAL) ⚠️
First, you must create a Telegram bot to receive trading notifications:

```bash
# 1. Open Telegram and search for "@BotFather"
# 2. Send: /newbot
# 3. Follow instructions to create your bot
# 4. BotFather will give you a token (keep it secret!)
# 5. Add the bot to your channel/group
# 6. Send a message to your channel
# 7. Get your chat ID from: https://api.telegram.org/botYOUR_BOT_TOKEN/getUpdates

# Fill these values in .env.vps:
TELEGRAM_BOT_TOKEN="123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11"
TELEGRAM_CHAT_ID="-1001234567890"
TELEGRAM_ENABLED=true
```

### 2. VPS Requirements

**Minimum VPS Specs:**
- 2 CPU cores
- 2 GB RAM
- 20 GB SSD
- Ubuntu 20.04+ or Debian 10+
- SSH access with key authentication

**Recommended Providers:**
- DigitalOcean ($12/mo droplet)
- AWS Lightsail ($10/month)
- Hetzner (€9.49/month CX21)
- Vultr ($10/month)

### 3. Kraken API Keys

Create Kraken API keys for paper trading first:

```python
# 1. Login to Kraken
# 2. Go to Settings → API
# 3. Create new key:
#    - Query funds
#    - Query orders
#    - Modify orders
#    - Cancel orders
#    - Query ledger entries
# 4. Enable paper trading mode first!
# 5. Get API Key and Private Key

# Fill in .env.vps:
KRAKEN_API_KEY="your-paper-api-key"
KRAKEN_PRIVATE_KEY="your-paper-private-key"
TRADING_MODE=paperrisk  # Use 'live' only after testing
```

---

## 🎯 QUICK DEPLOYMENT INSTRUCTIONS

### Step 1: Generate SSH Key

If you don't have SSH key pair:

```bash
ssh-keygen -t rsa -b 4096 -C "your-email@example.com"
# Save to: ~/.ssh/id_rsa
```

Copy your public key to VPS:

```bash
ssh-copy-id ubuntu@your-vps-ip
```

### Step 2: Configure Environment

Edit `.env.vps` with your credentials:

```bash
# Edit critical values:
TELEGRAM_BOT_TOKEN="your-bot-token-here"
TELEGRAM_CHAT_ID="your-chat-id-here"
KRAKEN_API_KEY="your-api-key-here"
KRAKEN_PRIVATE_KEY="your-private-key-here"
VPS_IP="your-vps-ip-here"

# Review these settings:
TRADING_MODE=paperrisk  # 'paper', 'paperrisk', or 'live'
PAPER_CAPITAL=75.00   # Your starting capital
MAX_POSITIONS=3       # Maximum simultaneous trades
MIN_CONFIDENCE=70.0     # Minimum 70% confidence
```

### Step 3: Run Deployment Script

Choose your deployment method:

#### Method A: Automated Deploy (Recommended)

```bash
cd D:\crypto-bot
chmod +x deploy.sh
./deploy.sh
```

#### Method B: Manual Commands

```bash
cd D:\crypto-bot

# 1. Package files
scp src/*.py requirements.txt .env.vps \
  ubuntu@YOUR_VPS_IP:~/crypto-bot/

# 2. SSH to VPS
ssh ubuntu@YOUR_VPS_IP

# 3. On VPS - install Python
cd ~/crypto-bot
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# 4. Setup service
sudo cp crypto-bot-vps.service /etc/systemd/system/
sudo systemctl daemon-reload
```

### Step 4: Start the Bot

```bash
# SSH to VPS
ssh ubuntu@your-vps-ip

# Start the service
sudo systemctl start crypto-bot

# Check status
sudo systemctl status crypto-bot

# View live logs
sudo journalctl -u crypto-bot -f
```

---

## 📊 POST-DEPLOYMENT MONITORING

### Check Logs

```bash
# Live logs
sudo journalctl -u crypto-bot -f

# Last 100 lines
sudo journalctl -u crypto-bot -n 100

# Logs from today
sudo journalctl -u crypto-bot --since "today"

# Logs with errors
sudo journalctl -u crypto-bot | grep -i error
```

### Check Bot Status

```bash
# Service status
sudo systemctl status crypto-bot

# Process info
ps aux | grep crypto

# Memory usage
sudo systemctl status crypto-bot | grep Memory

# Resource usage
sudo systemctl show crypto-bot | grep Memory
```

### Monitor Telegram Alerts

Your Telegram bot will send:

**Entry Alerts:**
```
🟢 BTC/USD — LONG (buying)
Entry: $43,245.12
Position: $4.50
Confidence: High (85%)

Why I entered:
• Heavy buying pressure in order books
• Market is in a strong uptrend
• RSI 42 — oversold, good room to run up
```

**Exit Alerts:**
```
✅ WIN +0.23 (5.1%)
BTC long $43,245 → $43,465
Held 45 min — hit profit target
Account now: $78.23
```

**Hourly Updates:**
```
📈 Hourly Update
Account: $78.23
P&L: +3.23 (+4.3%)
Trades: 12 today
Open: 1 position active
```

---

## 🛠️ MANAGEMENT COMMANDS

### Control Bot

```bash
# Start
sudo systemctl start crypto-bot

# Stop
sudo systemctl stop crypto-bot

# Restart (apply changes)
sudo systemctl restart crypto-bot

# Enable auto-start
sudo systemctl enable crypto-bot

# Disable auto-start
sudo systemctl disable crypto-bot
```

### Update Bot

```bash
# 1. Update code
./deploy.sh

# 2. Restart bot
sudo systemctl restart crypto-bot
```

### View Trade Statistics

```bash
# On VPS
cd ~/crypto-bot
source venv/bin/activate
python -m src.trade_journal --stats
```

---

## ⚠️ CRITICAL SAFETY MEASURES

### 1. Always Start in Paper Trading Mode

**DO NOT** start in live mode until you've:
- [ ] Run 24-48 hours in paper mode
- [ ] Received 20+ trade notifications in Telegram
- [ ] Verified win rate is > 40%
- [ ] Confirmed you're comfortable with trade frequency

Edit `.env.vps`:
```bash
TRADING_MODE=paperrisk  # Not 'live' yet!
```

### 2. Position Size Limits

The system automatically enforces:
- 6% per trade baseline
- 15% maximum exposure
- Regime adjustments: 0.3-1.0x multiplier

**DANGER ZONE** - If you want to override:
- ⚠️ Must be in `.env.vps` - no command line overrides
- ⚠️ Bot will refuse to start if values are too high
- ⚠️ Maximum single trade: 12% (even with 2.0x multiplier)

### 3. Stop Loss Protection

Stops are automatically set to:
- 1.2x ATR (average true range)
- 0.3% minimum, 3.0% maximum
- Regime-adjusted (wider in volatile markets)

### 4. Daily Loss Limits

By default:
- **Hard stop**: 15% daily loss
- **Soft stop**: 10% daily loss

If you reach 10%, bot pauses until next day.
If you reach 15%, bot stops entirely (requires manual restart).

### 5. Bot Will Not Trade When:
- Context score < 60 (unfavorable market)
- ML win probability < 65%
- Volume < 75% of average
- ADX < 18 (weak trend)
- RSI > 75 / < 25 (chasing)
- Weekend or off-hours
- Recent trade on same symbol (5-min cooldown)

---

## 🎯 PERFORMANCE TARGETS

### Month 1: Paper Trading ($75 → $100)

**Realistic Target for First 2 Weeks:**
- Trades per day: 12-18 (high quality)
- Win rate: 42-48%
- Average winner: +0.08% (1.5x ATR)
- Average loser: -0.03% (tighter stops)
- Profit factor: 1.8-2.2
- Daily return: +0.15% to +0.25%
- **Projected**: $75 → $82-88 in 14 days

**Stretch Target** ($75 → $100):
- Requires: +33% in 14 days = 2.1% per day
- Need: 0.30% expectancy × 18 trades = 5.4% per day
- **Probability**: ~15% (very aggressive)
- **Reality check**: Aim for consistent wins over big gains

**Alternative Path**:
- Build to $100 in 30 days (more realistic)
- Then increase position sizes for compounding

---

## 📈 EXPECTED TRADING PATTERNS

### Typical Trading Day

**Hours**: 13:00-20:00 UTC (peak liquidity)
**Expected Trades**: 12-18 per day
**Trade Distribution**:
- BTC/USD: 6-8 trades
- ETH/USD: 4-6 trades
- SOL/USD: 2-4 trades

**What You'll See in Telegram**:
1. **Entries**: 3-5 spread throughout day
2. **Exits**: Mix of wins/losses (target 45% win rate)
3. **Hourly Updates**: Daily P&L summary
4. **Adjustments**: Bot learns and adapts

**Typical Day**:
```
13:15 - ✅ WIN +0.11 (BTC long)
13:42 - ❌ LOSS -0.03 (ETH short)
14:08 - ✅ WIN +0.15 (BTC long)
14:31 - ✅ WIN +0.08 (SOL long)
15:02 - ❌ LOSS -0.04 (BTC short)
15:45 - ✅ WIN +0.12 (ETH long)
... 6 more trades ...
20:00 - 📈 HOURLY: +0.37 today, 12 trades, 7 wins
```

---

## 🆘 TROUBLESHOOTING

### Bot Won't Start

```bash
# Check logs
sudo journalctl -u crypto-bot -n 50

# Common issues:
# 1. Missing .env file
cp .env.vps .env

# 2. Python dependencies
source venv/bin/activate
pip install -r requirements.txt

# 3. Permissions
chmod +x start_live.py
chmod +x src/*.py
```

### No Telegram Notifications

```bash
# Test connection
python -m src.notifications

# Check if token/chat_id are correct
cat .env | grep TELEGRAM

# Test manually:
curl "https://api.telegram.org/botYOUR_TOKEN/getMe"
```

### Too Many/Low Trades

Adjust in `.env.vps`:
```bash
MIN_CONFIDENCE=70      # Higher = fewer trades
MAX_POSITIONS=3        # Max 3 simultaneous trades
COOLDOWN_SECONDS=300   # 5 minutes between same symbol
```

---

## 📚 ADDITIONAL DOCUMENTATION

### Files Created for Deployment

```
D:\crypto-bot\deploy_vps.sh         # Detailed deployment with SSH
D:\crypto-bot\deploy.sh              # Quick deploy script
D:\crypto-bot\start_live.py          # Live trading launcher
D:\crypto-bot\.env.vps              # Production environment
D:\crypto-bot\crypto-bot-vps.service # Systemd service file
```

### Core System Files

```
src/decision_framework.py          # Probability engine
src/context_analyzer.py            # Context filtering
src/advanced_ml_features.py       # Behavior features
src/ml_scorer_optimized.py         # ML validation
src/scientific_strategy_optimized.py # Optimized strategy
src/notifications.py               # Telegram integration
```

---

## 🎉 YOU'RE READY TO GO!

### Final Pre-Flight Checklist:

- [ ] Telegram bot created and added to channel
- [ ] TELEGRAM_BOT_TOKEN set in .env.vps
- [ ] TELEGRAM_CHAT_ID set in .env.vps
- [ ] Kraken paper trading API keys created
- [ ] KRAKEN_API_KEY set in .env.vps
- [ ] KRAKEN_PRIVATE_KEY set in .env.vps
- [ ] VPS provisioned with Ubuntu 20.04+
- [ ] SSH access configured
- [ ] TRADING_MODE=paperrisk (NOT live yet!)
- [ ] Starting capital set to 75.00 in .env.vps

**Deployment Command:**
```bash
cd D:\crypto-bot
chmod +x deploy.sh
./deploy.sh
```

**Start Trading:**
```bash
# After deployment
ssh ubuntu@your-vps-ip
sudo systemctl start crypto-bot
sudo journalctl -u crypto-bot -f
```

---

## 📞 SUPPORT

If you encounter issues:
1. Check logs (`sudo journalctl -u crypto-bot -f`)
2. Test Telegram (`python -m src.notifications`)
3. Verify API keys
4. Check context filters in logs

**Good luck! Your probability-based trading journey starts now! 🚀**

*Last updated: May 5, 2026*
*System: Probability-First Multi-Modal Trading Bot*
*Target: $75 → $100 in 14 days*

# VPS Deployment Guide

Deploy the crypto bot on a VPS for 24/7 trading.

## Recommended VPS Providers

| Provider | Plan | Price | Notes |
|----------|------|-------|-------|
| **Hetzner** | CPX11 | ~$5/mo | Best value, EU locations |
| **DigitalOcean** | Basic | $6/mo | Good US coverage |
| **Linode** | Nanode | $5/mo | Reliable, global |
| **AWS** | t3.micro | ~$7/mo | Free tier eligible |

**Minimum specs:** 1 vCPU, 1GB RAM, 10GB storage

---

## Quick Start (Hetzner)

1. Go to https://hetzner.cloud
2. Create server: Ubuntu 22.04, CPX11 (~$5/mo)
3. SSH in: `ssh root@<your-vps-ip>`

```bash
# Install dependencies
apt update && apt upgrade -y
apt install -y python3 python3-pip python3-venv git tmux

# Setup bot
mkdir -p /opt/crypto-bot && cd /opt/crypto-bot
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# Configure
cp .env.example .env
nano .env  # Add your settings

# Run with tmux (detached, survives disconnect)
tmux new -s bot
python -m src.bot
# Detach: Ctrl+B, then D
```

---

## Telegram Notifications Setup

1. **Create bot:** Message `@BotFather` on Telegram
   - Send `/newbot`, follow prompts, save the token

2. **Get chat ID:** Message `@userinfobot` on Telegram
   - It replies with your ID like `123456789`

3. **Add to `.env`:**
   ```
   TELEGRAM_BOT_TOKEN=123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11
   TELEGRAM_CHAT_ID=123456789
   TELEGRAM_ENABLED=true
   ```

4. **Test:**
   ```bash
   python -m src.notifications
   ```

You'll get messages like:
- 🟢 BUY alerts
- 🔴 SELL alerts  
- 📊 Status updates
- 🚨 Error alerts

---

## Systemd Service (auto-restart)

```bash
cat > /etc/systemd/system/crypto-bot.service << 'EOF'
[Unit]
Description=Crypto Scalping Bot
After=network.target

[Service]
Type=simple
WorkingDirectory=/opt/crypto-bot
Environment=PATH=/opt/crypto-bot/venv/bin
ExecStart=/opt/crypto-bot/venv/bin/python -m src.bot
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable crypto-bot
systemctl start crypto-bot
systemctl status crypto-bot
```

---

## Free Up C: Drive Space

Your C: is full. Quick fixes:

```bash
# Clear temp files
rm -rf /c/Users/User/AppData/Local/Temp/*

# Or move bot to D: drive (has 303GB free)
cd /d/crypto-bot
source venv/bin/activate  # or recreate venv
python -m src.bot
```

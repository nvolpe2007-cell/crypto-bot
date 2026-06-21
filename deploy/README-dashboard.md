# Dashboard + tournament — install on the VPS

Both are **standalone and read-only** — they never touch the `crypto-bot`
service, only read the `data/` files it writes. Install after this branch is
merged and pulled to `/opt/crypto-bot` (normal deploy pipeline).

## 1. Live dashboard (systemd service)

```bash
sudo cp /opt/crypto-bot/deploy/crypto-dashboard.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now crypto-dashboard
systemctl status crypto-dashboard          # should be active
curl -s localhost:8787/healthz             # -> ok
```

It binds **127.0.0.1** only. View it from your laptop over an SSH tunnel:

```bash
ssh -L 8787:127.0.0.1:8787 crypto-bot-vps
# then open http://localhost:8787
```

(LAN exposure: drop in a systemd override setting `DASHBOARD_HOST=0.0.0.0` and
firewall the port — not recommended.)

## 2. Strategy tournament (cron, like the other single-shot arms)

Re-scores 100+ candidates daily and writes `data/tournament.json`, which the
dashboard picks up automatically. Matches the repo convention (swing_paper /
pairs_paper are cron single-shots).

```bash
crontab -e
# daily at 05:30 UTC:
30 5 * * * cd /opt/crypto-bot && ./venv/bin/python scripts/run_tournament.py >> data/tournament.log 2>&1
```

Run once now to populate the board immediately:

```bash
cd /opt/crypto-bot && ./venv/bin/python scripts/run_tournament.py
```

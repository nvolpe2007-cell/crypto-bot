"""
stockbot's OWN Telegram poster — independent of the crypto bot's notifier (so the
crypto mute, CRYPTO_TELEGRAM_MUTE, never silences it, and vice-versa).

Dependency-light (urllib only). Opt-in + fail-safe: posts only when
STOCKBOT_TELEGRAM is truthy AND a token + chat id are present; any error → False
(never raises into a backtest run).

Env:
  STOCKBOT_TELEGRAM=1            enable posting
  TELEGRAM_BOT_TOKEN=...         bot token (shared with the crypto bot is fine)
  STOCKBOT_TELEGRAM_CHAT_ID=...  chat id; falls back to TELEGRAM_CHAT_ID
"""
from __future__ import annotations

import json
import os
import urllib.request
from typing import List

from .strategy import Trade


def enabled() -> bool:
    return os.getenv("STOCKBOT_TELEGRAM", "").strip().lower() in ("1", "true", "yes", "on")


def post(text: str) -> bool:
    """Send one HTML message. Returns True on success, False if disabled/misconfigured/failed."""
    if not enabled():
        return False
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat = (os.getenv("STOCKBOT_TELEGRAM_CHAT_ID", "").strip()
            or os.getenv("TELEGRAM_CHAT_ID", "").strip())
    if not token or not chat:
        return False
    try:
        payload = json.dumps({"chat_id": chat, "text": text, "parse_mode": "HTML"}).encode()
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data=payload, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as r:
            return 200 <= r.status < 300
    except Exception:
        return False


def format_report(symbol: str, s: dict, trades: List[Trade],
                  capital: float | None = None) -> str:
    """Compact '<b>stock money</b>' summary for Telegram: the P&L + verdict, and the
    $ figure if a notional `capital` is given (backtest returns are %, so $ = % × capital)."""
    from .metrics import verdict
    if s["n"] == 0:
        return f"📈 <b>{symbol} ORB</b> — no trades."
    net_pct = s["total"] * 100
    money = f"  (${net_pct/100*capital:+,.2f} on ${capital:,.0f})" if capital else ""
    last = trades[-1] if trades else None
    last_line = (f"\nlast: {last.date} {last.side} {last.net_ret*100:+.2f}%"
                 if last else "")
    return (
        f"📈 <b>{symbol} ORB — stock P&amp;L</b>\n"
        f"net <b>{net_pct:+.2f}%</b>{money} over {s['n']} trades\n"
        f"exp {s['expectancy']*100:+.3f}%/trade · win {s['win_rate']*100:.0f}% · "
        f"t={s['t_stat']:.2f}\n"
        f"{verdict(s)}{last_line}"
    )

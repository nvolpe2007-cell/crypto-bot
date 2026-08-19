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

Also used by orb_alerts.py for the interactive setup-alert flow: post_photo()
sends a chart with inline "trade for me" / "I'll take it" buttons, and
get_updates()/answer_callback_query()/edit_message_reply_markup() are the poll
loop that reads which button (if any) got pressed.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
import uuid
from typing import List, Optional

from .strategy import Trade


def enabled() -> bool:
    return os.getenv("STOCKBOT_TELEGRAM", "").strip().lower() in ("1", "true", "yes", "on")


def _creds() -> tuple[str, str] | tuple[None, None]:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat = (os.getenv("STOCKBOT_TELEGRAM_CHAT_ID", "").strip()
            or os.getenv("TELEGRAM_CHAT_ID", "").strip())
    return (token, chat) if (token and chat) else (None, None)


def post(text: str) -> bool:
    """Send one HTML message. Returns True on success, False if disabled/misconfigured/failed."""
    if not enabled():
        return False
    token, chat = _creds()
    if not token:
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


def _multipart(fields: dict, file_field: str, filename: str, file_bytes: bytes,
              mime: str = "image/png") -> tuple[bytes, str]:
    """Minimal multipart/form-data encoder (stdlib only). Returns (body, boundary)."""
    boundary = uuid.uuid4().hex
    parts: List[bytes] = []
    for name, value in fields.items():
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n'
            f'{value}\r\n'.encode())
    parts.append(
        f'--{boundary}\r\nContent-Disposition: form-data; name="{file_field}"; '
        f'filename="{filename}"\r\nContent-Type: {mime}\r\n\r\n'.encode()
        + file_bytes + b'\r\n')
    parts.append(f'--{boundary}--\r\n'.encode())
    return b''.join(parts), boundary


def post_photo(caption: str, png_bytes: bytes, reply_markup: Optional[dict] = None
              ) -> Optional[int]:
    """Send a PNG with an HTML caption and optional inline keyboard. Returns the
    sent message_id on success, None if disabled/misconfigured/failed."""
    if not enabled():
        return None
    token, chat = _creds()
    if not token:
        return None
    fields = {"chat_id": chat, "caption": caption, "parse_mode": "HTML"}
    if reply_markup is not None:
        fields["reply_markup"] = json.dumps(reply_markup)
    body, boundary = _multipart(fields, "photo", "chart.png", png_bytes)
    try:
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendPhoto", data=body,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
        with urllib.request.urlopen(req, timeout=20) as r:
            data = json.loads(r.read())
            return data.get("result", {}).get("message_id") if data.get("ok") else None
    except Exception:
        return None


def get_updates(offset: Optional[int] = None, timeout: int = 0) -> List[dict]:
    """Poll for new updates (used to detect inline-button presses). Pass the last
    processed update_id + 1 as `offset` to avoid re-seeing old updates. Returns
    [] if disabled/misconfigured/failed — never raises."""
    if not enabled():
        return []
    token, _ = _creds()
    if not token:
        return []
    params = {"timeout": timeout, "allowed_updates": json.dumps(["callback_query"])}
    if offset is not None:
        params["offset"] = offset
    qs = urllib.parse.urlencode(params)
    try:
        req = urllib.request.Request(f"https://api.telegram.org/bot{token}/getUpdates?{qs}")
        with urllib.request.urlopen(req, timeout=timeout + 10) as r:
            data = json.loads(r.read())
            return data.get("result", []) if data.get("ok") else []
    except Exception:
        return []


def answer_callback_query(callback_query_id: str, text: str = "") -> bool:
    """Ack a button press (stops the client's loading spinner)."""
    if not enabled():
        return False
    token, _ = _creds()
    if not token:
        return False
    try:
        payload = json.dumps({"callback_query_id": callback_query_id, "text": text}).encode()
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/answerCallbackQuery",
            data=payload, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as r:
            return 200 <= r.status < 300
    except Exception:
        return False


def edit_message_reply_markup(chat_id: str, message_id: int,
                              reply_markup: Optional[dict] = None) -> bool:
    """Remove/replace a message's inline keyboard (used after a button is handled,
    so a second tap can't double-fire the action)."""
    if not enabled():
        return False
    token, _ = _creds()
    if not token:
        return False
    try:
        payload = json.dumps({
            "chat_id": chat_id, "message_id": message_id,
            "reply_markup": reply_markup or {"inline_keyboard": []},
        }).encode()
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/editMessageReplyMarkup",
            data=payload, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as r:
            return 200 <= r.status < 300
    except urllib.error.HTTPError as e:
        # "message is not modified" etc. — not actionable, treat as handled
        return e.code == 400
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

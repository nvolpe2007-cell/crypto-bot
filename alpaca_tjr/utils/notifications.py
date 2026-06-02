"""Telegram trade alerts — optional, disabled by default."""
from __future__ import annotations

import logging
import requests

logger = logging.getLogger(__name__)


class Notifier:
    def __init__(self, token: str, chat_id: str, enabled: bool = False):
        self._token = token
        self._chat_id = str(chat_id)
        self._enabled = enabled and bool(token) and bool(chat_id)

    def send(self, text: str) -> None:
        if not self._enabled:
            return
        url = f"https://api.telegram.org/bot{self._token}/sendMessage"
        try:
            requests.post(
                url,
                json={"chat_id": self._chat_id, "text": text, "parse_mode": "HTML"},
                timeout=5,
            )
        except Exception as exc:
            logger.warning("Telegram send failed: %s", exc)

    def entry(self, symbol: str, side: str, entry: float, stop: float,
              target: float, qty: float, setup: str) -> None:
        rr = abs(target - entry) / max(abs(entry - stop), 1e-9)
        self.send(
            f"<b>ENTRY {side.upper()} {symbol}</b>\n"
            f"Setup: {setup}\n"
            f"Entry: {entry:.4f}  Stop: {stop:.4f}  Target: {target:.4f}\n"
            f"Qty: {qty}  R:R {rr:.1f}:1"
        )

    def exit(self, symbol: str, side: str, entry: float, exit_price: float,
             reason: str, pnl: float) -> None:
        emoji = "+" if pnl >= 0 else ""
        self.send(
            f"<b>EXIT {symbol}</b>\n"
            f"Reason: {reason}\n"
            f"Entry: {entry:.4f}  Exit: {exit_price:.4f}\n"
            f"P&L: {emoji}{pnl:.2f}"
        )

    def daily_summary(self, trades: int, wins: int, pnl: float, equity: float) -> None:
        win_rate = (wins / trades * 100) if trades else 0
        self.send(
            f"<b>Daily Summary</b>\n"
            f"Trades: {trades}  Wins: {wins} ({win_rate:.0f}%)\n"
            f"Day P&L: {pnl:+.2f}  Equity: {equity:.2f}"
        )

    def alert(self, message: str) -> None:
        self.send(f"<b>ALERT</b> {message}")

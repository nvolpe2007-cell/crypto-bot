"""
Telegram Notifications for Crypto Bot
Sends trade alerts to your phone via Telegram
"""

import logging
import requests
from typing import Optional
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class TelegramConfig:
    bot_token: str
    chat_id: str
    enabled: bool = True


class TelegramNotifier:
    """
    Send trading notifications to Telegram

    Setup:
    1. Create bot via @BotFather on Telegram
    2. Get your chat ID via @userinfobot
    3. Add token and chat_id to .env
    """

    def __init__(self, bot_token: str, chat_id: str, enabled: bool = True):
        self.token = bot_token
        self.chat_id = chat_id
        self.enabled = enabled
        self.base_url = f"https://api.telegram.org/bot{bot_token}"

        if enabled:
            logger.info(f"Telegram notifications enabled for chat {chat_id}")

    def send_message(self, message: str, parse_mode: str = "HTML") -> bool:
        """Send a message to Telegram"""
        if not self.enabled:
            logger.debug(f"Notification suppressed: {message[:50]}")
            return False

        try:
            url = f"{self.base_url}/sendMessage"
            payload = {
                "chat_id": self.chat_id,
                "text": message,
                "parse_mode": parse_mode
            }
            response = requests.post(url, json=payload, timeout=10)
            response.raise_for_status()
            logger.debug(f"Telegram message sent: {message[:50]}...")
            return True
        except Exception as e:
            logger.error(f"Failed to send Telegram message: {e}")
            return False

    def send_trade_alert(self, action: str, symbol: str, price: float,
                         size: float, pnl: Optional[float] = None,
                         reason: str = ""):
        """Send formatted trade alert"""
        if action.upper() == "BUY":
            emoji = "🟢"
        elif action.upper() == "SELL":
            emoji = "🔴"
        else:
            emoji = "⚪"

        if pnl is not None:
            pnl_emoji = "✅" if pnl >= 0 else "❌"
            message = (
                f"{emoji} <b>{action.upper()}</b>\n\n"
                f"Pair: <code>{symbol}</code>\n"
                f"Price: <code>${price:.2f}</code>\n"
                f"Size: <code>${size:.2f}</code>\n"
                f"PnL: <code>${pnl:+.2f}</code> {pnl_emoji}\n"
                f"Reason: {reason}"
            )
        else:
            message = (
                f"{emoji} <b>{action.upper()}</b>\n\n"
                f"Pair: <code>{symbol}</code>\n"
                f"Price: <code>${price:.2f}</code>\n"
                f"Size: <code>${size:.2f}</code>\n"
                f"Reason: {reason}"
            )

        return self.send_message(message)

    def send_signal(self, symbol: str, signal: str, price: float,
                    rsi: float, ema_fast: float, ema_slow: float):
        """Send signal notification with indicator values"""
        emoji = "📈" if signal == "BUY" else "📉" if signal == "SELL" else "⏸️"

        message = (
            f"{emoji} <b>{signal} SIGNAL</b>\n\n"
            f"Pair: <code>{symbol}</code>\n"
            f"Price: <code>${price:.2f}</code>\n"
            f"RSI: <code>{rsi:.1f}</code>\n"
            f"EMA Fast: <code>{ema_fast:.2f}</code>\n"
            f"EMA Slow: <code>{ema_slow:.2f}</code>"
        )

        return self.send_message(message)

    def send_win(self, symbol: str, pnl: float, pnl_pct: float,
                 exit_price: float, total_equity: float, reason: str = ""):
        """Send a WIN notification with money emojis."""
        money_emojis = "💰" * min(10, max(1, int(pnl // 5) + 1))
        message = (
            f"💸💸💸 <b>WIN</b> 💸💸💸\n\n"
            f"{money_emojis}\n\n"
            f"<b>+${pnl:.2f}</b>  ({pnl_pct:+.2f}%)\n\n"
            f"Pair:   <code>{symbol}</code>\n"
            f"Exit:   <code>${exit_price:.2f}</code>\n"
            f"Reason: {reason}\n\n"
            f"💼 Account: <b>${total_equity:.2f}</b>"
        )
        return self.send_message(message)

    def send_loss(self, symbol: str, pnl: float, pnl_pct: float,
                  exit_price: float, total_equity: float, reason: str = ""):
        """Send a loss notification."""
        message = (
            f"🔴 <b>LOSS</b>\n\n"
            f"<b>${pnl:.2f}</b>  ({pnl_pct:+.2f}%)\n\n"
            f"Pair:   <code>{symbol}</code>\n"
            f"Exit:   <code>${exit_price:.2f}</code>\n"
            f"Reason: {reason}\n\n"
            f"💼 Account: <b>${total_equity:.2f}</b>"
        )
        return self.send_message(message)

    def send_status(self, capital: float, pnl: float, pnl_pct: float,
                    open_positions: int, trades_today: int):
        """Send periodic status update"""
        emoji = "📈" if pnl >= 0 else "📉"
        money = "💰" if pnl >= 0 else "🔴"

        message = (
            f"📊 <b>BOT STATUS</b>\n\n"
            f"{money} PnL: <b>${pnl:+.2f}</b> ({pnl_pct:+.2f}%) {emoji}\n"
            f"💼 Account: <code>${capital:.2f}</code>\n"
            f"📂 Open:    <code>{open_positions}</code>\n"
            f"🔁 Trades:  <code>{trades_today}</code>"
        )

        return self.send_message(message)

    def send_error(self, error_message: str):
        """Send error alert"""
        message = f"🚨 <b>ERROR ALERT</b>\n\n<code>{error_message}</code>"
        return self.send_message(message)

    def test_connection(self) -> bool:
        """Test Telegram connection"""
        return self.send_message("✅ <b>Bot connected!</b>\n\nTelegram notifications are working.")


def create_notifier_from_env() -> TelegramNotifier:
    """Create notifier from environment variables"""
    import os
    from dotenv import load_dotenv

    load_dotenv()

    token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
    enabled = os.getenv("TELEGRAM_ENABLED", "false").lower() == "true"

    if not token or not chat_id:
        logger.warning("Telegram not configured - notifications disabled")
        return TelegramNotifier("", "", enabled=False)

    return TelegramNotifier(token, chat_id, enabled)


if __name__ == '__main__':
    # Test Telegram connection
    import os
    from dotenv import load_dotenv

    load_dotenv()

    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")

    if token and chat_id:
        notifier = TelegramNotifier(token, chat_id)
        print("Testing Telegram connection...")
        if notifier.test_connection():
            print("Success! Check your Telegram.")
        else:
            print("Failed to send message.")
    else:
        print("Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env")

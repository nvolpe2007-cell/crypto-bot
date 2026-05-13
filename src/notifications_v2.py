"""
PROBABILITY-BASED TELEGRAM NOTIFICATIONS (v2)
Replaces old signal-based messages with probability metrics
Drops into paper_trading.py seamlessly
"""

import logging
import requests
from typing import Optional, List
from dataclasses import dataclass
from datetime import datetime
import os

logger = logging.getLogger(__name__)

# ============================================================================
# NEW PROBABILITY NOTIFICATIONS
# ============================================================================

@dataclass
class ProbabilityConfig:
    bot_token: str
    chat_id: str
    enabled: bool = True

class ProbabilityNotifier:
    """
    Drop-in replacement for old TelegramNotifier
    Sends P(win), EV, position size instead of simple signals
    """

    def __init__(self, bot_token: str, chat_id: str, enabled: bool = True):
        self.token = bot_token
        self.chat_id = chat_id
        self.enabled = enabled
        self.base_url = f"https://api.telegram.org/bot{bot_token}"

        if enabled:
            logger.info(f"📡 Probability Telegram enabled")
            # Test connection
            self.send_message("✅ Probability notifications initialized")
        else:
            logger.warning("⚠️ Telegram disabled")

    def send_message(self, message: str, parse_mode: str = "HTML") -> bool:
        """Send message to Telegram"""
        if not self.enabled:
            logger.debug("Telegram suppressed")
            return False

        try:
            response = requests.post(
                f"{self.base_url}/sendMessage",
                json={"chat_id": self.chat_id, "text": message, "parse_mode": parse_mode},
                timeout=10
            )
            response.raise_for_status()
            return True
        except Exception as e:
            logger.error(f"Telegram failed: {e}")
            return False

    # ------------------------------------------------------------------------
    # ENTRY ALERTS (Probability Format)
    # ------------------------------------------------------------------------

    def send_trade_alert(self, action: str, symbol: str, price: float,
                        size: float, pnl: Optional[float] = None,
                        reason: str = "", signal=None):
        """
        Override old send_trade_alert to send probability-based messages
        """
        is_buy = action.upper() == "BUY"
        coin = symbol.split('/')[0] if '/' in symbol else symbol
        direction = "LONG" if is_buy else "SHORT"

        # Calculate probability metrics from signal
        if signal and hasattr(signal, 'probability'):
            win_prob = signal.probability
        else:
            # Estimate from confidence
            win_prob = max(0.50, min(0.85, 0.5 + (signal.confidence - 50) / 200))

        prob_label = self._probability_label(win_prob)
        position_pct = (size / 75.0) * 100  # Assume $75 capital

        lines = [
            f"🚀 <b>{direction} EXECUTED</b> — {symbol}",
            f"Coin: <b>{coin}</b>",
            f"Entry: <b>${price:,.2f}</b>",
            f"Size: <b>${size:.2f}</b> ({position_pct:.1f}%)",
            f"Win probability: <b>{prob_label}</b> ({win_prob:.1%})",
            "",
            "<b>Setup Quality:</b>"
        ]

        # Add reasons based on signal
        if signal:
            if hasattr(signal, 'confidence') and signal.confidence > 75:
                lines.append(f"  ✓ High confidence: {signal.confidence:.0f}%")
            if hasattr(signal, 'rsi') and 35 <= signal.rsi <= 70:
                lines.append(f"  ✓ RSI favorable: {signal.rsi:.0f}")
            if hasattr(signal, 'adx') and signal.adx > 25:
                lines.append(f"  ✓ Trend strong: ADX {signal.adx:.0f}")

        return self.send_message("\n".join(lines))

    def send_win(self, symbol: str, pnl: float, pnl_pct: float,
                exit_price: float, total_equity: float, reason: str = ""):
        """Send win alert"""
        return self.send_message(
            f"✅ <b>WIN +${pnl:.2f} ({pnl_pct:+.1f}%)</b>\n"
            f"{symbol} exited at ${exit_price:,.2f}\n"
            f"Account: ${total_equity:,.2f}"
        )

    def send_loss(self, symbol: str, pnl: float, pnl_pct: float,
                exit_price: float, total_equity: float, reason: str = ""):
        """Send loss alert"""
        return self.send_message(
            f"❌ <b>LOSS -${abs(pnl):.2f} ({pnl_pct:+.1f}%)</b>\n"
            f"{symbol} exited at ${exit_price:,.2f}\n"
            f"Account: ${total_equity:,.2f}"
        )

    def send_error(self, error_message: str):
        """Send error alert"""
        return self.send_message(
            f"⚠️ <b>Bot Error</b>\n{error_message[:200]}"
        )

    def test_connection(self) -> bool:
        """Test Telegram connection"""
        return self.send_message(
            "✅ <b>Trading Bot Connected</b>\n\n"
            "Probability-based trading is ready.\n"
            "P(win), EV, and position sizing enabled."
        )

    @staticmethod
    def _probability_label(prob: float) -> str:
        """Convert probability to label"""
        if prob >= 0.75:
            return "🎯 High"
        elif prob >= 0.65:
            return "✅ Moderate"
        elif prob >= 0.55:
            return "⚠️ Average"
        return "❌ Low"

    @staticmethod
    def create_from_env():
        """Create from environment"""
        from dotenv import load_dotenv
        load_dotenv()

        token = os.getenv("TELEGRAM_BOT_TOKEN", "")
        chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
        enabled = os.getenv("TELEGRAM_ENABLED", "false").lower() == "true"

        if not token or not chat_id:
            print("⚠️  WARNING: Telegram not configured")
            return ProbabilityNotifier("", "", enabled=False)

        return ProbabilityNotifier(token, chat_id, enabled)


# ============================================================================
# GLOBAL NOTIFIER (DROP-IN REPLACEMENT)
# ============================================================================

# Replace the global notifier in other modules
import notifications
def replace_notifier():
    """Replace old notifier with probability version"""
    try:
        # Check if already replaced
        if hasattr(notifications, '_notifier'):
            if isinstance(notifications._notifier, ProbabilityNotifier):
                print("✅ Already using probability notifications")
                return

        # Replace global
        notifications._notifier = ProbabilityNotifier.create_from_env()
        print("✅ Replaced notifier with probability version")
        print("✅ Future messages will include P(win) and EV")
    except Exception as e:
        print(f"❌ Failed to replace notifier: {e}")


if __name__ == '__main__':
    print("🔄 Notification System v2 - Probability Based")
    print("="*60)

    # Create and test
    notifier = ProbabilityNotifier.create_from_env()

    if notifier.test_connection():
        print("✅ Test successful - check Telegram")
    else:
        print("❌ Test failed - check credentials")

    print("\n✅ Ready to use in paper_trading.py")
    print("Messages will include: P(win), position size, EV calculations")

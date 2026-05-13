"""
Probability-Based Telegram Notifications
Sends P(win), EV, position size, and quality metrics.
"""

import logging
import os
import requests
from typing import Optional, List, Dict
from dataclasses import dataclass
from datetime import datetime

logger = logging.getLogger(__name__)


@dataclass
class ProbabilityTelegramConfig:
    bot_token: str
    chat_id: str
    enabled: bool = True


class ProbabilityTelegramNotifier:
    """
    Probability-based trade notifications.
    Includes: P(win), EV, position size, decision quality.
    Also implements the full TelegramNotifier interface so paper_trading.py
    can use it as a drop-in replacement.
    """

    def __init__(self, bot_token: str, chat_id: str, enabled: bool = True):
        self.token = bot_token
        self.chat_id = chat_id
        self.enabled = enabled
        self.base_url = f"https://api.telegram.org/bot{bot_token}"
        if enabled:
            logger.info(f"Probability Telegram enabled for chat {chat_id}")
        else:
            logger.warning("Telegram disabled — no alerts will be sent")

    # ── Core send ─────────────────────────────────────────────────────────────

    def send_message(self, message: str, parse_mode: str = "HTML") -> bool:
        if not self.enabled:
            logger.debug(f"Telegram suppressed: {message[:50]}")
            return False
        try:
            response = requests.post(
                f"{self.base_url}/sendMessage",
                json={"chat_id": self.chat_id, "text": message, "parse_mode": parse_mode},
                timeout=10,
            )
            response.raise_for_status()
            logger.info(f"Telegram sent: {message[:60]}...")
            return True
        except Exception as e:
            logger.error(f"Telegram failed: {e}")
            return False

    def test_connection(self) -> bool:
        return self.send_message(
            "✅ <b>Bot Connected!</b>\n\n"
            "Probability-based trading is ready.\n"
            "P(win), EV, and ML scoring enabled."
        )

    # ── TelegramNotifier-compatible interface ─────────────────────────────────
    # These methods match what paper_trading.py calls on the notifier.

    def send_trade_alert(self, action: str, symbol: str, price: float,
                         size: float, pnl: Optional[float] = None,
                         reason: str = "", signal=None):
        is_buy = action.upper() == "BUY"
        coin = symbol.split('/')[0]
        direction = "LONG" if is_buy else "SHORT"
        icon = "🟢" if is_buy else "🔴"

        conf = getattr(signal, 'confidence', None)
        prob = max(0.50, min(0.85, 0.5 + (conf - 50) / 200)) if conf else None

        lines = [
            f"{icon} <b>{coin} — {direction} EXECUTED</b>",
            f"Entry: <b>${price:,.2f}</b>",
            f"Size: <b>${size:.2f}</b>",
        ]
        if conf:
            lines.append(f"Confidence: <b>{conf:.0f}%</b>")
        if prob:
            lines.append(f"Win probability: <b>{prob:.1%}</b>")
        if signal and hasattr(signal, 'stop_loss_pct'):
            sl_pct = signal.stop_loss_pct()
            tp_pct = signal.take_profit_pct()
            sl_price = price * (1 - sl_pct / 100) if is_buy else price * (1 + sl_pct / 100)
            tp_price = price * (1 + tp_pct / 100) if is_buy else price * (1 - tp_pct / 100)
            lines.append(f"Stop: ${sl_price:,.2f}  Target: ${tp_price:,.2f}")
        return self.send_message("\n".join(lines))

    def send_win(self, symbol: str, pnl: float, pnl_pct: float,
                 exit_price: float, total_equity: float, reason: str = ""):
        return self.send_message(
            f"✅ <b>WIN +${pnl:.2f} ({pnl_pct:+.1f}%)</b>\n"
            f"{symbol.split('/')[0]} exited at ${exit_price:,.2f}\n"
            f"Account: <b>${total_equity:,.2f}</b>"
        )

    def send_loss(self, symbol: str, pnl: float, pnl_pct: float,
                  exit_price: float, total_equity: float, reason: str = ""):
        return self.send_message(
            f"❌ <b>LOSS -${abs(pnl):.2f} ({pnl_pct:+.1f}%)</b>\n"
            f"{symbol.split('/')[0]} exited at ${exit_price:,.2f}\n"
            f"Account: <b>${total_equity:,.2f}</b>"
        )

    def send_trade_analysis(self, symbol: str, side: str, pnl: float, pnl_pct: float,
                             entry_price: float, exit_price: float, total_equity: float,
                             exit_reason: str, holding_minutes: float,
                             regime: str, regime_conf: float,
                             rsi: float, adx: float, volume_ratio: float,
                             ofi: Optional[float], funding_apy: Optional[float],
                             btc_lead: Optional[str],
                             issues: list, positives: list,
                             loss_streak: int, win_streak: int,
                             adaptations: Optional[list] = None) -> bool:
        is_win = pnl >= 0
        icon = "✅" if is_win else "❌"
        result = f"+${pnl:.2f}" if is_win else f"-${abs(pnl):.2f}"
        label = "WIN" if is_win else "LOSS"
        coin = symbol.split('/')[0]
        held = f"{holding_minutes:.0f} min" if holding_minutes < 60 else f"{holding_minutes/60:.1f} hrs"

        lines = [
            f"{icon} <b>{label} {result} ({pnl_pct:+.1f}%)</b>",
            f"{coin} {side}   ${entry_price:,.2f} → ${exit_price:,.2f}",
            f"Held {held}",
            f"Account: <b>${total_equity:,.2f}</b>",
        ]

        context = []
        if ofi is not None:
            if ofi > 0.20:
                context.append("More buyers than sellers in order books")
            elif ofi < -0.20:
                context.append("More sellers than buyers in order books")
        if regime:
            context.append(f"Market regime: {regime.lower().replace('_', ' ')}")
        if context:
            lines.append("")
            lines.append("<b>Context at entry:</b>")
            for c in context[:3]:
                lines.append(f"  • {c}")

        if loss_streak >= 3:
            lines.append(f"\n⚠️ <b>{loss_streak} losses in a row</b> — entry rules tightened")
        elif win_streak >= 3:
            lines.append(f"\n🔥 {win_streak} wins in a row")

        if adaptations:
            lines.append("<i>Bot self-adjusted: " + "; ".join(adaptations[:2]) + "</i>")

        return self.send_message("\n".join(lines))

    def send_status(self, capital: float, pnl: float, pnl_pct: float,
                    open_positions: int, trades_today: int):
        icon = "📈" if pnl >= 0 else "📉"
        result = f"+${pnl:.2f}" if pnl >= 0 else f"-${abs(pnl):.2f}"
        lines = [
            f"{icon} <b>Hourly Update</b>",
            f"Account: <b>${capital:,.2f}</b>",
            f"P&L: <b>{result} ({pnl_pct:+.1f}%)</b>",
            f"Trades: {trades_today}",
        ]
        if open_positions > 0:
            lines.append(f"Open: {open_positions} position{'s' if open_positions > 1 else ''}")
        else:
            lines.append("Open: watching markets")
        return self.send_message("\n".join(lines))

    def send_error(self, error_message: str):
        return self.send_message(f"⚠️ <b>Bot Error</b>\n{error_message[:200]}")

    # ── Probability-specific alerts ───────────────────────────────────────────

    def send_probability_entry(self, symbol: str, decision, current_price: float,
                                reasons: List[str]) -> bool:
        prob_label = self._probability_label(decision.probability)
        lines = [
            f"🚀 <b>TRADE EXECUTED</b> — {symbol}",
            f"Entry: <b>${current_price:,.2f}</b>",
            f"Win probability: <b>{prob_label}</b> ({decision.probability:.1%})",
            f"Expected value: <b>{decision.expected_value:+.4f}</b>",
            f"Position size: {decision.position_size_pct:.2%}",
        ]
        if reasons:
            lines.append("")
            lines.append("<b>Rationale:</b>")
            for r in reasons[:4]:
                lines.append(f"  • {r}")
        return self.send_message("\n".join(lines))

    def send_probability_exit(self, symbol: str, side: str, entry_price: float,
                               exit_price: float, pnl: float, pnl_pct: float,
                               decision, holding_minutes: float) -> bool:
        is_win = pnl >= 0
        icon = "✅ WIN" if is_win else "❌ LOSS"
        result = f"+${pnl:.2f}" if is_win else f"-${abs(pnl):.2f}"
        lines = [
            f"{icon} <b>{result} ({pnl_pct:+.1f}%)</b> — {symbol}",
            f"{side} ${entry_price:,.2f} → ${exit_price:,.2f}",
            f"Held {holding_minutes:.0f} min",
            f"Predicted win: {decision.probability:.1%}  |  Actual: {'WIN' if is_win else 'LOSS'}",
        ]
        return self.send_message("\n".join(lines))

    def send_threshold_adjustment(self, old_threshold: float, new_threshold: float, reason: str):
        return self.send_message(
            f"🔄 <b>ML Threshold Adjusted</b>\n"
            f"{old_threshold:.0%} → {new_threshold:.0%}\n"
            f"Reason: {reason}"
        )

    def send_daily_summary(self, stats: Dict):
        trades = stats.get('trades', 0)
        wins = stats.get('wins', 0)
        win_rate = (wins / trades * 100) if trades > 0 else 0
        lines = [
            f"📊 <b>Daily Summary</b> ({datetime.now().strftime('%Y-%m-%d')})",
            f"Trades: <b>{trades}</b> ({wins} wins, {trades - wins} losses)",
            f"Win rate: <b>{win_rate:.1f}%</b>",
        ]
        ml_accuracy = stats.get('ml_accuracy', 0)
        if ml_accuracy:
            lines.append(f"ML accuracy: {ml_accuracy:.1%}")
        return self.send_message("\n".join(lines))

    def send_streak_alert(self, win_streak: int = 0, loss_streak: int = 0):
        if win_streak >= 3:
            return self.send_message(
                f"🔥 <b>Winning Streak!</b>\n{win_streak} wins in a row"
            )
        elif loss_streak >= 3:
            return self.send_message(
                f"⚠️ <b>Losing Streak</b>\n{loss_streak} losses in a row\n"
                f"Bot has tightened entry rules"
            )

    # ── Static helpers ────────────────────────────────────────────────────────

    @staticmethod
    def _probability_label(prob: float) -> str:
        if prob >= 0.80:
            return "🎯 Very High"
        if prob >= 0.70:
            return "✅ High"
        if prob >= 0.60:
            return "⚠️ Moderate"
        return "❌ Low"

    @staticmethod
    def create_from_env():
        from dotenv import load_dotenv
        load_dotenv()
        return ProbabilityTelegramNotifier(
            bot_token=os.getenv('TELEGRAM_BOT_TOKEN', ''),
            chat_id=os.getenv('TELEGRAM_CHAT_ID', ''),
            enabled=os.getenv('TELEGRAM_ENABLED', 'true').lower() == 'true',
        )

"""
Telegram alerts for the alt-perp strategy. Wraps the project's existing
TelegramNotifier (src/notifications.py). Implements the alerter interface that
PositionManager calls: trade_opened / trade_closed / circuit_breaker, plus
daily_summary and error. All sends are best-effort and never raise.
"""

import logging
from datetime import datetime

from . import config

logger = logging.getLogger(__name__)


def _tp_short(entry):  # short TP1 / stop reference prices
    return entry * (1 - config.SHORT_TP1_PCT), entry * (1 + config.SHORT_STOP_PCT)


def _tp_long(entry):
    return entry * (1 + config.LONG_TP_PCT), entry * (1 - config.LONG_STOP_PCT)


class AltperpAlerter:
    def __init__(self, notifier=None):
        self.notifier = notifier  # a TelegramNotifier or None

    def _send(self, msg: str):
        if not self.notifier:
            logger.info("[ALERT] %s", msg.replace("\n", " | "))
            return
        try:
            self.notifier.send_message(msg)
        except Exception as e:
            logger.warning("[ALTPERP] telegram send failed: %s", e)

    def trade_opened(self, pos, setup):
        arrow = "🔴 SHORT" if pos.direction == "short" else "🟢 LONG"
        if pos.direction == "short":
            tp1, stop = _tp_short(pos.entry_price)
        else:
            tp1, stop = _tp_long(pos.entry_price)
        funding_pct = pos.funding_at_entry * 100
        oi_pct = pos.oi_change_at_entry * 100
        cvd = "✓" if pos.cvd_confirmed else "✗"
        liq = "✓" if pos.liq_proximity else "✗"
        fired = 2 + setup.tier2_score  # funding + oi + tier2
        label = {0: "MIN", 1: "MED", 2: "MAX"}.get(setup.tier2_score, "MED")
        self._send(
            f"{arrow} OPENED  (PAPER)\n"
            f"Coin: {pos.coin}\n"
            f"Entry: ${pos.entry_price:,.4f}\n"
            f"Size: ${pos.notional_at_entry:,.2f} ({pos.leverage:.2f}x, {pos.size_multiplier:.2f}× risk)\n"
            f"Signals: Funding {funding_pct:+.3f}% ✓ | OI {oi_pct:+.0f}% ✓ | CVD {cvd} | Liq {liq}\n"
            f"Confluence: {label} ({fired}/4)\n"
            f"Stop: ${stop:,.4f} | TP1: ${tp1:,.4f}"
        )

    def trade_closed(self, pos, reason, net, pnl_pct, equity):
        emoji = "✅" if net >= 0 else "❌"
        self._send(
            f"{emoji} CLOSED {pos.coin} {pos.direction.upper()}\n"
            f"Reason: {reason}\n"
            f"Net PnL: ${net:+,.4f} ({pnl_pct:+.2f}%)\n"
            f"Equity: ${equity:,.2f}"
        )

    def circuit_breaker(self, reason, equity, until):
        until_s = until.strftime("%Y-%m-%d %H:%M UTC") if isinstance(until, datetime) else str(until)
        self._send(
            f"⛔️ <b>CIRCUIT BREAKER</b>\n"
            f"Reason: {reason}\n"
            f"Equity: ${equity:,.2f}\n"
            f"Trading halted until {until_s}"
        )

    def daily_summary(self, stats: dict, equity: float):
        self._send(
            f"📊 <b>Alt-Perp Daily (PAPER)</b>\n"
            f"Trades: {stats.get('count', 0)} ({stats.get('wins', 0)}W)\n"
            f"Net PnL today: ${stats.get('net_pnl', 0):+,.4f}\n"
            f"Equity: ${equity:,.2f}"
        )

    def error(self, context: str, exc: Exception):
        self._send(f"⚠️ Alt-Perp error in {context}: {exc}")


def _selftest():
    a = AltperpAlerter(notifier=None)  # logs instead of sending
    from types import SimpleNamespace
    pos = SimpleNamespace(coin="SOLUSDT", direction="short", entry_price=185.40,
                          notional_at_entry=450.0, leverage=2.5, size_multiplier=1.5,
                          funding_at_entry=0.0007, oi_change_at_entry=0.32,
                          cvd_confirmed=True, liq_proximity=False)
    setup = SimpleNamespace(tier2_score=1)
    a.trade_opened(pos, setup)
    a.trade_closed(pos, "TP1", 6.30, 1.40, 1006.30)
    a.circuit_breaker("daily_drawdown_5pct", 945.0, datetime(2026, 5, 27))
    a.daily_summary({"count": 3, "wins": 2, "net_pnl": 12.5}, 1012.5)
    print("telegram selftest OK (messages logged above)")


if __name__ == "__main__":
    _selftest()

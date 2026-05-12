"""
Telegram Notifications — plain-English trade alerts.
"""

import logging
import requests
from typing import Optional
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# ── Helpers ────────────────────────────────────────────────────────────────────

_COIN_NAMES = {
    'BTC/USD': 'Bitcoin',
    'ETH/USD': 'Ethereum',
    'SOL/USD': 'Solana',
}

def _coin(symbol: str) -> str:
    return _COIN_NAMES.get(symbol, symbol.split('/')[0])

def _regime_plain(regime: str) -> str:
    return {
        'TRENDING_UP':   'strong uptrend',
        'TRENDING_DOWN': 'strong downtrend',
        'RANGING':       'sideways / range-bound',
        'VOLATILE':      'choppy and unpredictable',
        'CRASH':         'market in freefall',
        'UNKNOWN':       'unclear direction',
    }.get(regime, regime.lower())

def _conf_label(confidence: float) -> str:
    if confidence >= 93: return 'Very High'
    if confidence >= 80: return 'High'
    if confidence >= 65: return 'Moderate'
    return 'Low'

def _exit_plain(reason: str) -> str:
    return {
        'STOP_LOSS':   'stopped out (hit stop loss)',
        'TAKE_PROFIT': 'hit profit target',
        'SIGNAL':      'signal reversed — exited early',
    }.get(reason, reason)

def _ofi_plain(ofi: float, is_buy: bool) -> Optional[str]:
    """Translate OFI value to plain English. Returns None if neutral."""
    if ofi > 0.35:
        msg = "Heavy buying pressure in the order books"
    elif ofi > 0.20:
        msg = "More buyers than sellers in order books"
    elif ofi < -0.35:
        msg = "Heavy selling pressure in the order books"
    elif ofi < -0.20:
        msg = "More sellers than buyers in order books"
    else:
        return None  # neutral — skip
    return msg

def _rsi_plain(rsi: float, is_buy: bool) -> Optional[str]:
    if is_buy:
        if rsi < 32:   return f"RSI {rsi:.0f} — very oversold, likely to bounce"
        if rsi < 42:   return f"RSI {rsi:.0f} — oversold, good room to run up"
        if rsi > 68:   return f"RSI {rsi:.0f} — overbought warning (risky long)"
    else:
        if rsi > 68:   return f"RSI {rsi:.0f} — very overbought, likely to fall"
        if rsi > 58:   return f"RSI {rsi:.0f} — overbought, good room to fall"
        if rsi < 32:   return f"RSI {rsi:.0f} — oversold warning (risky short)"
    return None

def _adx_plain(adx: float) -> Optional[str]:
    if adx >= 30: return f"Trend is very strong (ADX {adx:.0f})"
    if adx >= 22: return f"Trend is solid (ADX {adx:.0f})"
    return None

def _lead_lag_plain(lead_dir: str, is_buy: bool) -> Optional[str]:
    if lead_dir == 'BUY' and is_buy:
        return "Bitcoin just moved up — altcoins usually follow within minutes"
    if lead_dir == 'SELL' and not is_buy:
        return "Bitcoin just dropped — altcoins usually follow"
    if lead_dir == 'BUY' and not is_buy:
        return "⚠️ Bitcoin moving up but shorting — going against BTC"
    if lead_dir == 'SELL' and is_buy:
        return "⚠️ Bitcoin dropping but buying — going against BTC"
    return None

def _entry_reasons(sig, is_buy: bool) -> list:
    """Build a plain-English list of reasons for entering this trade."""
    reasons = []
    if sig is None:
        return reasons

    ofi_msg = _ofi_plain(sig.ofi or 0.0, is_buy)
    if ofi_msg:
        reasons.append(ofi_msg)

    ll_msg = _lead_lag_plain(sig.lead_lag_dir, is_buy) if sig.lead_lag_dir else None
    if ll_msg:
        reasons.append(ll_msg)

    regime_msg = _regime_plain(sig.regime)
    aligned = (sig.regime == 'TRENDING_UP' and is_buy) or \
              (sig.regime == 'TRENDING_DOWN' and not is_buy)
    reasons.append(f"Market is in a {regime_msg}")

    rsi_msg = _rsi_plain(sig.rsi, is_buy)
    if rsi_msg:
        reasons.append(rsi_msg)

    adx_msg = _adx_plain(sig.adx)
    if adx_msg and aligned:
        reasons.append(adx_msg)

    if sig.funding_rate is not None:
        annual = sig.funding_rate * 3 * 365 * 100
        if is_buy and sig.funding_rate < -0.001:
            reasons.append(f"Shorts are paying longs — bullish funding ({annual:+.0f}% APY)")
        elif not is_buy and sig.funding_rate > 0.001:
            reasons.append(f"Market is over-leveraged long — shorts get paid ({annual:+.0f}% APY)")

    return reasons[:4]  # cap at 4 reasons to keep messages short


@dataclass
class TelegramConfig:
    bot_token: str
    chat_id: str
    enabled: bool = True


class TelegramNotifier:
    """
    Send plain-English trading notifications to Telegram.
    """

    def __init__(self, bot_token: str, chat_id: str, enabled: bool = True):
        self.token    = bot_token
        self.chat_id  = chat_id
        self.enabled  = enabled
        self.base_url = f"https://api.telegram.org/bot{bot_token}"
        if enabled:
            logger.info(f"Telegram notifications enabled for chat {chat_id}")

    def send_message(self, message: str, parse_mode: str = "HTML") -> bool:
        if not self.enabled:
            logger.debug(f"Notification suppressed: {message[:50]}")
            return False
        try:
            response = requests.post(
                f"{self.base_url}/sendMessage",
                json={"chat_id": self.chat_id, "text": message, "parse_mode": parse_mode},
                timeout=10,
            )
            response.raise_for_status()
            return True
        except Exception as e:
            logger.error(f"Failed to send Telegram message: {e}")
            return False

    # ── Trade entry ────────────────────────────────────────────────────────────

    def send_trade_alert(self, action: str, symbol: str, price: float,
                         size: float, pnl: Optional[float] = None,
                         reason: str = "", signal=None):
        """
        Entry notification.  Pass signal=ScientificSignal for rich output.
        """
        is_buy      = action.upper() == "BUY"
        coin        = _coin(symbol)
        direction   = "LONG (buying)" if is_buy else "SHORT (selling)"
        icon        = "🟢" if is_buy else "🔴"
        conf_label  = _conf_label(signal.confidence) if signal else "—"

        lines = [
            f"{icon} <b>{coin} — {direction}</b>",
            f"Entry price:  <b>${price:,.2f}</b>",
            f"Position:     <b>${size:.2f}</b>",
            f"Confidence:   <b>{conf_label}</b>  ({signal.confidence:.0f}%)" if signal else f"<i>{reason}</i>",
        ]

        if signal:
            sl_pct = signal.stop_loss_pct()
            tp_pct = signal.take_profit_pct()
            if is_buy:
                sl_price = price * (1 - sl_pct / 100)
                tp_price = price * (1 + tp_pct / 100)
            else:
                sl_price = price * (1 + sl_pct / 100)
                tp_price = price * (1 - tp_pct / 100)

            lines.append(f"Stop loss:    ${sl_price:,.2f}  ({sl_pct:.1f}% risk)")
            lines.append(f"Target:       ${tp_price:,.2f}  ({tp_pct:.1f}% gain)")

            reasons = _entry_reasons(signal, is_buy)
            if reasons:
                lines.append("")
                lines.append("<b>Why I entered:</b>")
                for r in reasons:
                    lines.append(f"  • {r}")

        return self.send_message("\n".join(lines))

    # ── Signal-only alert (no position opened) ─────────────────────────────────

    def send_signal(self, symbol: str, signal: str, price: float,
                    rsi: float, ema_fast: float, ema_slow: float):
        return self.send_message(
            f"📡 <b>{signal}  {_coin(symbol)}</b>\n"
            f"${price:,.2f}   RSI {rsi:.0f}"
        )

    # ── Post-trade analysis (main exit notification) ───────────────────────────

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

        is_win    = pnl >= 0
        is_buy    = side == 'buy'
        icon      = "✅" if is_win else "❌"
        result    = f"+${pnl:.2f}" if is_win else f"-${abs(pnl):.2f}"
        label     = "WIN" if is_win else "LOSS"
        coin      = _coin(symbol)
        direction = "long" if is_buy else "short"
        held      = f"{holding_minutes:.0f} min" if holding_minutes < 60 else f"{holding_minutes/60:.1f} hrs"
        exit_desc = _exit_plain(exit_reason)

        lines = [
            f"{icon} <b>{label}  {result}  ({pnl_pct:+.1f}%)</b>",
            f"{coin}  {direction}   ${entry_price:,.2f} → ${exit_price:,.2f}",
            f"Held {held}  —  {exit_desc}",
            f"Account now:  <b>${total_equity:,.2f}</b>",
        ]

        # Plain-English explanation
        if is_win and positives:
            lines.append("")
            lines.append("<b>Why it worked:</b>")
            for p in _translate_issues(positives)[:3]:
                lines.append(f"  • {p}")
        elif not is_win and issues:
            lines.append("")
            lines.append("<b>What went wrong:</b>")
            for i in _translate_issues(issues)[:3]:
                lines.append(f"  • {i}")

        # Market snapshot at entry (simplified)
        context = []
        if ofi is not None:
            ofi_msg = _ofi_plain(ofi, is_buy)
            if ofi_msg:
                context.append(ofi_msg)
        if btc_lead:
            ll = _lead_lag_plain(btc_lead, is_buy)
            if ll:
                context.append(ll)
        context.append(f"Market was {_regime_plain(regime)} when I entered")

        if context:
            lines.append("")
            lines.append("<b>Market context at entry:</b>")
            for c in context[:3]:
                lines.append(f"  • {c}")

        # Streak / adaptation notices
        if loss_streak >= 3:
            lines.append(f"\n⚠️ <b>{loss_streak} losses in a row</b> — I've tightened my entry rules until conditions improve")
        elif win_streak >= 3:
            lines.append(f"\n🔥 {win_streak} wins in a row — bot is performing well")

        if adaptations:
            lines.append("")
            lines.append("<i>Bot self-adjusted: " + "; ".join(adaptations[:2]) + "</i>")

        return self.send_message("\n".join(lines))

    # ── Simple win/loss (fallback when no signal available) ───────────────────

    def send_win(self, symbol: str, pnl: float, pnl_pct: float,
                 exit_price: float, total_equity: float, reason: str = ""):
        return self.send_message(
            f"✅ <b>WIN  +${pnl:.2f}  ({pnl_pct:+.1f}%)</b>\n"
            f"{_coin(symbol)}   exited at ${exit_price:,.2f}\n"
            f"Account: <b>${total_equity:,.2f}</b>"
        )

    def send_loss(self, symbol: str, pnl: float, pnl_pct: float,
                  exit_price: float, total_equity: float, reason: str = ""):
        return self.send_message(
            f"❌ <b>LOSS  -${abs(pnl):.2f}  ({pnl_pct:+.1f}%)</b>\n"
            f"{_coin(symbol)}   exited at ${exit_price:,.2f}\n"
            f"Account: <b>${total_equity:,.2f}</b>"
        )

    # ── Hourly status ──────────────────────────────────────────────────────────

    def send_status(self, capital: float, pnl: float, pnl_pct: float,
                    open_positions: int, trades_today: int):
        icon   = "📈" if pnl >= 0 else "📉"
        result = f"+${pnl:.2f}" if pnl >= 0 else f"-${abs(pnl):.2f}"

        lines = [
            f"{icon} <b>Hourly Update</b>",
            f"Account:  <b>${capital:,.2f}</b>",
            f"P&L:      <b>{result}  ({pnl_pct:+.1f}%)</b>",
            f"Trades:   {trades_today}",
        ]
        if open_positions > 0:
            lines.append(f"Open:     {open_positions} position{'s' if open_positions > 1 else ''} active")
        else:
            lines.append("Open:     no open positions (watching markets)")

        return self.send_message("\n".join(lines))

    # ── Error / connection ─────────────────────────────────────────────────────

    def send_error(self, error_message: str):
        return self.send_message(f"⚠️ <b>Bot Error</b>\n{error_message}")

    def test_connection(self) -> bool:
        return self.send_message(
            "✅ <b>Bot connected!</b>\n\n"
            "Telegram notifications are working.\n"
            "You'll get plain-English alerts for every trade."
        )


# ── Issue translation ──────────────────────────────────────────────────────────

def _translate_issues(raw_issues: list) -> list:
    """Convert internal diagnostic strings to plain English."""
    out = []
    for msg in raw_issues:
        msg_l = msg.lower()
        if 'ofi' in msg_l and 'confirmed' in msg_l:
            out.append("Order books backed the trade direction")
        elif 'ofi' in msg_l and ('against' in msg_l or 'warned' in msg_l or 'weak' in msg_l):
            out.append("Order books were not supporting the direction at entry")
        elif 'overbought' in msg_l:
            out.append("Price was already overbought when I entered long — bad timing")
        elif 'oversold' in msg_l and 'risky short' in msg_l:
            out.append("Price was already oversold when I shorted — risky entry")
        elif 'oversold' in msg_l and 'room to run' in msg_l:
            out.append("Plenty of upside room — RSI had space to recover")
        elif 'btc lead confirmed' in msg_l:
            out.append("Bitcoin's move confirmed the direction for this trade")
        elif 'btc lead' in msg_l and 'opposing' in msg_l:
            out.append("Bitcoin was moving the other way — went against BTC momentum")
        elif 'regime' in msg_l and 'aligned' in msg_l:
            out.append("The broader trend matched the trade direction")
        elif 'volatile' in msg_l or 'crash' in msg_l:
            out.append("Market conditions were chaotic — unpredictable")
        elif 'stopped out' in msg_l or 'false breakout' in msg_l:
            out.append("Price immediately reversed after entry — false breakout")
        elif 'target reached' in msg_l:
            out.append("Price hit the exact target as planned")
        elif 'high conviction' in msg_l or 'confidence' in msg_l and '%' in msg_l:
            if 'low' in msg_l:
                out.append("Signal wasn't strong enough — should have waited")
            else:
                out.append("High-conviction entry — strong signal alignment")
        elif 'funding' in msg_l and 'bearish' in msg_l:
            out.append("Market was over-leveraged long — funding rate was warning of a drop")
        elif 'funding' in msg_l and 'bullish' in msg_l:
            out.append("Shorts were paying longs — funding favored this trade")
        else:
            out.append(msg)  # pass through if no translation matched
    return out


# ── Factory ────────────────────────────────────────────────────────────────────

def create_notifier_from_env() -> TelegramNotifier:
    import os
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass  # python-dotenv not installed; env vars from the OS/shell are still readable

    token   = os.getenv("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
    enabled = os.getenv("TELEGRAM_ENABLED", "false").lower() == "true"

    if not token or not chat_id:
        logger.warning("Telegram not configured — notifications disabled")
        return TelegramNotifier("", "", enabled=False)

    return TelegramNotifier(token, chat_id, enabled)


if __name__ == '__main__':
    import os
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass
    token   = os.getenv("TELEGRAM_BOT_TOKEN")
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

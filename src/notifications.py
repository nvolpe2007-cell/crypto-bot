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
        is_buy = action.upper() == "BUY"
        icon   = "🟢" if is_buy else "🔴"
        side   = "BUY" if is_buy else "SELL"
        coin   = _coin(symbol)
        return self.send_message(
            f"{icon} <b>{side} {coin}</b>\n"
            f"Price: <b>${price:,.2f}</b>   Size: <b>${size:.2f}</b>"
        )

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
        is_win = pnl >= 0
        icon   = "✅" if is_win else "❌"
        label  = "WIN" if is_win else "LOSS"
        result = f"+${pnl:.2f}" if is_win else f"-${abs(pnl):.2f}"
        coin   = _coin(symbol)

        msg = (
            f"{icon} <b>{label}  {result}</b>\n"
            f"{coin}   ${entry_price:,.2f} → ${exit_price:,.2f}\n"
            f"Account: <b>${total_equity:,.2f}</b>"
        )

        if loss_streak >= 3:
            msg += f"\n⚠️ {loss_streak} losses in a row"
        elif win_streak >= 3:
            msg += f"\n🔥 {win_streak} wins in a row"

        return self.send_message(msg)

    # ── Simple win/loss (fallback when no signal available) ───────────────────

    def send_win(self, symbol: str, pnl: float, pnl_pct: float,
                 exit_price: float, total_equity: float, reason: str = ""):
        return self.send_message(
            f"✅ <b>WIN  +${pnl:.2f}</b>\n"
            f"{_coin(symbol)}   exited at ${exit_price:,.2f}\n"
            f"Account: <b>${total_equity:,.2f}</b>"
        )

    def send_loss(self, symbol: str, pnl: float, pnl_pct: float,
                  exit_price: float, total_equity: float, reason: str = ""):
        return self.send_message(
            f"❌ <b>LOSS  -${abs(pnl):.2f}</b>\n"
            f"{_coin(symbol)}   exited at ${exit_price:,.2f}\n"
            f"Account: <b>${total_equity:,.2f}</b>"
        )

    # ── Hourly status ──────────────────────────────────────────────────────────

    def send_status(self, capital: float, pnl: float, pnl_pct: float,
                    open_positions: int, trades_today: int):
        icon   = "📈" if pnl >= 0 else "📉"
        result = f"+${pnl:.2f}" if pnl >= 0 else f"-${abs(pnl):.2f}"
        open_str = f"{open_positions} trade open" if open_positions == 1 else \
                   f"{open_positions} trades open" if open_positions > 1 else "no open trades"

        lines = [
            f"{icon} <b>Update</b>",
            f"Account: <b>${capital:,.2f}</b>",
            f"P&L:     <b>{result}</b>",
            f"Trades today: {trades_today}   {open_str}",
        ]

        return self.send_message("\n".join(lines))

    # ── Perp / futures alerts ──────────────────────────────────────────────────

    def send_perp_entry(self, symbol: str, side: str, price: float,
                        size_usd: float, leverage: int = 1,
                        funding_rate: Optional[float] = None,
                        stop_price: Optional[float] = None,
                        target_price: Optional[float] = None) -> bool:
        """Alert when a perp position is opened."""
        coin = _coin(symbol)
        is_long = side.lower() in ('buy', 'long')
        direction = "LONG" if is_long else "SHORT"
        icon = "🟢" if is_long else "🔴"

        lines = [
            f"{icon} <b>PERP {direction} — {coin}</b>",
            f"Entry: <b>${price:,.2f}</b>",
            f"Size: <b>${size_usd:.2f}</b>" + (f" ({leverage}x)" if leverage > 1 else ""),
        ]
        if funding_rate is not None:
            apy = funding_rate * 3 * 365 * 100  # 3 payments/day × 365
            sign = "+" if funding_rate >= 0 else ""
            lines.append(f"Funding: {sign}{funding_rate*100:.4f}% / 8h  ({sign}{apy:.1f}% APY)")
        if stop_price:
            lines.append(f"Stop: ${stop_price:,.2f}")
        if target_price:
            lines.append(f"Target: ${target_price:,.2f}")
        return self.send_message("\n".join(lines))

    def send_perp_exit(self, symbol: str, side: str, entry_price: float,
                       exit_price: float, pnl: float, pnl_pct: float,
                       total_equity: float, exit_reason: str = "",
                       funding_paid: Optional[float] = None) -> bool:
        """Alert when a perp position is closed."""
        coin = _coin(symbol)
        is_win = pnl >= 0
        icon = "✅" if is_win else "❌"
        label = "WIN" if is_win else "LOSS"
        result = f"+${pnl:.2f}" if is_win else f"-${abs(pnl):.2f}"

        lines = [
            f"{icon} <b>PERP {label}  {result} ({pnl_pct:+.1f}%)</b>",
            f"{coin}  ${entry_price:,.2f} → ${exit_price:,.2f}",
        ]
        if exit_reason:
            lines.append(_exit_plain(exit_reason))
        if funding_paid is not None:
            sign = "" if funding_paid >= 0 else "-"
            lines.append(f"Funding paid: {sign}${abs(funding_paid):.4f}")
        lines.append(f"Account: <b>${total_equity:,.2f}</b>")
        return self.send_message("\n".join(lines))

    def send_funding_rate_alert(self, symbol: str, funding_rate: float,
                                side: Optional[str] = None) -> bool:
        """Alert when funding rate becomes extreme (>0.1% per 8h)."""
        coin = _coin(symbol)
        apy = funding_rate * 3 * 365 * 100
        positive = funding_rate >= 0
        icon = "🔴" if positive else "🟢"  # high positive = longs pay shorts (bad for longs)
        lines = [
            f"{icon} <b>Funding Rate Alert — {coin}</b>",
            f"Rate: {funding_rate*100:+.4f}% / 8h  ({apy:+.1f}% APY)",
        ]
        if positive:
            lines.append("Longs are paying shorts — bearish signal")
        else:
            lines.append("Shorts are paying longs — bullish signal")
        if side:
            direction = "long" if side.lower() in ('buy', 'long') else "short"
            cost_sign = "+" if (positive and direction == "short") or (not positive and direction == "long") else "-"
            lines.append(f"Your {direction} position receives funding" if cost_sign == "+" else f"Your {direction} position pays funding")
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


def _translate_issues(raw_issues: list) -> list:
    return raw_issues


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

"""
Telegram Notifications — plain-English trade alerts.
"""

import html
import logging
import os
import requests
from typing import Optional
from dataclasses import dataclass

logger = logging.getLogger(__name__)


def _esc(text) -> str:
    """Escape <, >, & in dynamic text so it can't break Telegram HTML parsing.
    Apply to any data-derived string interpolated into an HTML message (the
    static <b>/<i> tags in the templates are written by us and must NOT be escaped)."""
    return html.escape(str(text), quote=False)


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

    fr = getattr(sig, 'funding_rate', None)
    if fr is not None and abs(fr) > 0.001:
        if fr < 0 and is_buy:
            reasons.append("Funding rate: shorts paying longs — supports this long")
        elif fr > 0 and not is_buy:
            reasons.append("Funding rate: over-leveraged longs paying shorts — supports short")

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
        # Global crypto-Telegram mute: silences EVERY crypto alert (every send_*
        # helper funnels through here) while the bot keeps trading normally. Set
        # CRYPTO_TELEGRAM_MUTE=1 to go quiet without stopping the bot. Read per-call
        # so it's a single, simple kill switch. (stockbot posts via its OWN
        # independent notifier, so it is unaffected by this.)
        if os.getenv("CRYPTO_TELEGRAM_MUTE", "").strip().lower() in ("1", "true", "yes", "on"):
            return False
        if not self.enabled:
            logger.debug(f"Notification suppressed: {message[:50]}")
            return False
        try:
            response = requests.post(
                f"{self.base_url}/sendMessage",
                json={"chat_id": self.chat_id, "text": message, "parse_mode": parse_mode},
                timeout=10,
            )
            # A 400 with parse_mode set is almost always an entity-parse error from
            # an unescaped <, >, or & in dynamic text. Don't lose the alert — log
            # Telegram's actual reason and resend as plain text.
            if response.status_code == 400 and parse_mode:
                try:
                    desc = response.json().get("description", response.text[:200])
                except Exception:
                    desc = response.text[:200]
                logger.warning(f"Telegram 400 (parse_mode={parse_mode}): {desc} — retrying as plain text")
                response = requests.post(
                    f"{self.base_url}/sendMessage",
                    json={"chat_id": self.chat_id, "text": message},
                    timeout=10,
                )
            response.raise_for_status()
            return True
        except Exception as e:
            logger.error(f"Failed to send Telegram message: {e}")
            return False

    # ── Pre-trade reasoning (probability gate output) ──────────────────────────

    def send_trade_reasoning(self, symbol: str, side: str, price: float,
                              reasoning, size: float, entry_path: str = "main"):
        """
        Pre-trade Telegram message showing the full probability stack.
        `reasoning` is a probability_gate.TradeReasoning.
        """
        is_buy = side.upper() == "LONG"
        icon   = "🟢" if is_buy else "🔴"
        coin   = _coin(symbol)

        if reasoning.rejected:
            header = f"🚫 <b>SKIPPED: {side} {coin}</b>"
        else:
            header = f"{icon} <b>EVALUATING: {side} {coin}</b>"

        lines = [
            header,
            f"Price <b>${price:,.2f}</b>  ·  Path <i>{entry_path}</i>",
            "",
            f"<b>P(win) = {reasoning.combined_p:.0%}</b>"
            f"   (Kelly f*={reasoning.kelly_fraction:.2f}, qtr={reasoning.quarter_kelly:.3f})",
            f"Size scale: <b>{reasoning.size_scale:.2f}x</b>"
            f" → ${size:.2f}",
            "",
            "<b>Edges stacked:</b>",
        ]

        for edge in reasoning.edges:
            mark = "✓" if edge.present else "·"
            p_str = f"p={edge.p_win:.2f}" if edge.present else "—"
            lines.append(f"  {mark} <b>{_esc(edge.name)}</b> {p_str}  {_esc(edge.note)}")

        if reasoning.rejected and reasoning.rejection_reason:
            lines += ["", f"<b>Reject:</b> {_esc(reasoning.rejection_reason)}"]

        return self.send_message("\n".join(lines))

    # ── Trade entry ────────────────────────────────────────────────────────────

    def send_trade_alert(self, action: str, symbol: str, price: float,
                         size: float, pnl: Optional[float] = None,
                         reason: str = "", signal=None, entry_path: str = "main"):
        is_buy = action.upper() == "BUY"
        icon   = "🟢" if is_buy else "🔴"
        side   = "LONG" if is_buy else "SHORT"
        coin   = _coin(symbol)

        # Short label for which strategy pathway fired this trade
        _PATH_LABELS = {
            'main':       '⚙️ main',
            'mr':         '🔄 MR',
            'mr-extreme': '🔄 MR-extreme',
            'fast-track': '⚡ fast-track',
        }
        path_label = _PATH_LABELS.get(entry_path, entry_path)

        lines = [
            f"{icon} <b>TAKING TRADE: {side} {coin}</b>  <i>[{path_label}]</i>",
            f"Price: <b>${price:,.2f}</b>   Size: <b>${size:.2f}</b>",
        ]

        if signal:
            conf = getattr(signal, 'confidence', None)
            if conf is not None:
                lines.append(f"Confidence: <b>{conf:.0f}%</b> ({_conf_label(conf)})")
            if hasattr(signal, 'stop_loss_pct') and callable(signal.stop_loss_pct):
                sl = signal.stop_loss_pct() / 100
                tp = signal.take_profit_pct() / 100
                sl_price = price * (1 - sl) if is_buy else price * (1 + sl)
                tp_price = price * (1 + tp) if is_buy else price * (1 - tp)
                lines.append(f"Stop: ${sl_price:,.2f}   Target: ${tp_price:,.2f}")
            reasons = _entry_reasons(signal, is_buy)
            if reasons:
                lines.append("\n<b>Why:</b>")
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
                             adaptations: Optional[list] = None,
                             entry_path: str = "main",
                             mfe_pct: float = 0.0,
                             mae_pct: float = 0.0) -> bool:
        is_win = pnl >= 0
        icon   = "✅" if is_win else "❌"
        label  = "WIN" if is_win else "LOSS"
        result = f"+${pnl:.2f}" if is_win else f"-${abs(pnl):.2f}"
        coin   = _coin(symbol)
        held   = f"{holding_minutes:.0f} min" if holding_minutes < 60 else f"{holding_minutes/60:.1f}h"

        _PATH_LABELS = {
            'main':       'main',
            'mr':         'MR',
            'mr-extreme': 'MR-extreme',
            'fast-track': 'fast-track',
        }

        lines = [
            f"{icon} <b>{label}  {result}</b>  <i>[{_PATH_LABELS.get(entry_path, entry_path)}]</i>",
            f"{coin}   ${entry_price:,.2f} → ${exit_price:,.2f}  ({pnl_pct:+.2f}%)",
            f"{_exit_plain(exit_reason)}   held {held}",
            f"Account: <b>${total_equity:,.2f}</b>",
        ]

        # Excursion: how much we left on the table / how close we came to stopping
        if mfe_pct or mae_pct:
            lines.append(f"Best: <b>{mfe_pct:+.2f}%</b>   Worst: <b>{mae_pct:+.2f}%</b>")

        if loss_streak >= 3:
            lines.append(f"\n⚠️ {loss_streak} losses in a row")
        elif win_streak >= 3:
            lines.append(f"\n🔥 {win_streak} wins in a row")

        # What went wrong / what worked
        if not is_win and issues:
            lines.append("\n<b>What went wrong:</b>")
            for issue in issues[:4]:
                lines.append(f"  • {issue}")
        elif is_win and positives:
            lines.append("\n<b>What worked:</b>")
            for p in positives[:3]:
                lines.append(f"  • {p}")

        # Learner adaptations
        if adaptations:
            lines.append("\n<b>Learner adapts:</b>")
            for a in adaptations:
                lines.append(f"  • {a}")

        return self.send_message("\n".join(lines))

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
                   f"{open_positions} trades open" if open_positions > 1 else "no open positions"

        lines = [
            f"{icon} <b>Update</b>",
            f"Account: <b>${capital:,.2f}</b>",
            f"P&L:     <b>{result}</b>",
            f"Trades today: {trades_today}   {open_str}",
        ]

        return self.send_message("\n".join(lines))

    # ── Daily summary ──────────────────────────────────────────────────────────

    def send_daily_summary(self, total_equity: float, start_equity: float,
                           trades: int, wins: int, losses: int,
                           best_trade: float, worst_trade: float,
                           path_stats: Optional[dict] = None,
                           regime_stats: Optional[dict] = None,
                           open_positions: int = 0) -> bool:
        pnl     = total_equity - start_equity
        pnl_pct = (pnl / start_equity * 100) if start_equity else 0
        wr      = (wins / trades * 100) if trades else 0
        icon    = "📈" if pnl >= 0 else "📉"
        result  = f"+${pnl:.2f}" if pnl >= 0 else f"-${abs(pnl):.2f}"
        from datetime import datetime, timezone
        lines = [
            f"{icon} <b>Daily Summary — {datetime.now(timezone.utc).strftime('%Y-%m-%d')}</b>",
            f"P&L: <b>{result} ({pnl_pct:+.1f}%)</b>",
            f"Account: <b>${total_equity:,.2f}</b>",
            f"Trades: {trades}  ({wins}W / {losses}L)",
        ]
        if trades:
            lines.append(f"Win rate: <b>{wr:.0f}%</b>")
        if best_trade != 0 or worst_trade != 0:
            lines.append(f"Best: +${best_trade:.2f}   Worst: -${abs(worst_trade):.2f}")
        if open_positions:
            lines.append(f"Open positions: {open_positions}")

        # By entry path — which strategy paid off today
        if path_stats:
            lines.append("\n<b>By entry path:</b>")
            for path, v in sorted(path_stats.items(), key=lambda kv: -kv[1].get('n', 0)):
                if v.get('n', 0) > 0:
                    lines.append(f"  {path:<11} {v['n']}× WR={v['win_rate']:.0f}%  ${v['total_pnl']:+.2f}")

        # By regime
        if regime_stats:
            lines.append("\n<b>By regime:</b>")
            for regime, v in sorted(regime_stats.items(), key=lambda kv: -kv[1].get('n', 0)):
                if v.get('n', 0) > 0:
                    lines.append(f"  {regime:<14} {v['n']}× WR={v['win_rate']:.0f}%  ${v['total_pnl']:+.2f}")

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

    # ── Error / connection ─────────────────────────────────────────────────────

    def send_error(self, error_message: str):
        return self.send_message(f"⚠️ <b>Bot Error</b>\n{error_message}")

    async def send(self, msg: str) -> None:
        """Async wrapper around send_message(), required by TaskSupervisor.

        TaskSupervisor calls ``await notifier.send(msg)`` when a supervised
        task crashes or gives up.  Without this method, _safe_notify() raises
        AttributeError on every crash, which is silently swallowed at DEBUG
        level, meaning crash alerts never reach Telegram.
        """
        self.send_message(msg)

    def test_connection(self) -> bool:
        return self.send_message(
            "✅ <b>Bot connected!</b>\n\n"
            "Telegram notifications are working.\n"
            "You'll get plain-English alerts for every trade."
        )


def _translate_issues(raw_issues: list) -> list:
    """Convert technical diagnostic strings to plain English for Telegram."""
    result = []
    for msg in raw_issues:
        lower = msg.lower()

        # OFI patterns
        if lower.startswith('ofi') and 'confirmed direction' in lower:
            result.append("Order book flow confirmed the trade direction")
        elif lower.startswith('ofi') and 'was against direction' in lower:
            result.append("Order book was not supporting this trade direction")
        elif lower.startswith('ofi') and 'was weak' in lower:
            result.append("Order book showed no clear conviction — not supporting either direction")

        # RSI patterns
        elif 'rsi' in lower and 'overbought' in lower:
            result.append("RSI was in overbought territory at entry — risky long")
        elif 'rsi' in lower and 'oversold' in lower and 'short' in lower:
            result.append("RSI was in oversold territory at entry — risky short")
        elif 'rsi' in lower and ('had room to run' in lower or 'confirmed bearish' in lower):
            result.append(msg)

        # BTC lead-lag patterns
        elif lower.startswith('btc lead confirmed'):
            result.append("Bitcoin moved in the same direction first — altcoins typically follow")
        elif lower.startswith('btc lead was') and 'opposing' in lower:
            result.append("Bitcoin moved against this trade direction — higher reversal risk")

        # Regime patterns
        elif 'regime' in lower and 'aligned with trade direction' in lower:
            result.append("Market trend was aligned with the trade direction")
        elif 'regime' in lower and 'unpredictable conditions' in lower:
            result.append("Market conditions were chaotic and unpredictable at entry")

        # Confidence patterns
        elif 'high conviction entry' in lower:
            result.append("High conviction entry with strong signal quality")
        elif 'low confidence entry' in lower:
            result.append("Signal wasn't strong enough — should have waited for a cleaner setup")

        # Exit patterns
        elif 'stopped out' in lower and 'rejection' in lower:
            result.append("Price reversed immediately at entry — possible false breakout")
        elif 'target reached' in lower:
            result.append("Target reached as planned — trade worked as expected")

        # Holding time / other breakout note
        elif 'false breakout' in lower:
            result.append(msg)

        # Funding patterns
        elif lower.startswith('funding') and 'over-leveraged' in lower:
            result.append(f"Market is over-leveraged long — {msg}")
        elif lower.startswith('funding') and 'shorts paying' in lower:
            result.append(f"Funding rate bullish — {msg}")

        # Default: pass through unchanged
        else:
            result.append(msg)

    return result


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

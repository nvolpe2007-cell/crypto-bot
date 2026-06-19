"""Unit tests for src/notifications.py

Covers all pure helper functions and the TelegramNotifier class:

Helper functions (no I/O, fully testable):
- _coin: symbol → coin name lookup
- _regime_plain: regime label → plain-English string
- _conf_label: confidence score → tier label
- _exit_plain: exit reason code → description
- _ofi_plain: OFI value → plain-English (or None if neutral)
- _rsi_plain: RSI value → plain-English (or None if neutral)
- _adx_plain: ADX value → plain-English (or None)
- _lead_lag_plain: lead-lag direction + side → plain-English
- _translate_issues: list of internal strings → plain-English list
- _entry_reasons: signal object → list of reasons (max 4)

TelegramNotifier:
- send_message: disabled suppresses HTTP; HTTP error returns False
- send_trade_alert, send_win, send_loss, send_status, send_error
- test_connection delegates to send_message

create_notifier_from_env:
- missing tokens → disabled notifier
- tokens present + TELEGRAM_ENABLED=true → enabled
"""

import os
import threading
import time
import pytest
from unittest.mock import patch, MagicMock
from types import SimpleNamespace

from src.notifications import (
    TelegramNotifier,
    _coin,
    _regime_plain,
    _conf_label,
    _exit_plain,
    _ofi_plain,
    _rsi_plain,
    _adx_plain,
    _lead_lag_plain,
    _translate_issues,
    _entry_reasons,
    create_notifier_from_env,
)


# ── Fixtures / helpers ────────────────────────────────────────────────────────

def _make_signal(
    ofi: float = 0.0,
    lead_lag_dir: str = None,
    regime: str = "TRENDING_UP",
    rsi: float = 50.0,
    adx: float = 25.0,
    funding_rate: float = None,
    confidence: float = 80.0,
    atr: float = 100.0,
    close: float = 50_000.0,
) -> SimpleNamespace:
    """Return a minimal signal-like object accepted by _entry_reasons."""
    return SimpleNamespace(
        ofi=ofi,
        lead_lag_dir=lead_lag_dir,
        regime=regime,
        rsi=rsi,
        adx=adx,
        funding_rate=funding_rate,
        confidence=confidence,
        atr=atr,
        close=close,
        # Methods used by send_trade_alert
        stop_loss_pct=lambda: 1.5,
        take_profit_pct=lambda: 3.0,
    )


def _disabled() -> TelegramNotifier:
    return TelegramNotifier("tok", "chat", enabled=False)


def _enabled() -> TelegramNotifier:
    return TelegramNotifier("tok", "chat", enabled=True)


# ── _coin ─────────────────────────────────────────────────────────────────────

class TestCoin:
    def test_btc(self):
        assert _coin("BTC/USD") == "Bitcoin"

    def test_eth(self):
        assert _coin("ETH/USD") == "Ethereum"

    def test_sol(self):
        assert _coin("SOL/USD") == "Solana"

    def test_unknown_returns_prefix(self):
        assert _coin("DOGE/USD") == "DOGE"

    def test_unknown_no_slash(self):
        # Symbols without a slash fall back to the full string
        result = _coin("BTCUSD")
        assert "BTCUSD" in result


# ── _regime_plain ─────────────────────────────────────────────────────────────

class TestRegimePlain:
    def test_trending_up(self):
        assert _regime_plain("TRENDING_UP") == "strong uptrend"

    def test_trending_down(self):
        assert _regime_plain("TRENDING_DOWN") == "strong downtrend"

    def test_ranging(self):
        assert _regime_plain("RANGING") == "sideways / range-bound"

    def test_volatile(self):
        assert _regime_plain("VOLATILE") == "choppy and unpredictable"

    def test_crash(self):
        assert _regime_plain("CRASH") == "market in freefall"

    def test_unknown(self):
        assert _regime_plain("UNKNOWN") == "unclear direction"

    def test_unrecognised_returns_lowercase(self):
        assert _regime_plain("WEIRD_REGIME") == "weird_regime"


# ── _conf_label ───────────────────────────────────────────────────────────────

class TestConfLabel:
    def test_very_high(self):
        assert _conf_label(97) == "Very High"
        assert _conf_label(93) == "Very High"

    def test_high(self):
        assert _conf_label(85) == "High"
        assert _conf_label(80) == "High"

    def test_moderate(self):
        assert _conf_label(70) == "Moderate"
        assert _conf_label(65) == "Moderate"

    def test_low(self):
        assert _conf_label(64) == "Low"
        assert _conf_label(0) == "Low"

    def test_boundary_92_is_high(self):
        assert _conf_label(92) == "High"

    def test_boundary_79_is_moderate(self):
        assert _conf_label(79) == "Moderate"


# ── _exit_plain ───────────────────────────────────────────────────────────────

class TestExitPlain:
    def test_stop_loss(self):
        assert "stop" in _exit_plain("STOP_LOSS").lower()

    def test_take_profit(self):
        assert "profit" in _exit_plain("TAKE_PROFIT").lower() or "target" in _exit_plain("TAKE_PROFIT").lower()

    def test_signal(self):
        assert "signal" in _exit_plain("SIGNAL").lower() or "reversed" in _exit_plain("SIGNAL").lower()

    def test_unknown_passthrough(self):
        assert _exit_plain("CUSTOM_REASON") == "CUSTOM_REASON"


# ── _ofi_plain ────────────────────────────────────────────────────────────────

class TestOfiPlain:
    def test_heavy_buying(self):
        msg = _ofi_plain(0.40, is_buy=True)
        assert msg is not None
        assert "buy" in msg.lower() or "buying" in msg.lower()

    def test_more_buyers(self):
        msg = _ofi_plain(0.25, is_buy=True)
        assert msg is not None
        assert "buyer" in msg.lower() or "buy" in msg.lower()

    def test_heavy_selling(self):
        msg = _ofi_plain(-0.40, is_buy=False)
        assert msg is not None
        assert "sell" in msg.lower() or "selling" in msg.lower()

    def test_more_sellers(self):
        msg = _ofi_plain(-0.25, is_buy=False)
        assert msg is not None
        assert "seller" in msg.lower() or "sell" in msg.lower()

    def test_neutral_positive_returns_none(self):
        assert _ofi_plain(0.10, is_buy=True) is None

    def test_neutral_negative_returns_none(self):
        assert _ofi_plain(-0.10, is_buy=False) is None

    def test_zero_returns_none(self):
        assert _ofi_plain(0.0, is_buy=True) is None

    def test_boundary_exactly_0_35_is_heavy(self):
        msg = _ofi_plain(0.36, is_buy=True)
        assert msg is not None

    def test_boundary_exactly_0_20_is_moderate(self):
        msg = _ofi_plain(0.21, is_buy=True)
        assert msg is not None


# ── _rsi_plain ────────────────────────────────────────────────────────────────

class TestRsiPlain:
    # Long side
    def test_buy_very_oversold(self):
        msg = _rsi_plain(28, is_buy=True)
        assert msg is not None
        assert "oversold" in msg.lower()

    def test_buy_oversold(self):
        msg = _rsi_plain(38, is_buy=True)
        assert msg is not None
        assert "oversold" in msg.lower()

    def test_buy_overbought_warning(self):
        msg = _rsi_plain(72, is_buy=True)
        assert msg is not None
        assert "overbought" in msg.lower()

    def test_buy_neutral_returns_none(self):
        assert _rsi_plain(55, is_buy=True) is None

    # Short side
    def test_sell_very_overbought(self):
        msg = _rsi_plain(72, is_buy=False)
        assert msg is not None
        assert "overbought" in msg.lower()

    def test_sell_overbought(self):
        msg = _rsi_plain(62, is_buy=False)
        assert msg is not None

    def test_sell_oversold_warning(self):
        msg = _rsi_plain(28, is_buy=False)
        assert msg is not None
        assert "oversold" in msg.lower()

    def test_sell_neutral_returns_none(self):
        assert _rsi_plain(50, is_buy=False) is None


# ── _adx_plain ────────────────────────────────────────────────────────────────

class TestAdxPlain:
    def test_very_strong(self):
        msg = _adx_plain(35)
        assert msg is not None
        assert "very strong" in msg.lower() or "strong" in msg.lower()

    def test_solid(self):
        msg = _adx_plain(25)
        assert msg is not None
        assert "solid" in msg.lower() or "strong" in msg.lower()

    def test_weak_returns_none(self):
        assert _adx_plain(18) is None

    def test_exactly_22_is_solid(self):
        msg = _adx_plain(22)
        assert msg is not None

    def test_exactly_30_is_very_strong(self):
        msg = _adx_plain(30)
        assert msg is not None
        assert "very strong" in msg.lower()

    def test_21_returns_none(self):
        assert _adx_plain(21) is None


# ── _lead_lag_plain ───────────────────────────────────────────────────────────

class TestLeadLagPlain:
    def test_btc_up_confirms_buy(self):
        msg = _lead_lag_plain("BUY", is_buy=True)
        assert msg is not None
        assert "bitcoin" in msg.lower() or "btc" in msg.lower()
        assert "up" in msg.lower() or "moved" in msg.lower()

    def test_btc_down_confirms_sell(self):
        msg = _lead_lag_plain("SELL", is_buy=False)
        assert msg is not None
        assert "bitcoin" in msg.lower() or "btc" in msg.lower()

    def test_btc_up_warns_short(self):
        msg = _lead_lag_plain("BUY", is_buy=False)
        assert msg is not None
        # Should warn about going against BTC
        assert "btc" in msg.lower() or "bitcoin" in msg.lower()

    def test_btc_down_warns_long(self):
        msg = _lead_lag_plain("SELL", is_buy=True)
        assert msg is not None
        assert "bitcoin" in msg.lower() or "btc" in msg.lower()

    def test_none_direction_returns_none(self):
        assert _lead_lag_plain(None, is_buy=True) is None


# ── _translate_issues ─────────────────────────────────────────────────────────

class TestTranslateIssues:
    def test_empty_list_returns_empty(self):
        assert _translate_issues([]) == []

    def test_ofi_confirmed(self):
        out = _translate_issues(["OFI +0.30 confirmed direction at entry"])
        assert len(out) == 1
        assert "order book" in out[0].lower()

    def test_ofi_against(self):
        out = _translate_issues(["OFI -0.25 was against direction — order flow warned us"])
        assert len(out) == 1
        assert "order book" in out[0].lower() or "not supporting" in out[0].lower()

    def test_ofi_weak(self):
        out = _translate_issues(["OFI 0.05 was weak (no clear conviction)"])
        assert len(out) == 1
        assert "order book" in out[0].lower() or "not supporting" in out[0].lower()

    def test_overbought(self):
        out = _translate_issues(["RSI 72 was overbought at entry"])
        assert len(out) == 1
        assert "overbought" in out[0].lower()

    def test_oversold_risky_short(self):
        out = _translate_issues(["RSI 28 was oversold — risky short"])
        assert len(out) == 1
        assert "oversold" in out[0].lower()

    def test_btc_lead_confirmed(self):
        out = _translate_issues(["BTC lead confirmed BUY"])
        assert len(out) == 1
        assert "bitcoin" in out[0].lower() or "direction" in out[0].lower()

    def test_btc_lead_opposing(self):
        out = _translate_issues(["BTC lead was SELL — opposing this trade"])
        assert len(out) == 1
        assert "bitcoin" in out[0].lower() or "against" in out[0].lower()

    def test_regime_aligned(self):
        out = _translate_issues(["Regime TRENDING_UP aligned with trade direction"])
        assert len(out) == 1
        assert "trend" in out[0].lower() or "direction" in out[0].lower()

    def test_volatile_regime(self):
        out = _translate_issues(["Regime VOLATILE — unpredictable conditions"])
        assert len(out) == 1
        assert "chaotic" in out[0].lower() or "unpredictable" in out[0].lower()

    def test_crash_regime(self):
        out = _translate_issues(["Regime CRASH — unpredictable conditions"])
        assert len(out) == 1
        assert "chaotic" in out[0].lower() or "unpredictable" in out[0].lower()

    def test_stopped_out(self):
        out = _translate_issues(["Stopped out — immediate rejection at entry level"])
        assert len(out) == 1
        assert "reversal" in out[0].lower() or "false" in out[0].lower() or "breakout" in out[0].lower()

    def test_false_breakout(self):
        out = _translate_issues(["Held only 1min — false breakout"])
        assert len(out) == 1

    def test_target_reached(self):
        out = _translate_issues(["Target reached as predicted"])
        assert len(out) == 1
        assert "target" in out[0].lower() or "planned" in out[0].lower()

    def test_high_conviction(self):
        out = _translate_issues(["High conviction entry (92% confidence)"])
        assert len(out) == 1
        assert "high" in out[0].lower() or "conviction" in out[0].lower() or "strong" in out[0].lower()

    def test_low_confidence(self):
        out = _translate_issues(["Low confidence entry (55%) — should have skipped"])
        assert len(out) == 1
        assert "strong" in out[0].lower() or "waited" in out[0].lower() or "signal" in out[0].lower()

    def test_funding_bearish(self):
        out = _translate_issues(["Funding 200% APY — market over-leveraged long (bearish pressure)"])
        assert len(out) == 1
        assert "leveraged" in out[0].lower() or "funding" in out[0].lower()

    def test_funding_bullish(self):
        out = _translate_issues(["Funding -100% APY — shorts paying longs (bullish pressure)"])
        assert len(out) == 1
        assert "funding" in out[0].lower() or "short" in out[0].lower()

    def test_unknown_passes_through(self):
        msg = "Some completely custom message"
        out = _translate_issues([msg])
        assert out == [msg]

    def test_multiple_issues(self):
        issues = [
            "OFI +0.30 confirmed direction at entry",
            "Target reached as predicted",
        ]
        out = _translate_issues(issues)
        assert len(out) == 2

    def test_preserves_list_length(self):
        issues = ["a totally unknown issue"] * 5
        out = _translate_issues(issues)
        assert len(out) == 5


# ── _entry_reasons ────────────────────────────────────────────────────────────

class TestEntryReasons:
    def test_always_includes_regime(self):
        sig = _make_signal(regime="TRENDING_UP")
        reasons = _entry_reasons(sig, is_buy=True)
        assert any("trend" in r.lower() or "sideways" in r.lower() or "freefall" in r.lower()
                   or "choppy" in r.lower() or "unclear" in r.lower() for r in reasons)

    def test_max_four_reasons(self):
        sig = _make_signal(
            ofi=0.40,
            lead_lag_dir="BUY",
            rsi=28,
            adx=35,
            funding_rate=-0.002,
        )
        reasons = _entry_reasons(sig, is_buy=True)
        assert len(reasons) <= 4

    def test_no_reasons_when_neutral_ofi(self):
        sig = _make_signal(ofi=0.05, lead_lag_dir=None, rsi=50, adx=18, funding_rate=None)
        reasons = _entry_reasons(sig, is_buy=True)
        # Only regime should be present (OFI neutral, no lead-lag, neutral RSI, weak ADX)
        assert len(reasons) == 1

    def test_returns_empty_when_signal_is_none(self):
        reasons = _entry_reasons(None, is_buy=True)
        assert reasons == []

    def test_strong_ofi_included_as_buy(self):
        sig = _make_signal(ofi=0.40)
        reasons = _entry_reasons(sig, is_buy=True)
        assert any("buy" in r.lower() or "buying" in r.lower() for r in reasons)

    def test_strong_ofi_included_as_sell(self):
        sig = _make_signal(ofi=-0.40)
        reasons = _entry_reasons(sig, is_buy=False)
        assert any("sell" in r.lower() or "selling" in r.lower() for r in reasons)

    def test_lead_lag_included_when_present(self):
        sig = _make_signal(lead_lag_dir="BUY")
        reasons = _entry_reasons(sig, is_buy=True)
        assert any("bitcoin" in r.lower() or "btc" in r.lower() for r in reasons)

    def test_funding_bullish_included(self):
        sig = _make_signal(funding_rate=-0.002)
        reasons = _entry_reasons(sig, is_buy=True)
        assert any("funding" in r.lower() or "short" in r.lower() for r in reasons)

    def test_funding_bearish_included_for_short(self):
        sig = _make_signal(funding_rate=0.002)
        reasons = _entry_reasons(sig, is_buy=False)
        assert any("funding" in r.lower() or "leveraged" in r.lower() or "paid" in r.lower() for r in reasons)

    def test_negligible_funding_not_included(self):
        sig = _make_signal(funding_rate=0.0005, lead_lag_dir=None, ofi=0.05, rsi=50)
        reasons = _entry_reasons(sig, is_buy=True)
        assert not any("funding" in r.lower() for r in reasons)


# ── TelegramNotifier.send (async wrapper used by TaskSupervisor) ──────────────

class TestSendAsync:
    """TelegramNotifier.send() is the async interface consumed by TaskSupervisor.

    Without it, _safe_notify() raises AttributeError on every task crash,
    which is silently swallowed, and crash alerts never reach Telegram.
    """

    async def test_send_is_awaitable(self):
        """send() must be an awaitable coroutine so TaskSupervisor can await it."""
        notifier = _disabled()
        import inspect
        assert inspect.iscoroutinefunction(notifier.send), (
            "TelegramNotifier.send must be an async method for TaskSupervisor compatibility"
        )

    async def test_send_delegates_to_send_message(self):
        """send() should call send_message() with the same message."""
        notifier = _enabled()
        sent: list[str] = []

        def _capture(url, json=None, timeout=None):
            sent.append(json["text"])
            r = MagicMock()
            r.raise_for_status = MagicMock()
            return r

        with patch("requests.post", side_effect=_capture):
            await notifier.send("supervisor alert")

        assert sent == ["supervisor alert"]

    async def test_send_disabled_does_not_make_http_request(self):
        """Disabled notifier's send() should not make HTTP calls."""
        notifier = _disabled()
        with patch("requests.post") as mock_post:
            await notifier.send("no-op")
        mock_post.assert_not_called()

    async def test_send_http_error_does_not_raise(self):
        """send() must not propagate HTTP errors (TaskSupervisor wraps it in try/except
        but we should be defensive here too via send_message's own error handling)."""
        notifier = _enabled()
        with patch("requests.post", side_effect=Exception("network down")):
            # Should not raise — send_message swallows it, so send() does too.
            await notifier.send("crash notification")


# ── TelegramNotifier.send_message ─────────────────────────────────────────────

class TestSendMessage:
    def test_disabled_returns_false_no_http(self):
        notifier = _disabled()
        with patch("requests.post") as mock_post:
            result = notifier.send_message("hello")
        assert result is False
        mock_post.assert_not_called()

    def test_enabled_success_returns_true(self):
        notifier = _enabled()
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        with patch("requests.post", return_value=mock_resp) as mock_post:
            result = notifier.send_message("hello")
        assert result is True
        mock_post.assert_called_once()

    def test_enabled_http_exception_returns_false(self):
        notifier = _enabled()
        with patch("requests.post", side_effect=Exception("connection refused")):
            result = notifier.send_message("hello")
        assert result is False

    def test_raise_for_status_called(self):
        notifier = _enabled()
        mock_resp = MagicMock()
        with patch("requests.post", return_value=mock_resp):
            notifier.send_message("test")
        mock_resp.raise_for_status.assert_called_once()

    def test_message_sent_in_json_body(self):
        notifier = _enabled()
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        with patch("requests.post", return_value=mock_resp) as mock_post:
            notifier.send_message("my message")
        _, kwargs = mock_post.call_args
        assert kwargs["json"]["text"] == "my message"
        assert kwargs["json"]["chat_id"] == "chat"

    def test_raise_for_status_error_returns_false(self):
        notifier = _enabled()
        mock_resp = MagicMock()
        mock_resp.raise_for_status.side_effect = Exception("403 Forbidden")
        with patch("requests.post", return_value=mock_resp):
            result = notifier.send_message("test")
        assert result is False


# ── TelegramNotifier.send_message (async_safe=True) ───────────────────────────
# The live bot drives its whole trading loop from one asyncio event loop.
# async_safe=True moves the HTTP call onto a background thread so a slow
# Telegram round-trip can't stall price polling / exit checks / other arms.

class TestSendMessageAsyncSafe:
    def _make(self, enabled=True) -> TelegramNotifier:
        return TelegramNotifier("tok", "chat", enabled=enabled, async_safe=True)

    def test_default_constructor_is_still_blocking(self):
        """Sanity check: existing callers that don't pass async_safe see no change."""
        notifier = TelegramNotifier("tok", "chat", enabled=True)
        assert notifier._async_safe is False

    def test_returns_true_immediately_without_blocking_on_slow_http(self):
        notifier = self._make()
        release = threading.Event()

        def slow_post(*args, **kwargs):
            release.wait(timeout=2)
            r = MagicMock()
            r.raise_for_status = MagicMock()
            return r

        with patch("requests.post", side_effect=slow_post):
            start = time.monotonic()
            result = notifier.send_message("hello")
            elapsed = time.monotonic() - start
        release.set()  # let the background thread finish before the patch exits
        time.sleep(0.05)
        assert result is True
        assert elapsed < 0.5, "send_message blocked on the HTTP call instead of queueing it"

    def test_message_is_eventually_delivered_in_order(self):
        notifier = self._make()
        sent = []

        def capture(url, json=None, timeout=None):
            sent.append(json["text"])
            r = MagicMock()
            r.raise_for_status = MagicMock()
            return r

        with patch("requests.post", side_effect=capture):
            notifier.send_message("first")
            notifier.send_message("second")
            for _ in range(50):
                if len(sent) >= 2:
                    break
                time.sleep(0.02)
        assert sent == ["first", "second"]

    def test_disabled_does_not_queue_or_call_http(self):
        notifier = self._make(enabled=False)
        with patch("requests.post") as mock_post:
            result = notifier.send_message("no-op")
            time.sleep(0.05)
        assert result is False
        mock_post.assert_not_called()

    def test_http_failure_in_background_thread_does_not_raise(self):
        notifier = self._make()
        with patch("requests.post", side_effect=Exception("network down")):
            result = notifier.send_message("hello")  # must not raise
            time.sleep(0.05)
        assert result is True  # queued successfully; delivery failure is logged, not surfaced


# ── send_trade_alert ──────────────────────────────────────────────────────────

class TestSendTradeAlert:
    def _call(self, action="BUY", symbol="BTC/USD", price=50_000.0,
              size=100.0, signal=None, enabled=True):
        notifier = TelegramNotifier("tok", "chat", enabled=enabled)
        messages = []
        def capture(url, json=None, timeout=None):
            messages.append(json["text"])
            r = MagicMock()
            r.raise_for_status = MagicMock()
            return r
        with patch("requests.post", side_effect=capture):
            notifier.send_trade_alert(action, symbol, price, size, signal=signal)
        return messages[0] if messages else None

    def test_buy_contains_price(self):
        msg = self._call(action="BUY", price=50_000.0)
        assert "50,000" in msg or "50000" in msg

    def test_buy_contains_long_label(self):
        msg = self._call(action="BUY")
        assert "long" in msg.lower() or "buying" in msg.lower()

    def test_sell_contains_short_label(self):
        msg = self._call(action="SELL")
        assert "short" in msg.lower() or "selling" in msg.lower()

    def test_signal_adds_stop_and_target(self):
        sig = _make_signal(confidence=80.0)
        msg = self._call(action="BUY", signal=sig)
        assert "stop" in msg.lower() or "Stop" in msg
        assert "target" in msg.lower() or "Target" in msg

    def test_disabled_returns_false(self):
        notifier = TelegramNotifier("tok", "chat", enabled=False)
        with patch("requests.post") as mock_post:
            result = notifier.send_trade_alert("BUY", "BTC/USD", 50_000, 100)
        assert result is False
        mock_post.assert_not_called()

    def test_bitcoin_name_used(self):
        msg = self._call(symbol="BTC/USD")
        assert "Bitcoin" in msg

    def test_eth_name_used(self):
        msg = self._call(symbol="ETH/USD")
        assert "Ethereum" in msg


# ── send_win / send_loss ──────────────────────────────────────────────────────

class TestSendWinLoss:
    def _notifier(self) -> tuple:
        notifier = _enabled()
        messages = []
        def capture(url, json=None, timeout=None):
            messages.append(json["text"])
            r = MagicMock()
            r.raise_for_status = MagicMock()
            return r
        return notifier, messages, capture

    def test_send_win_contains_win(self):
        notifier, messages, capture = self._notifier()
        with patch("requests.post", side_effect=capture):
            notifier.send_win("BTC/USD", pnl=5.0, pnl_pct=5.0,
                              exit_price=52_500, total_equity=1_005)
        assert "WIN" in messages[0] or "win" in messages[0].lower()

    def test_send_loss_contains_loss(self):
        notifier, messages, capture = self._notifier()
        with patch("requests.post", side_effect=capture):
            notifier.send_loss("BTC/USD", pnl=-3.0, pnl_pct=-3.0,
                               exit_price=48_500, total_equity=997)
        assert "LOSS" in messages[0] or "loss" in messages[0].lower()

    def test_send_win_contains_price(self):
        notifier, messages, capture = self._notifier()
        with patch("requests.post", side_effect=capture):
            notifier.send_win("BTC/USD", pnl=5.0, pnl_pct=5.0,
                              exit_price=52_500, total_equity=1_005)
        assert "52,500" in messages[0] or "52500" in messages[0]

    def test_send_loss_contains_equity(self):
        notifier, messages, capture = self._notifier()
        with patch("requests.post", side_effect=capture):
            notifier.send_loss("ETH/USD", pnl=-2.0, pnl_pct=-2.0,
                               exit_price=3_000, total_equity=998)
        assert "998" in messages[0]


# ── send_status ───────────────────────────────────────────────────────────────

class TestSendStatus:
    def _capture(self):
        messages = []
        def side_effect(url, json=None, timeout=None):
            messages.append(json["text"])
            r = MagicMock()
            r.raise_for_status = MagicMock()
            return r
        return messages, side_effect

    def test_positive_pnl_uses_up_icon(self):
        notifier = _enabled()
        messages, cap = self._capture()
        with patch("requests.post", side_effect=cap):
            notifier.send_status(capital=1_010, pnl=10, pnl_pct=1.0,
                                 open_positions=0, trades_today=3)
        assert "\U0001f4c8" in messages[0]

    def test_negative_pnl_uses_down_icon(self):
        notifier = _enabled()
        messages, cap = self._capture()
        with patch("requests.post", side_effect=cap):
            notifier.send_status(capital=990, pnl=-10, pnl_pct=-1.0,
                                 open_positions=0, trades_today=2)
        assert "\U0001f4c9" in messages[0]

    def test_open_positions_mentioned(self):
        notifier = _enabled()
        messages, cap = self._capture()
        with patch("requests.post", side_effect=cap):
            notifier.send_status(capital=1_000, pnl=0, pnl_pct=0,
                                 open_positions=2, trades_today=5)
        assert "2" in messages[0]

    def test_no_open_positions_message(self):
        notifier = _enabled()
        messages, cap = self._capture()
        with patch("requests.post", side_effect=cap):
            notifier.send_status(capital=1_000, pnl=5, pnl_pct=0.5,
                                 open_positions=0, trades_today=1)
        assert "no open position" in messages[0].lower() or "watching" in messages[0].lower()


# ── send_error / test_connection ──────────────────────────────────────────────

class TestSendError:
    def test_send_error_contains_message(self):
        notifier = _enabled()
        messages = []
        def cap(url, json=None, timeout=None):
            messages.append(json["text"])
            r = MagicMock(); r.raise_for_status = MagicMock()
            return r
        with patch("requests.post", side_effect=cap):
            notifier.send_error("network timeout")
        assert "network timeout" in messages[0]

    def test_test_connection_returns_send_result(self):
        notifier = _disabled()
        result = notifier.test_connection()
        assert result is False


# ── send_trade_analysis ───────────────────────────────────────────────────────

class TestSendTradeAnalysis:
    def _call(self, pnl=5.0, issues=None, positives=None, loss_streak=0,
              win_streak=0, adaptations=None):
        notifier = _enabled()
        messages = []
        def cap(url, json=None, timeout=None):
            messages.append(json["text"])
            r = MagicMock(); r.raise_for_status = MagicMock()
            return r
        with patch("requests.post", side_effect=cap):
            notifier.send_trade_analysis(
                symbol="BTC/USD", side="buy", pnl=pnl, pnl_pct=pnl/100,
                entry_price=50_000, exit_price=50_500, total_equity=1_005,
                exit_reason="TAKE_PROFIT", holding_minutes=5,
                regime="TRENDING_UP", regime_conf=0.8,
                rsi=55, adx=25, volume_ratio=1.2,
                ofi=0.30, funding_apy=None, btc_lead="BUY",
                issues=issues or [], positives=positives or [],
                loss_streak=loss_streak, win_streak=win_streak,
                adaptations=adaptations,
            )
        return messages[0] if messages else None

    def test_win_shows_win(self):
        msg = self._call(pnl=5.0)
        assert "WIN" in msg

    def test_loss_shows_loss(self):
        msg = self._call(pnl=-3.0)
        assert "LOSS" in msg

    def test_loss_streak_warning_when_3(self):
        msg = self._call(pnl=-1.0, loss_streak=3)
        assert "3" in msg and ("loss" in msg.lower() or "row" in msg.lower())

    def test_win_streak_shown_when_3(self):
        msg = self._call(pnl=1.0, win_streak=3)
        assert "3" in msg

    def test_adaptation_shown_when_present(self):
        msg = self._call(adaptations=["min confidence raised to 68"])
        assert "68" in msg or "adjusted" in msg.lower() or "self-adjusted" in msg.lower()

    def test_positives_shown_on_win(self):
        msg = self._call(pnl=5.0, positives=["Target reached as predicted"])
        assert "worked" in msg.lower() or "target" in msg.lower()

    def test_issues_shown_on_loss(self):
        msg = self._call(pnl=-3.0, issues=["RSI 72 was overbought at entry"])
        assert "wrong" in msg.lower() or "overbought" in msg.lower()

    def test_account_equity_shown(self):
        msg = self._call(pnl=5.0)
        assert "1,005" in msg or "1005" in msg

    def test_price_range_shown(self):
        msg = self._call()
        assert "50,000" in msg or "50000" in msg


# ── create_notifier_from_env ──────────────────────────────────────────────────

class TestCreateNotifierFromEnv:
    def test_no_tokens_returns_disabled(self):
        env = {"TELEGRAM_BOT_TOKEN": "", "TELEGRAM_CHAT_ID": "", "TELEGRAM_ENABLED": "true"}
        with patch.dict(os.environ, env, clear=False):
            notifier = create_notifier_from_env()
        assert not notifier.enabled

    def test_partial_token_only_returns_disabled(self):
        env = {"TELEGRAM_BOT_TOKEN": "abc123", "TELEGRAM_CHAT_ID": "", "TELEGRAM_ENABLED": "true"}
        with patch.dict(os.environ, env, clear=False):
            notifier = create_notifier_from_env()
        assert not notifier.enabled

    def test_full_config_enabled_true(self):
        env = {
            "TELEGRAM_BOT_TOKEN": "real_token",
            "TELEGRAM_CHAT_ID": "123456",
            "TELEGRAM_ENABLED": "true",
        }
        with patch.dict(os.environ, env, clear=False):
            notifier = create_notifier_from_env()
        assert notifier.enabled

    def test_full_config_enabled_false(self):
        env = {
            "TELEGRAM_BOT_TOKEN": "real_token",
            "TELEGRAM_CHAT_ID": "123456",
            "TELEGRAM_ENABLED": "false",
        }
        with patch.dict(os.environ, env, clear=False):
            notifier = create_notifier_from_env()
        assert not notifier.enabled

    def test_missing_enabled_key_defaults_to_false(self):
        env = {
            "TELEGRAM_BOT_TOKEN": "real_token",
            "TELEGRAM_CHAT_ID": "123456",
        }
        # Remove TELEGRAM_ENABLED if present
        with patch.dict(os.environ, env, clear=False):
            os.environ.pop("TELEGRAM_ENABLED", None)
            notifier = create_notifier_from_env()
        assert not notifier.enabled

    def test_configured_notifier_is_async_safe(self):
        """The bot's shared notifier sends from inside the asyncio trading loop —
        it must use the non-blocking (async_safe) path, not the default sync one."""
        env = {
            "TELEGRAM_BOT_TOKEN": "real_token",
            "TELEGRAM_CHAT_ID": "123456",
            "TELEGRAM_ENABLED": "true",
        }
        with patch.dict(os.environ, env, clear=False):
            notifier = create_notifier_from_env()
        assert notifier._async_safe is True

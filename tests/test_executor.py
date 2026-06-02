"""
Unit tests for src/executor.py

Covers:
- OrderResult dataclass: field defaults, is_leveraged flag
- PaperExecutor: delegates open_long/open_short/close_long/close_short to PaperTrader;
  OrderResult.success mirrors whether the trade was recorded
- KrakenPerpsExecutor: open_long / open_short place market orders with correct side,
  track _open_size, extract filled_price/fee from ccxt response
- KrakenPerpsExecutor.close_long / close_short: use reduce_only=True, read accumulated
  _open_size, set size back to 0 on success
- KrakenPerpsExecutor: zero-amount guard (price=0 → error OrderResult)
- KrakenPerpsExecutor: no-position guard on close (returns error, does not call exchange)
- KrakenPerpsExecutor: exchange exception → error OrderResult, does not propagate
- KrakenPerpsExecutor._perp_symbol: BTC/USD → BTC/USD:USD; already-qualified passthrough
- KrakenPerpsExecutor._amount_from_usd: size_usd / price; price=0 guard
- KrakenSpotExecutor: supports_shorts returns False
- KrakenSpotExecutor: open_short/close_short return error OrderResult (not raise)
- KrakenSpotExecutor: open_long/close_long raise NotImplementedError (stub)
- make_executor: paper mode returns PaperExecutor; spot mode returns KrakenSpotExecutor;
  perps mode returns KrakenPerpsExecutor; missing client raises ValueError;
  unknown mode raises ValueError
"""

import os
import pytest
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from src.executor import (
    OrderResult,
    Executor,
    PaperExecutor,
    KrakenSpotExecutor,
    KrakenPerpsExecutor,
    make_executor,
)

NOW = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)


# ── helpers ───────────────────────────────────────────────────────────────────

def _mock_ccxt(
    order_id: str = "ord-123",
    average: float = 50_000.0,
    filled: float = 0.001,
    fee_cost: float = 1.30,
) -> MagicMock:
    """A minimal ccxt futures client mock that returns a realistic order dict."""
    client = MagicMock()
    client.create_order.return_value = {
        "id": order_id,
        "average": average,
        "filled": filled,
        "fee": {"cost": fee_cost},
    }
    return client


def _mock_trader(
    buy_returns=None,
    sell_returns=None,
    short_returns=None,
    cover_returns=None,
) -> MagicMock:
    """A PaperTrader mock with configurable return values per method."""
    trader = MagicMock()
    trader.execute_buy   = MagicMock(return_value=buy_returns)
    trader.execute_sell  = MagicMock(return_value=sell_returns)
    trader.execute_short = MagicMock(return_value=short_returns)
    trader.execute_cover = MagicMock(return_value=cover_returns)
    return trader


# ── OrderResult ───────────────────────────────────────────────────────────────

class TestOrderResult:
    def test_success_true(self):
        r = OrderResult(True, "id", 50_000.0, 0.001, 1.0)
        assert r.success is True

    def test_error_defaults_to_none(self):
        r = OrderResult(True, "id", 50_000.0, 0.001, 1.0)
        assert r.error is None

    def test_is_leveraged_defaults_to_false(self):
        r = OrderResult(True, "id", 50_000.0, 0.001, 1.0)
        assert r.is_leveraged is False

    def test_leverage_defaults_to_one(self):
        r = OrderResult(True, "id", 50_000.0, 0.001, 1.0)
        assert r.leverage == 1.0

    def test_error_result(self):
        r = OrderResult(False, None, 0, 0, 0, error="exchange down")
        assert r.success is False
        assert r.error == "exchange down"

    def test_leveraged_result(self):
        r = OrderResult(True, "id", 50_000.0, 0.001, 1.0, is_leveraged=True, leverage=3.0)
        assert r.is_leveraged is True
        assert r.leverage == 3.0


# ── PaperExecutor ─────────────────────────────────────────────────────────────

class TestPaperExecutor:
    def _make_position(self, entry_fee: float = 1.3) -> MagicMock:
        pos = MagicMock()
        pos.entry_fee = entry_fee
        return pos

    def test_open_long_delegates_to_execute_buy(self):
        pos = self._make_position()
        trader = _mock_trader(buy_returns=pos)
        ex = PaperExecutor(trader)
        ex.open_long("BTC/USD", 50_000.0, 100.0, NOW)
        trader.execute_buy.assert_called_once_with(
            "BTC/USD", 50_000.0, NOW, size_usd=100.0, signal=None
        )

    def test_open_long_success_when_position_returned(self):
        pos = self._make_position(entry_fee=2.0)
        ex = PaperExecutor(_mock_trader(buy_returns=pos))
        result = ex.open_long("BTC/USD", 50_000.0, 100.0, NOW)
        assert result.success is True
        assert result.fee == pytest.approx(2.0)

    def test_open_long_failure_when_none_returned(self):
        ex = PaperExecutor(_mock_trader(buy_returns=None))
        result = ex.open_long("BTC/USD", 50_000.0, 100.0, NOW)
        assert result.success is False
        assert result.fee == 0.0

    def test_open_short_delegates_to_execute_short(self):
        pos = self._make_position()
        trader = _mock_trader(short_returns=pos)
        ex = PaperExecutor(trader)
        ex.open_short("ETH/USD", 3_000.0, 50.0, NOW)
        trader.execute_short.assert_called_once_with(
            "ETH/USD", 3_000.0, NOW, size_usd=50.0, signal=None
        )

    def test_open_short_success_when_position_returned(self):
        pos = self._make_position(entry_fee=0.5)
        ex = PaperExecutor(_mock_trader(short_returns=pos))
        result = ex.open_short("ETH/USD", 3_000.0, 50.0, NOW)
        assert result.success is True

    def test_open_short_failure_when_none_returned(self):
        ex = PaperExecutor(_mock_trader(short_returns=None))
        result = ex.open_short("ETH/USD", 3_000.0, 50.0, NOW)
        assert result.success is False

    def test_close_long_delegates_to_execute_sell(self):
        trade = MagicMock()
        trader = _mock_trader(sell_returns=trade)
        ex = PaperExecutor(trader)
        ex.close_long("BTC/USD", 51_000.0, NOW, reason="TAKE_PROFIT")
        trader.execute_sell.assert_called_once_with(
            "BTC/USD", 51_000.0, NOW, reason="TAKE_PROFIT"
        )

    def test_close_long_success_when_trade_returned(self):
        ex = PaperExecutor(_mock_trader(sell_returns=MagicMock()))
        result = ex.close_long("BTC/USD", 51_000.0, NOW)
        assert result.success is True

    def test_close_long_failure_when_none_returned(self):
        ex = PaperExecutor(_mock_trader(sell_returns=None))
        result = ex.close_long("BTC/USD", 51_000.0, NOW)
        assert result.success is False

    def test_close_short_delegates_to_execute_cover(self):
        trade = MagicMock()
        trader = _mock_trader(cover_returns=trade)
        ex = PaperExecutor(trader)
        ex.close_short("ETH/USD", 2_900.0, NOW, reason="STOP_LOSS")
        trader.execute_cover.assert_called_once_with(
            "ETH/USD", 2_900.0, NOW, reason="STOP_LOSS"
        )

    def test_close_short_success_when_trade_returned(self):
        ex = PaperExecutor(_mock_trader(cover_returns=MagicMock()))
        result = ex.close_short("ETH/USD", 2_900.0, NOW)
        assert result.success is True

    def test_open_long_forwards_signal_kwarg(self):
        trader = _mock_trader(buy_returns=MagicMock())
        ex = PaperExecutor(trader)
        sig = MagicMock()
        ex.open_long("BTC/USD", 50_000.0, 100.0, NOW, signal=sig)
        _, kwargs = trader.execute_buy.call_args
        assert kwargs["signal"] is sig

    def test_filled_price_equals_passed_price(self):
        ex = PaperExecutor(_mock_trader(buy_returns=MagicMock()))
        result = ex.open_long("BTC/USD", 50_000.0, 100.0, NOW)
        assert result.filled_price == pytest.approx(50_000.0)

    def test_filled_size_is_size_usd_divided_by_price(self):
        ex = PaperExecutor(_mock_trader(buy_returns=MagicMock()))
        result = ex.open_long("BTC/USD", 50_000.0, 100.0, NOW)
        assert result.filled_size == pytest.approx(100.0 / 50_000.0)

    def test_filled_size_is_zero_when_price_is_zero(self):
        ex = PaperExecutor(_mock_trader(buy_returns=MagicMock()))
        result = ex.open_long("BTC/USD", 0.0, 100.0, NOW)
        assert result.filled_size == 0.0


# ── KrakenPerpsExecutor ───────────────────────────────────────────────────────

class TestKrakenPerpsExecutorAmountAndSymbol:
    def test_amount_from_usd_correct(self):
        ex = KrakenPerpsExecutor(MagicMock())
        assert ex._amount_from_usd(50_000.0, 100.0) == pytest.approx(0.002)

    def test_amount_from_usd_price_zero_returns_zero(self):
        ex = KrakenPerpsExecutor(MagicMock())
        assert ex._amount_from_usd(0.0, 100.0) == 0.0

    def test_perp_symbol_adds_colon_usd(self):
        ex = KrakenPerpsExecutor(MagicMock())
        assert ex._perp_symbol("BTC/USD") == "BTC/USD:USD"

    def test_perp_symbol_passthrough_when_qualified(self):
        ex = KrakenPerpsExecutor(MagicMock())
        assert ex._perp_symbol("BTC/USD:USD") == "BTC/USD:USD"

    def test_perp_symbol_eth(self):
        ex = KrakenPerpsExecutor(MagicMock())
        assert ex._perp_symbol("ETH/USD") == "ETH/USD:USD"


class TestKrakenPerpsExecutorOpenLong:
    def test_places_buy_market_order(self):
        client = _mock_ccxt()
        ex = KrakenPerpsExecutor(client, leverage=3.0)
        ex.open_long("BTC/USD", 50_000.0, 100.0, NOW)
        client.create_order.assert_called_once()
        args, kwargs = client.create_order.call_args
        assert args[0] == "BTC/USD:USD"       # perp symbol
        assert kwargs["side"] == "buy"         # side is a keyword arg

    def test_open_long_success_result(self):
        client = _mock_ccxt(order_id="x1", average=50_100.0, filled=0.002, fee_cost=1.5)
        ex = KrakenPerpsExecutor(client, leverage=3.0)
        result = ex.open_long("BTC/USD", 50_000.0, 100.0, NOW)
        assert result.success is True
        assert result.order_id == "x1"
        assert result.filled_price == pytest.approx(50_100.0)
        assert result.filled_size == pytest.approx(0.002)
        assert result.fee == pytest.approx(1.5)
        assert result.is_leveraged is True
        assert result.leverage == pytest.approx(3.0)

    def test_open_long_tracks_size(self):
        client = _mock_ccxt(filled=0.002)
        ex = KrakenPerpsExecutor(client, leverage=3.0)
        ex.open_long("BTC/USD", 50_000.0, 100.0, NOW)
        assert ex._open_size.get("BTC/USD", 0.0) == pytest.approx(0.002)

    def test_open_long_accumulates_size_on_second_call(self):
        client = _mock_ccxt(filled=0.001)
        ex = KrakenPerpsExecutor(client)
        ex.open_long("BTC/USD", 50_000.0, 50.0, NOW)
        ex.open_long("BTC/USD", 50_000.0, 50.0, NOW)
        assert ex._open_size["BTC/USD"] == pytest.approx(0.002)

    def test_open_long_zero_amount_returns_error(self):
        client = _mock_ccxt()
        ex = KrakenPerpsExecutor(client)
        result = ex.open_long("BTC/USD", 0.0, 100.0, NOW)   # price=0 → amount=0
        assert result.success is False
        assert "zero" in (result.error or "").lower()
        client.create_order.assert_not_called()

    def test_open_long_exchange_exception_returns_error(self):
        client = MagicMock()
        client.create_order.side_effect = Exception("network error")
        ex = KrakenPerpsExecutor(client)
        result = ex.open_long("BTC/USD", 50_000.0, 100.0, NOW)
        assert result.success is False
        assert result.error is not None

    def test_open_long_exchange_exception_does_not_propagate(self):
        client = MagicMock()
        client.create_order.side_effect = RuntimeError("boom")
        ex = KrakenPerpsExecutor(client)
        # must not raise
        result = ex.open_long("BTC/USD", 50_000.0, 100.0, NOW)
        assert result.success is False

    def test_open_long_uses_leverage_param(self):
        client = _mock_ccxt()
        ex = KrakenPerpsExecutor(client, leverage=5.0)
        ex.open_long("BTC/USD", 50_000.0, 100.0, NOW)
        _, kwargs = client.create_order.call_args
        assert kwargs.get("params", {}).get("leverage") == 5.0

    def test_open_long_not_reduce_only(self):
        client = _mock_ccxt()
        ex = KrakenPerpsExecutor(client)
        ex.open_long("BTC/USD", 50_000.0, 100.0, NOW)
        _, kwargs = client.create_order.call_args
        assert kwargs.get("params", {}).get("reduceOnly") is False

    def test_open_long_fallback_price_when_average_missing(self):
        client = MagicMock()
        client.create_order.return_value = {"id": "x", "average": None, "price": 49_500.0,
                                            "filled": 0.001, "fee": {"cost": 0.5}}
        ex = KrakenPerpsExecutor(client)
        result = ex.open_long("BTC/USD", 50_000.0, 50.0, NOW)
        assert result.filled_price == pytest.approx(49_500.0)

    def test_open_long_fallback_price_to_passed_price(self):
        client = MagicMock()
        client.create_order.return_value = {"id": "x", "average": None, "price": None,
                                            "filled": 0.001, "fee": {"cost": 0.5}}
        ex = KrakenPerpsExecutor(client)
        result = ex.open_long("BTC/USD", 50_000.0, 50.0, NOW)
        assert result.filled_price == pytest.approx(50_000.0)


class TestKrakenPerpsExecutorOpenShort:
    def test_places_sell_market_order(self):
        client = _mock_ccxt()
        ex = KrakenPerpsExecutor(client, leverage=3.0)
        ex.open_short("BTC/USD", 50_000.0, 100.0, NOW)
        client.create_order.assert_called_once()
        _, kwargs = client.create_order.call_args
        assert kwargs["side"] == "sell"

    def test_open_short_tracks_negative_size(self):
        client = _mock_ccxt(filled=0.002)
        ex = KrakenPerpsExecutor(client)
        ex.open_short("BTC/USD", 50_000.0, 100.0, NOW)
        assert ex._open_size.get("BTC/USD", 0.0) == pytest.approx(-0.002)

    def test_open_short_success_result(self):
        client = _mock_ccxt(order_id="short-1", average=50_500.0, filled=0.001, fee_cost=0.8)
        ex = KrakenPerpsExecutor(client, leverage=2.0)
        result = ex.open_short("BTC/USD", 50_000.0, 50.0, NOW)
        assert result.success is True
        assert result.filled_price == pytest.approx(50_500.0)
        assert result.is_leveraged is True
        assert result.leverage == pytest.approx(2.0)

    def test_open_short_zero_amount_returns_error(self):
        client = _mock_ccxt()
        ex = KrakenPerpsExecutor(client)
        result = ex.open_short("BTC/USD", 0.0, 100.0, NOW)
        assert result.success is False
        client.create_order.assert_not_called()

    def test_open_short_exchange_exception_returns_error(self):
        client = MagicMock()
        client.create_order.side_effect = Exception("timeout")
        ex = KrakenPerpsExecutor(client)
        result = ex.open_short("BTC/USD", 50_000.0, 100.0, NOW)
        assert result.success is False
        assert result.error is not None


class TestKrakenPerpsExecutorCloseLong:
    def test_close_long_places_sell_reduce_only_order(self):
        client = _mock_ccxt(filled=0.002)
        ex = KrakenPerpsExecutor(client)
        ex._open_size["BTC/USD"] = 0.002          # pre-seed open position
        ex.close_long("BTC/USD", 51_000.0, NOW)
        client.create_order.assert_called_once()
        _, kwargs = client.create_order.call_args
        assert kwargs["side"] == "sell"
        assert kwargs.get("params", {}).get("reduceOnly") is True

    def test_close_long_success_result(self):
        client = _mock_ccxt(order_id="cl-1", average=51_000.0, filled=0.002)
        ex = KrakenPerpsExecutor(client)
        ex._open_size["BTC/USD"] = 0.002
        result = ex.close_long("BTC/USD", 51_000.0, NOW, reason="TAKE_PROFIT")
        assert result.success is True
        assert result.filled_price == pytest.approx(51_000.0)

    def test_close_long_resets_open_size_to_zero(self):
        client = _mock_ccxt(filled=0.002)
        ex = KrakenPerpsExecutor(client)
        ex._open_size["BTC/USD"] = 0.002
        ex.close_long("BTC/USD", 51_000.0, NOW)
        assert ex._open_size["BTC/USD"] == pytest.approx(0.0)

    def test_close_long_no_position_returns_error(self):
        client = _mock_ccxt()
        ex = KrakenPerpsExecutor(client)
        # _open_size not set → defaults to 0 → amount=0 → error
        result = ex.close_long("BTC/USD", 51_000.0, NOW)
        assert result.success is False
        assert "no long" in (result.error or "").lower()
        client.create_order.assert_not_called()

    def test_close_long_exchange_exception_returns_error(self):
        client = MagicMock()
        client.create_order.side_effect = Exception("disconnected")
        ex = KrakenPerpsExecutor(client)
        ex._open_size["BTC/USD"] = 0.001
        result = ex.close_long("BTC/USD", 51_000.0, NOW)
        assert result.success is False

    def test_close_long_uses_abs_of_open_size(self):
        """Size is always positive regardless of sign stored in _open_size."""
        client = _mock_ccxt(filled=0.002)
        ex = KrakenPerpsExecutor(client)
        ex._open_size["BTC/USD"] = 0.002
        ex.close_long("BTC/USD", 50_000.0, NOW)
        _, kwargs = client.create_order.call_args
        assert kwargs["amount"] == pytest.approx(0.002)


class TestKrakenPerpsExecutorCloseShort:
    def test_close_short_places_buy_reduce_only_order(self):
        client = _mock_ccxt(filled=0.001)
        ex = KrakenPerpsExecutor(client)
        ex._open_size["ETH/USD"] = -0.001         # short position (negative)
        ex.close_short("ETH/USD", 2_900.0, NOW)
        _, kwargs = client.create_order.call_args
        assert kwargs["side"] == "buy"
        assert kwargs.get("params", {}).get("reduceOnly") is True

    def test_close_short_resets_open_size_to_zero(self):
        client = _mock_ccxt(filled=0.001)
        ex = KrakenPerpsExecutor(client)
        ex._open_size["ETH/USD"] = -0.001
        ex.close_short("ETH/USD", 2_900.0, NOW)
        assert ex._open_size["ETH/USD"] == pytest.approx(0.0)

    def test_close_short_no_position_returns_error(self):
        client = _mock_ccxt()
        ex = KrakenPerpsExecutor(client)
        result = ex.close_short("ETH/USD", 2_900.0, NOW)
        assert result.success is False
        assert "no short" in (result.error or "").lower()
        client.create_order.assert_not_called()

    def test_close_short_success_result(self):
        client = _mock_ccxt(order_id="cs-1", average=2_900.0)
        ex = KrakenPerpsExecutor(client)
        ex._open_size["ETH/USD"] = -0.002
        result = ex.close_short("ETH/USD", 2_900.0, NOW, reason="STOP_LOSS")
        assert result.success is True

    def test_close_short_uses_abs_of_negative_size(self):
        """abs(-0.002) = 0.002 — close must use positive amount."""
        client = _mock_ccxt(filled=0.002)
        ex = KrakenPerpsExecutor(client)
        ex._open_size["ETH/USD"] = -0.002
        ex.close_short("ETH/USD", 2_900.0, NOW)
        _, kwargs = client.create_order.call_args
        assert kwargs["amount"] == pytest.approx(0.002)

    def test_close_short_exchange_exception_returns_error(self):
        client = MagicMock()
        client.create_order.side_effect = RuntimeError("timeout")
        ex = KrakenPerpsExecutor(client)
        ex._open_size["SOL/USD"] = -0.01
        result = ex.close_short("SOL/USD", 150.0, NOW)
        assert result.success is False


class TestKrakenPerpsExecutorLeverageInit:
    def test_default_leverage_is_from_env_or_3(self):
        ex = KrakenPerpsExecutor(MagicMock())
        assert ex.leverage >= 1.0   # at least 1x

    def test_custom_leverage_stored(self):
        ex = KrakenPerpsExecutor(MagicMock(), leverage=10.0)
        assert ex.leverage == pytest.approx(10.0)

    def test_leverage_clamped_to_minimum_1(self):
        ex = KrakenPerpsExecutor(MagicMock(), leverage=0.0)
        assert ex.leverage == pytest.approx(1.0)

    def test_is_leveraged_flag_in_open_result(self):
        client = _mock_ccxt()
        ex = KrakenPerpsExecutor(client, leverage=5.0)
        result = ex.open_long("BTC/USD", 50_000.0, 100.0, NOW)
        assert result.is_leveraged is True

    def test_supports_shorts_is_true(self):
        assert KrakenPerpsExecutor(MagicMock()).supports_shorts() is True


# ── KrakenSpotExecutor ────────────────────────────────────────────────────────

class TestKrakenSpotExecutor:
    def test_supports_shorts_is_false(self):
        ex = KrakenSpotExecutor(MagicMock())
        assert ex.supports_shorts() is False

    def test_open_short_returns_error_order_result(self):
        ex = KrakenSpotExecutor(MagicMock())
        result = ex.open_short("BTC/USD", 50_000.0, 100.0, NOW)
        assert result.success is False
        assert result.error is not None
        assert "short" in (result.error or "").lower()

    def test_close_short_returns_error_order_result(self):
        ex = KrakenSpotExecutor(MagicMock())
        result = ex.close_short("BTC/USD", 51_000.0, NOW)
        assert result.success is False
        assert result.error is not None

    def test_open_long_raises_not_implemented(self):
        ex = KrakenSpotExecutor(MagicMock())
        with pytest.raises(NotImplementedError):
            ex.open_long("BTC/USD", 50_000.0, 100.0, NOW)

    def test_close_long_raises_not_implemented(self):
        ex = KrakenSpotExecutor(MagicMock())
        with pytest.raises(NotImplementedError):
            ex.close_long("BTC/USD", 51_000.0, NOW)


# ── make_executor factory ─────────────────────────────────────────────────────

class TestMakeExecutor:
    def test_paper_mode_returns_paper_executor(self, monkeypatch):
        monkeypatch.setenv("TRADING_MODE", "paper")
        with patch("src.executor.TRADING_MODE", "paper"):
            ex = make_executor(trader=MagicMock())
        assert isinstance(ex, PaperExecutor)

    def test_paper_mode_without_trader_raises(self, monkeypatch):
        with patch("src.executor.TRADING_MODE", "paper"):
            with pytest.raises(ValueError, match="PaperExecutor"):
                make_executor(trader=None)

    def test_spot_mode_returns_kraken_spot_executor(self):
        with patch("src.executor.TRADING_MODE", "spot"):
            ex = make_executor(ccxt_client=MagicMock())
        assert isinstance(ex, KrakenSpotExecutor)

    def test_spot_mode_without_client_raises(self):
        with patch("src.executor.TRADING_MODE", "spot"):
            with pytest.raises(ValueError, match="KrakenSpotExecutor"):
                make_executor(ccxt_client=None)

    def test_perps_mode_returns_kraken_perps_executor(self):
        with patch("src.executor.TRADING_MODE", "perps"):
            ex = make_executor(ccxt_futures_client=MagicMock())
        assert isinstance(ex, KrakenPerpsExecutor)

    def test_perps_mode_without_client_raises(self):
        with patch("src.executor.TRADING_MODE", "perps"):
            with pytest.raises(ValueError, match="KrakenPerpsExecutor"):
                make_executor(ccxt_futures_client=None)

    def test_unknown_mode_raises(self):
        with patch("src.executor.TRADING_MODE", "live"):
            with pytest.raises(ValueError, match="Unknown TRADING_MODE"):
                make_executor()


# ── Executor ABC contract ─────────────────────────────────────────────────────

class TestExecutorAbcContract:
    def test_paper_executor_is_executor(self):
        ex = PaperExecutor(_mock_trader())
        assert isinstance(ex, Executor)

    def test_kraken_perps_executor_is_executor(self):
        ex = KrakenPerpsExecutor(MagicMock())
        assert isinstance(ex, Executor)

    def test_kraken_spot_executor_is_executor(self):
        ex = KrakenSpotExecutor(MagicMock())
        assert isinstance(ex, Executor)

    def test_default_supports_shorts_true(self):
        """Base Executor.supports_shorts() defaults True; spot overrides to False."""
        assert Executor.supports_shorts(None) is True

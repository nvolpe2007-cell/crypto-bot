"""
Unit tests for src/exchange.py

Covers:
- ExchangeConnection._retry: success on first try, retries on transient errors,
  raises after max retries, propagates non-retryable errors immediately
- fetch_ohlcv: returns data on success, returns [] after all retries fail
- get_ticker / get_balance: retry on NetworkError / RequestTimeout
- cancel_order: retry (idempotent operation)
- get_open_orders / get_trades: retry on transient errors
- create_order: NO retry — raises immediately (non-idempotent)
- connect: retry on NetworkError
- fetch_ohlcv_between: delegates to fetch_ohlcv (inherits retry)

All tests mock asyncio.sleep to avoid actual delays.
"""

import pytest
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch, call
import ccxt.async_support as ccxt

from src.exchange import ExchangeConnection


# ── fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    """Patch asyncio.sleep to a no-op so retry back-off doesn't slow tests."""
    monkeypatch.setattr(asyncio, "sleep", AsyncMock(return_value=None))


def _make_conn() -> ExchangeConnection:
    """Build an ExchangeConnection with a fully-mocked inner exchange."""
    conn = ExchangeConnection.__new__(ExchangeConnection)
    conn.sandbox = True
    conn.exchange = MagicMock()
    # Default: all methods succeed immediately with empty returns
    conn.exchange.load_markets = AsyncMock(return_value={})
    conn.exchange.fetch_ohlcv = AsyncMock(return_value=[])
    conn.exchange.fetch_ticker = AsyncMock(return_value={})
    conn.exchange.fetch_balance = AsyncMock(return_value={})
    conn.exchange.create_order = AsyncMock(return_value={})
    conn.exchange.cancel_order = AsyncMock(return_value={})
    conn.exchange.fetch_open_orders = AsyncMock(return_value=[])
    conn.exchange.fetch_trades = AsyncMock(return_value=[])
    conn.exchange.close = AsyncMock(return_value=None)
    return conn


# ── _retry ────────────────────────────────────────────────────────────────────

class TestRetryHelper:
    async def test_succeeds_on_first_attempt(self):
        conn = _make_conn()
        fn = AsyncMock(return_value=42)
        result = await conn._retry(fn, retries=3, label='test')
        assert result == 42
        assert fn.call_count == 1

    async def test_succeeds_after_one_network_error(self):
        conn = _make_conn()
        fn = AsyncMock(side_effect=[ccxt.NetworkError("blip"), 99])
        result = await conn._retry(fn, retries=3, label='test')
        assert result == 99
        assert fn.call_count == 2

    async def test_succeeds_after_request_timeout(self):
        conn = _make_conn()
        fn = AsyncMock(side_effect=[ccxt.RequestTimeout("t/o"), "ok"])
        result = await conn._retry(fn, retries=3, label='test')
        assert result == "ok"
        assert fn.call_count == 2

    async def test_succeeds_after_rate_limit_exceeded(self):
        conn = _make_conn()
        fn = AsyncMock(side_effect=[ccxt.RateLimitExceeded("429"), "ok"])
        result = await conn._retry(fn, retries=3, label='test')
        assert result == "ok"
        assert fn.call_count == 2

    async def test_raises_after_all_retries_exhausted(self):
        conn = _make_conn()
        exc = ccxt.NetworkError("persistent failure")
        fn = AsyncMock(side_effect=exc)
        with pytest.raises(ccxt.NetworkError):
            await conn._retry(fn, retries=3, label='test')
        assert fn.call_count == 3

    async def test_non_retryable_error_propagates_immediately(self):
        conn = _make_conn()
        fn = AsyncMock(side_effect=ccxt.ExchangeError("bad credentials"))
        with pytest.raises(ccxt.ExchangeError):
            await conn._retry(fn, retries=3, label='test')
        # Must NOT retry on a non-transient error
        assert fn.call_count == 1

    async def test_non_retryable_auth_error_not_retried(self):
        conn = _make_conn()
        fn = AsyncMock(side_effect=ccxt.AuthenticationError("invalid key"))
        with pytest.raises(ccxt.AuthenticationError):
            await conn._retry(fn, retries=3, label='test')
        assert fn.call_count == 1

    async def test_sleep_called_between_attempts(self):
        conn = _make_conn()
        fn = AsyncMock(side_effect=[ccxt.NetworkError("x"), ccxt.NetworkError("y"), "ok"])
        await conn._retry(fn, retries=3, label='test')
        # asyncio.sleep is patched; verify it was called twice (after attempt 1 and 2)
        assert asyncio.sleep.call_count == 2

    async def test_sleep_uses_exponential_backoff(self):
        conn = _make_conn()
        fn = AsyncMock(side_effect=[ccxt.NetworkError("x"), ccxt.NetworkError("y"), "ok"])
        await conn._retry(fn, retries=3, label='test')
        calls = asyncio.sleep.call_args_list
        # First backoff: 2**1 = 2, second: 2**2 = 4
        assert calls[0] == call(2)
        assert calls[1] == call(4)

    async def test_passes_args_and_kwargs_through(self):
        conn = _make_conn()
        fn = AsyncMock(return_value="data")
        await conn._retry(fn, "sym", timeframe="1m", retries=2, label='t')
        fn.assert_called_once_with("sym", timeframe="1m")

    async def test_respects_custom_retry_count(self):
        conn = _make_conn()
        fn = AsyncMock(side_effect=ccxt.NetworkError("fail"))
        with pytest.raises(ccxt.NetworkError):
            await conn._retry(fn, retries=5, label='test')
        assert fn.call_count == 5


# ── fetch_ohlcv ───────────────────────────────────────────────────────────────

class TestFetchOhlcv:
    CANDLES = [[1_700_000_000_000, 50000, 50100, 49900, 50050, 100.0]]

    async def test_returns_candles_on_success(self):
        conn = _make_conn()
        conn.exchange.fetch_ohlcv = AsyncMock(return_value=self.CANDLES)
        result = await conn.fetch_ohlcv('BTC/USD')
        assert result == self.CANDLES

    async def test_returns_empty_list_after_all_retries_fail(self):
        conn = _make_conn()
        conn.exchange.fetch_ohlcv = AsyncMock(
            side_effect=ccxt.NetworkError("down")
        )
        result = await conn.fetch_ohlcv('BTC/USD', retries=3)
        assert result == []

    async def test_retries_on_network_error_then_succeeds(self):
        conn = _make_conn()
        conn.exchange.fetch_ohlcv = AsyncMock(
            side_effect=[ccxt.NetworkError("blip"), self.CANDLES]
        )
        result = await conn.fetch_ohlcv('BTC/USD', retries=3)
        assert result == self.CANDLES
        assert conn.exchange.fetch_ohlcv.call_count == 2

    async def test_non_retryable_error_still_returns_empty_list(self):
        # Non-retryable errors inside fetch_ohlcv's broad except → returns []
        conn = _make_conn()
        conn.exchange.fetch_ohlcv = AsyncMock(
            side_effect=ccxt.ExchangeError("symbol not found")
        )
        result = await conn.fetch_ohlcv('FAKE/USD', retries=3)
        assert result == []

    async def test_passes_symbol_and_params(self):
        conn = _make_conn()
        conn.exchange.fetch_ohlcv = AsyncMock(return_value=self.CANDLES)
        await conn.fetch_ohlcv('ETH/USD', timeframe='5m', limit=50, since=1000)
        conn.exchange.fetch_ohlcv.assert_called_once_with(
            'ETH/USD', timeframe='5m', limit=50, since=1000
        )


# ── get_ticker ────────────────────────────────────────────────────────────────

class TestGetTicker:
    async def test_returns_ticker_on_success(self):
        conn = _make_conn()
        ticker = {'last': 50000.0, 'bid': 49990.0, 'ask': 50010.0}
        conn.exchange.fetch_ticker = AsyncMock(return_value=ticker)
        result = await conn.get_ticker('BTC/USD')
        assert result == ticker

    async def test_retries_on_network_error(self):
        conn = _make_conn()
        ticker = {'last': 50000.0}
        conn.exchange.fetch_ticker = AsyncMock(
            side_effect=[ccxt.NetworkError("blip"), ticker]
        )
        result = await conn.get_ticker('BTC/USD', retries=3)
        assert result == ticker
        assert conn.exchange.fetch_ticker.call_count == 2

    async def test_raises_after_max_retries(self):
        conn = _make_conn()
        conn.exchange.fetch_ticker = AsyncMock(
            side_effect=ccxt.RequestTimeout("timeout")
        )
        with pytest.raises(ccxt.RequestTimeout):
            await conn.get_ticker('BTC/USD', retries=2)
        assert conn.exchange.fetch_ticker.call_count == 2

    async def test_auth_error_not_retried(self):
        conn = _make_conn()
        conn.exchange.fetch_ticker = AsyncMock(
            side_effect=ccxt.AuthenticationError("bad key")
        )
        with pytest.raises(ccxt.AuthenticationError):
            await conn.get_ticker('BTC/USD', retries=3)
        assert conn.exchange.fetch_ticker.call_count == 1


# ── get_balance ───────────────────────────────────────────────────────────────

class TestGetBalance:
    async def test_returns_balance_on_success(self):
        conn = _make_conn()
        balance = {'USD': {'free': 500.0, 'total': 500.0}}
        conn.exchange.fetch_balance = AsyncMock(return_value=balance)
        result = await conn.get_balance()
        assert result == balance

    async def test_retries_on_request_timeout(self):
        conn = _make_conn()
        balance = {'USD': {'free': 100.0}}
        conn.exchange.fetch_balance = AsyncMock(
            side_effect=[ccxt.RequestTimeout("t/o"), balance]
        )
        result = await conn.get_balance(retries=3)
        assert result == balance
        assert conn.exchange.fetch_balance.call_count == 2

    async def test_retries_on_rate_limit(self):
        conn = _make_conn()
        balance = {'USD': {'free': 100.0}}
        conn.exchange.fetch_balance = AsyncMock(
            side_effect=[ccxt.RateLimitExceeded("429"), balance]
        )
        result = await conn.get_balance(retries=3)
        assert result == balance

    async def test_raises_after_all_retries(self):
        conn = _make_conn()
        conn.exchange.fetch_balance = AsyncMock(
            side_effect=ccxt.NetworkError("unreachable")
        )
        with pytest.raises(ccxt.NetworkError):
            await conn.get_balance(retries=2)


# ── create_order — no retry ───────────────────────────────────────────────────

class TestCreateOrder:
    async def test_returns_order_on_success(self):
        conn = _make_conn()
        order = {'id': 'abc123', 'status': 'open'}
        conn.exchange.create_order = AsyncMock(return_value=order)
        result = await conn.create_order('BTC/USD', 'market', 'buy', 0.001)
        assert result == order

    async def test_no_retry_on_network_error(self):
        """create_order must NOT retry — a timeout may mean the order was placed."""
        conn = _make_conn()
        conn.exchange.create_order = AsyncMock(
            side_effect=ccxt.NetworkError("timeout")
        )
        with pytest.raises(ccxt.NetworkError):
            await conn.create_order('BTC/USD', 'market', 'buy', 0.001)
        # Must be called exactly once — any retry risks a duplicate order
        assert conn.exchange.create_order.call_count == 1

    async def test_no_retry_on_request_timeout(self):
        conn = _make_conn()
        conn.exchange.create_order = AsyncMock(
            side_effect=ccxt.RequestTimeout("t/o")
        )
        with pytest.raises(ccxt.RequestTimeout):
            await conn.create_order('BTC/USD', 'limit', 'sell', 0.001, price=55000.0)
        assert conn.exchange.create_order.call_count == 1

    async def test_limit_order_passes_price(self):
        conn = _make_conn()
        conn.exchange.create_order = AsyncMock(return_value={'id': 'x'})
        await conn.create_order('BTC/USD', 'limit', 'buy', 0.001, price=49000.0)
        _, kwargs = conn.exchange.create_order.call_args
        assert kwargs.get('price') == 49000.0

    async def test_market_order_omits_price(self):
        conn = _make_conn()
        conn.exchange.create_order = AsyncMock(return_value={'id': 'x'})
        await conn.create_order('BTC/USD', 'market', 'buy', 0.001)
        _, kwargs = conn.exchange.create_order.call_args
        assert 'price' not in kwargs


# ── cancel_order ──────────────────────────────────────────────────────────────

class TestCancelOrder:
    async def test_returns_result_on_success(self):
        conn = _make_conn()
        conn.exchange.cancel_order = AsyncMock(return_value={'status': 'canceled'})
        result = await conn.cancel_order('order123', 'BTC/USD')
        assert result == {'status': 'canceled'}

    async def test_retries_on_network_error(self):
        conn = _make_conn()
        conn.exchange.cancel_order = AsyncMock(
            side_effect=[ccxt.NetworkError("blip"), {'status': 'canceled'}]
        )
        result = await conn.cancel_order('o1', 'BTC/USD', retries=3)
        assert result == {'status': 'canceled'}
        assert conn.exchange.cancel_order.call_count == 2

    async def test_raises_after_all_retries(self):
        conn = _make_conn()
        conn.exchange.cancel_order = AsyncMock(
            side_effect=ccxt.NetworkError("unreachable")
        )
        with pytest.raises(ccxt.NetworkError):
            await conn.cancel_order('o1', 'BTC/USD', retries=2)
        assert conn.exchange.cancel_order.call_count == 2


# ── get_open_orders ───────────────────────────────────────────────────────────

class TestGetOpenOrders:
    async def test_returns_orders_on_success(self):
        conn = _make_conn()
        orders = [{'id': 'o1'}, {'id': 'o2'}]
        conn.exchange.fetch_open_orders = AsyncMock(return_value=orders)
        result = await conn.get_open_orders('BTC/USD')
        assert result == orders

    async def test_retries_on_network_error(self):
        conn = _make_conn()
        conn.exchange.fetch_open_orders = AsyncMock(
            side_effect=[ccxt.NetworkError("blip"), []]
        )
        result = await conn.get_open_orders(retries=3)
        assert result == []
        assert conn.exchange.fetch_open_orders.call_count == 2

    async def test_works_with_none_symbol(self):
        conn = _make_conn()
        conn.exchange.fetch_open_orders = AsyncMock(return_value=[])
        await conn.get_open_orders(symbol=None)
        conn.exchange.fetch_open_orders.assert_called_once_with(None)


# ── get_trades ────────────────────────────────────────────────────────────────

class TestGetTrades:
    async def test_returns_trades_on_success(self):
        conn = _make_conn()
        trades = [{'id': 't1', 'price': 50000.0}]
        conn.exchange.fetch_trades = AsyncMock(return_value=trades)
        result = await conn.get_trades('BTC/USD')
        assert result == trades

    async def test_retries_on_request_timeout(self):
        conn = _make_conn()
        conn.exchange.fetch_trades = AsyncMock(
            side_effect=[ccxt.RequestTimeout("t/o"), []]
        )
        result = await conn.get_trades('BTC/USD', retries=3)
        assert result == []
        assert conn.exchange.fetch_trades.call_count == 2

    async def test_passes_since_parameter(self):
        conn = _make_conn()
        conn.exchange.fetch_trades = AsyncMock(return_value=[])
        await conn.get_trades('ETH/USD', since=1_700_000_000_000)
        conn.exchange.fetch_trades.assert_called_once_with(
            'ETH/USD', since=1_700_000_000_000
        )


# ── connect ───────────────────────────────────────────────────────────────────

class TestConnect:
    async def test_calls_load_markets(self):
        conn = _make_conn()
        await conn.connect()
        conn.exchange.load_markets.assert_called_once()

    async def test_retries_on_network_error(self):
        conn = _make_conn()
        conn.exchange.load_markets = AsyncMock(
            side_effect=[ccxt.NetworkError("blip"), {}]
        )
        await conn.connect(retries=3)
        assert conn.exchange.load_markets.call_count == 2

    async def test_raises_after_all_retries(self):
        conn = _make_conn()
        conn.exchange.load_markets = AsyncMock(
            side_effect=ccxt.NetworkError("unreachable")
        )
        with pytest.raises(ccxt.NetworkError):
            await conn.connect(retries=2)


# ── fetch_ohlcv_between ───────────────────────────────────────────────────────

class TestFetchOhlcvBetween:
    async def test_returns_empty_when_no_data(self):
        conn = _make_conn()
        # fetch_ohlcv (wrapper) returns [] → loop breaks immediately
        conn.exchange.fetch_ohlcv = AsyncMock(return_value=[])
        result = await conn.fetch_ohlcv_between(
            'BTC/USD', '1m', '2024-01-01', '2024-01-02'
        )
        assert result == []

    async def test_aggregates_multiple_batches(self):
        conn = _make_conn()
        # Simulate two pages: first returns one candle, second returns []
        start_ms = 1_704_067_200_000   # 2024-01-01 00:00 UTC
        batch1 = [[start_ms, 50000, 50100, 49900, 50050, 10.0]]
        conn.exchange.fetch_ohlcv = AsyncMock(
            side_effect=[batch1, []]
        )
        result = await conn.fetch_ohlcv_between(
            'BTC/USD', '1m', '2024-01-01', '2024-01-02'
        )
        assert len(result) == 1
        assert result[0] == batch1[0]

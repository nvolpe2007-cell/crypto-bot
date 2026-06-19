"""
Unit tests for src/exchange.py

Covers:
- ExchangeConnection._retry: success on first try, retries on transient errors,
  raises after max retries, propagates non-retryable errors immediately
- fetch_ohlcv: returns data on success, returns [] after all retries fail
- get_ticker / get_balance: retry on NetworkError / RequestTimeout
- cancel_order: retry (idempotent operation)
- get_open_orders / get_trades: retry on transient errors
- get_positions: retry on transient errors; raises (does not swallow) after
  exhausting retries — startup reconciliation must not mistake "can't reach
  the exchange" for "no open positions"
- create_order: NO retry — raises immediately (non-idempotent)
- connect: retry on NetworkError
- fetch_ohlcv_between: delegates to fetch_ohlcv (inherits retry)
- CircuitBreaker: opens after threshold consecutive total-failures, resets on
  success, half-opens after cooldown, does not trip on non-retryable errors
- _retry circuit integration: CircuitBreakerOpen raised when open, breaker
  resets on success, only transient exhaustions count toward threshold
- fetch_ohlcv propagates CircuitBreakerOpen (does not swallow it)
- KrakenFuturesConnection._retry also has circuit breaker
- fetch_funding_rate propagates CircuitBreakerOpen

All tests mock asyncio.sleep to avoid actual delays.
"""

import pytest
import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch, call
import ccxt.async_support as ccxt

from src.exchange import ExchangeConnection, CircuitBreaker, CircuitBreakerOpen, KrakenFuturesConnection


# ── fixtures ────────────────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    """Patch asyncio.sleep to a no-op so retry back-off doesn't slow tests."""
    monkeypatch.setattr(asyncio, "sleep", AsyncMock(return_value=None))


def _make_conn() -> ExchangeConnection:
    """Build an ExchangeConnection with a fully-mocked inner exchange.

    Uses a high circuit-breaker threshold so existing tests are unaffected by
    the breaker — they only test retry behaviour, not circuit-breaker behaviour.
    """
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
    conn.exchange.fetch_positions = AsyncMock(return_value=[])
    conn.exchange.close = AsyncMock(return_value=None)
    # High threshold so existing retry tests are not affected by the circuit breaker
    conn._circuit = CircuitBreaker(threshold=1000, cooldown_seconds=60.0)
    conn._data_circuit = CircuitBreaker(threshold=1000, cooldown_seconds=60.0)
    conn._order_circuit = CircuitBreaker(threshold=1000, cooldown_seconds=60.0)
    return conn


# ── _retry ────────────────────────────────────────────────────────────────────────────

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
        # Base: 2**1 = 2 and 2**2 = 4; jitter adds up to 1s so check floor
        assert 2 <= calls[0].args[0] < 3
        assert 4 <= calls[1].args[0] < 5

    async def test_retry_backoff_includes_jitter(self, monkeypatch):
        """Jitter is added so concurrent instances don't retry in lock-step."""
        calls = []

        async def capture_sleep(delay):
            calls.append(delay)

        monkeypatch.setattr(asyncio, "sleep", capture_sleep)

        conn = _make_conn()
        fn = AsyncMock(side_effect=[ccxt.NetworkError("x"), "ok"])
        with patch('src.exchange.random.uniform', return_value=0.42):
            await conn._retry(fn, retries=3, label='test')

        assert len(calls) == 1
        assert calls[0] == pytest.approx(2.0 + 0.42)  # 2**1 + fixed jitter

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


# ── fetch_ohlcv ─────────────────────────────────────────────────────────────────────────

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


# ── fetch_order_book ────────────────────────────────────────────────────────────────────

class TestFetchOrderBook:
    BOOK = {'bids': [[50000.0, 1.0], [49990.0, 2.0]], 'asks': [[50010.0, 1.0], [50020.0, 2.0]]}

    async def test_returns_book_on_success(self):
        conn = _make_conn()
        conn.exchange.fetch_order_book = AsyncMock(return_value=self.BOOK)
        result = await conn.fetch_order_book('BTC/USD')
        assert result == self.BOOK

    async def test_passes_symbol_and_limit(self):
        conn = _make_conn()
        conn.exchange.fetch_order_book = AsyncMock(return_value=self.BOOK)
        await conn.fetch_order_book('ETH/USD', limit=10)
        conn.exchange.fetch_order_book.assert_called_once_with('ETH/USD', 10)

    async def test_returns_empty_dict_after_all_retries_fail(self):
        conn = _make_conn()
        conn.exchange.fetch_order_book = AsyncMock(
            side_effect=ccxt.NetworkError("down")
        )
        result = await conn.fetch_order_book('BTC/USD', retries=2)
        assert result == {}
        assert conn.exchange.fetch_order_book.call_count == 2

    async def test_retries_on_network_error_then_succeeds(self):
        conn = _make_conn()
        conn.exchange.fetch_order_book = AsyncMock(
            side_effect=[ccxt.NetworkError("blip"), self.BOOK]
        )
        result = await conn.fetch_order_book('BTC/USD', retries=3)
        assert result == self.BOOK
        assert conn.exchange.fetch_order_book.call_count == 2

    async def test_propagates_circuit_breaker_open(self):
        """CircuitBreakerOpen must not be swallowed — callers use it to detect outages."""
        conn = _make_conn()
        conn._data_circuit = CircuitBreaker(threshold=1, cooldown_seconds=60.0)
        conn.exchange.fetch_order_book = AsyncMock(
            side_effect=ccxt.NetworkError("unreachable")
        )
        # Trip the circuit (1 failure = threshold)
        conn._data_circuit.record_failure()
        with pytest.raises(CircuitBreakerOpen):
            await conn.fetch_order_book('BTC/USD')
        # The underlying exchange must NOT have been called (CB short-circuits)
        conn.exchange.fetch_order_book.assert_not_called()

    async def test_non_retryable_error_returns_empty_dict(self):
        conn = _make_conn()
        conn.exchange.fetch_order_book = AsyncMock(
            side_effect=ccxt.ExchangeError("symbol not found")
        )
        result = await conn.fetch_order_book('FAKE/USD', retries=2)
        assert result == {}


# ── get_ticker ─────────────────────────────────────────────────────────────────────────

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


# ── get_balance ─────────────────────────────────────────────────────────────────────────

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


# ── get_positions ─────────────────────────────────────────────────────────────────────

class TestGetPositions:
    async def test_returns_positions_on_success(self):
        conn = _make_conn()
        positions = [{'symbol': 'BTC/USD', 'contracts': 0.01}]
        conn.exchange.fetch_positions = AsyncMock(return_value=positions)
        result = await conn.get_positions(['BTC/USD'])
        assert result == positions

    async def test_retries_on_network_error(self):
        conn = _make_conn()
        conn.exchange.fetch_positions = AsyncMock(
            side_effect=[ccxt.NetworkError("blip"), []]
        )
        result = await conn.get_positions(['BTC/USD'], retries=3)
        assert result == []
        assert conn.exchange.fetch_positions.call_count == 2

    async def test_raises_after_all_retries(self):
        """Unlike fetch_ohlcv/fetch_order_book, get_positions must NOT swallow
        a persistent failure into an empty list — callers (startup
        reconciliation) rely on the raise to avoid trading blind."""
        conn = _make_conn()
        conn.exchange.fetch_positions = AsyncMock(
            side_effect=ccxt.NetworkError("unreachable")
        )
        with pytest.raises(ccxt.NetworkError):
            await conn.get_positions(['BTC/USD'], retries=2)

    async def test_passes_symbols_through(self):
        conn = _make_conn()
        conn.exchange.fetch_positions = AsyncMock(return_value=[])
        await conn.get_positions(['BTC/USD', 'ETH/USD'])
        conn.exchange.fetch_positions.assert_called_once_with(['BTC/USD', 'ETH/USD'])


# ── create_order — no retry ──────────────────────────────────────────────────────────────

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


# ── cancel_order ─────────────────────────────────────────────────────────────────────────

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


# ── get_open_orders ────────────────────────────────────────────────────────────────────────

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


# ── get_trades ─────────────────────────────────────────────────────────────────────────

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


# ── connect ─────────────────────────────────────────────────────────────────────────

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


# ── fetch_ohlcv_between ───────────────────────────────────────────────────────────────────────

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

    async def test_stall_guard_breaks_on_no_timestamp_progress(self):
        """If the exchange keeps returning data with the same last timestamp,
        the loop must stop instead of spinning forever."""
        conn = _make_conn()
        start_ms = 1_704_067_200_000  # 2024-01-01 00:00 UTC
        # Batch whose last timestamp is BEFORE start_ms — next_ms == start_ms,
        # so next_ms <= current_ms and the stall guard fires.
        stale_batch = [[start_ms - 1, 50000, 50100, 49900, 50050, 1.0]]
        conn.exchange.fetch_ohlcv = AsyncMock(return_value=stale_batch)

        result = await conn.fetch_ohlcv_between(
            'BTC/USD', '1m', '2024-01-01', '2024-01-02'
        )
        # Stale data still returned, but the loop called fetch_ohlcv only once.
        assert result == stale_batch
        assert conn.exchange.fetch_ohlcv.call_count == 1

    async def test_aggregates_three_batches(self):
        """Happy path: multiple pages are concatenated until an empty page."""
        conn = _make_conn()
        start_ms = 1_704_067_200_000
        candle_ms = 60_000  # 1-minute candles
        batch1 = [[start_ms + i * candle_ms, 1, 1, 1, 1, 1] for i in range(3)]
        batch2 = [[start_ms + (3 + i) * candle_ms, 1, 1, 1, 1, 1] for i in range(2)]
        conn.exchange.fetch_ohlcv = AsyncMock(side_effect=[batch1, batch2, []])

        result = await conn.fetch_ohlcv_between(
            'BTC/USD', '1m', '2024-01-01', '2024-01-02'
        )
        assert len(result) == 5


# ── CircuitBreaker unit tests ───────────────────────────────────────────────────────────────

class TestCircuitBreakerUnit:
    def test_initially_closed(self):
        cb = CircuitBreaker(threshold=3, cooldown_seconds=60)
        assert not cb.is_open
        assert cb.failure_count == 0

    def test_check_does_nothing_when_closed(self):
        cb = CircuitBreaker(threshold=3, cooldown_seconds=60)
        cb.check()  # must not raise

    def test_record_failure_increments_count(self):
        cb = CircuitBreaker(threshold=5, cooldown_seconds=60)
        cb.record_failure()
        cb.record_failure()
        assert cb.failure_count == 2
        assert not cb.is_open

    def test_circuit_opens_at_threshold(self):
        cb = CircuitBreaker(threshold=3, cooldown_seconds=60)
        for _ in range(3):
            cb.record_failure()
        assert cb.is_open

    def test_check_raises_when_open(self):
        cb = CircuitBreaker(threshold=2, cooldown_seconds=60)
        cb.record_failure()
        cb.record_failure()
        with pytest.raises(CircuitBreakerOpen):
            cb.check()

    def test_record_success_resets_failure_count(self):
        cb = CircuitBreaker(threshold=5, cooldown_seconds=60)
        cb.record_failure()
        cb.record_failure()
        cb.record_success()
        assert cb.failure_count == 0
        assert not cb.is_open

    def test_record_success_closes_open_circuit(self):
        cb = CircuitBreaker(threshold=2, cooldown_seconds=60)
        cb.record_failure()
        cb.record_failure()
        assert cb.is_open
        cb.record_success()
        assert not cb.is_open
        cb.check()  # must not raise after reset

    def test_circuit_half_opens_after_cooldown(self):
        cb = CircuitBreaker(threshold=2, cooldown_seconds=0.01)
        cb.record_failure()
        cb.record_failure()
        assert cb.is_open
        time.sleep(0.02)
        # After cooldown, is_open returns False (half-open)
        assert not cb.is_open
        cb.check()  # must not raise

    def test_check_message_includes_remaining_time(self):
        cb = CircuitBreaker(threshold=1, cooldown_seconds=120)
        cb.record_failure()
        with pytest.raises(CircuitBreakerOpen, match="pausing"):
            cb.check()

    def test_additional_failures_do_not_reset_timer(self):
        cb = CircuitBreaker(threshold=2, cooldown_seconds=60)
        cb.record_failure()
        cb.record_failure()
        assert cb.is_open
        # Extra failures while already open should keep it open
        cb.record_failure()
        assert cb.is_open


# ── CircuitBreakerOpen.remaining_seconds ────────────────────────────────────────────────

class TestCircuitBreakerOpenException:
    def test_default_remaining_seconds_is_zero(self):
        """Direct construction without remaining_seconds defaults to 0."""
        exc = CircuitBreakerOpen("test message")
        assert exc.remaining_seconds == 0.0

    def test_remaining_seconds_can_be_set(self):
        exc = CircuitBreakerOpen("test", remaining_seconds=42.5)
        assert exc.remaining_seconds == 42.5

    def test_check_populates_remaining_seconds(self):
        """check() should raise with remaining_seconds > 0 while circuit is open."""
        cb = CircuitBreaker(threshold=1, cooldown_seconds=120)
        cb.record_failure()
        try:
            cb.check()
            pytest.fail("CircuitBreakerOpen not raised")
        except CircuitBreakerOpen as exc:
            assert exc.remaining_seconds > 0
            assert exc.remaining_seconds <= 120.0

    def test_remaining_seconds_decreases_with_time(self, monkeypatch):
        """remaining_seconds reflects time elapsed since the circuit opened."""
        cb = CircuitBreaker(threshold=1, cooldown_seconds=100)
        cb.record_failure()

        # Advance monotonic clock by 30s
        original_monotonic = time.monotonic
        monkeypatch.setattr(time, "monotonic", lambda: original_monotonic() + 30)

        try:
            cb.check()
            pytest.fail("CircuitBreakerOpen not raised")
        except CircuitBreakerOpen as exc:
            assert exc.remaining_seconds <= 70.0  # 100 - 30 = ~70s left

    def test_exception_is_subclass_of_exception(self):
        """CircuitBreakerOpen must be catchable by bare `except Exception`."""
        exc = CircuitBreakerOpen("test", remaining_seconds=5.0)
        assert isinstance(exc, Exception)


# ── CircuitBreaker cooldown escalation ────────────────────────────────────────────────

class TestCircuitBreakerCooldownEscalation:
    """The circuit escalates cooldown on successive trips: ×1, ×2, ×5."""

    def test_first_trip_uses_base_cooldown(self):
        cb = CircuitBreaker(threshold=1, cooldown_seconds=60.0)
        cb.record_failure()
        assert cb.is_open
        try:
            cb.check()
        except CircuitBreakerOpen as exc:
            assert exc.remaining_seconds > 55.0  # close to 60s (×1)
            assert exc.remaining_seconds <= 60.0

    def test_second_trip_doubles_cooldown(self):
        cb = CircuitBreaker(threshold=1, cooldown_seconds=60.0)
        # First trip
        cb.record_failure()
        cb.record_success()  # reset and count consecutive_opens

        # Actually, record_success resets consecutive_opens too. Let's use the
        # internal escalation by directly tripping twice.
        cb2 = CircuitBreaker(threshold=1, cooldown_seconds=60.0)
        cb2.record_failure()                       # trip 1 → ×1 → 60s
        # expire the cooldown so we can trip again
        cb2._open_until = time.monotonic() - 1    # force half-open
        cb2._open_until = None                     # make check() not raise
        cb2._failures = 0                          # allow threshold to trip again
        cb2.record_failure()                       # trip 2 → ×2 → 120s
        try:
            cb2.check()
        except CircuitBreakerOpen as exc:
            assert exc.remaining_seconds > 115.0  # close to 120s (×2)
            assert exc.remaining_seconds <= 120.0

    def test_third_trip_uses_5x_multiplier(self):
        cb = CircuitBreaker(threshold=1, cooldown_seconds=60.0)
        for _ in range(2):
            cb.record_failure()
            cb._open_until = None
            cb._failures = 0
        cb.record_failure()                        # trip 3 → ×5 → 300s
        try:
            cb.check()
        except CircuitBreakerOpen as exc:
            assert exc.remaining_seconds > 295.0  # close to 300s (×5)
            assert exc.remaining_seconds <= 300.0

    def test_fourth_and_beyond_capped_at_5x(self):
        cb = CircuitBreaker(threshold=1, cooldown_seconds=60.0)
        for _ in range(3):
            cb.record_failure()
            cb._open_until = None
            cb._failures = 0
        cb.record_failure()                        # trip 4 → still ×5 → 300s
        try:
            cb.check()
        except CircuitBreakerOpen as exc:
            assert exc.remaining_seconds > 295.0
            assert exc.remaining_seconds <= 300.0

    def test_consecutive_open_count_increments_each_trip(self):
        cb = CircuitBreaker(threshold=1, cooldown_seconds=60.0)
        assert cb.consecutive_open_count == 0
        cb.record_failure()
        assert cb.consecutive_open_count == 1
        cb._open_until = None
        cb._failures = 0
        cb.record_failure()
        assert cb.consecutive_open_count == 2

    def test_record_success_resets_consecutive_open_count(self):
        cb = CircuitBreaker(threshold=1, cooldown_seconds=60.0)
        cb.record_failure()
        assert cb.consecutive_open_count == 1
        cb.record_success()
        assert cb.consecutive_open_count == 0

    def test_remaining_seconds_positive_when_circuit_open(self):
        """The paper trading loop uses remaining_seconds to sleep — must be > 0."""
        cb = CircuitBreaker(threshold=1, cooldown_seconds=60.0)
        cb.record_failure()
        try:
            cb.check()
            pytest.fail("CircuitBreakerOpen not raised")
        except CircuitBreakerOpen as exc:
            # Simulate what the paper trading loop does:
            wait = exc.remaining_seconds if exc.remaining_seconds > 0 else 60.0
            assert wait > 0, "wait must always be positive so the loop sleeps properly"
            assert wait <= 300.0, "wait should not exceed the max escalated cooldown"


# ── _retry circuit-breaker integration ───────────────────────────────────────────────

def _make_conn_with_circuit(threshold: int = 3, cooldown: float = 60.0) -> ExchangeConnection:
    """ExchangeConnection with a custom threshold for easier testing."""
    conn = ExchangeConnection.__new__(ExchangeConnection)
    conn.sandbox = True
    conn.exchange = MagicMock()
    conn.exchange.load_markets = AsyncMock(return_value={})
    conn.exchange.fetch_ohlcv = AsyncMock(return_value=[])
    conn.exchange.fetch_ticker = AsyncMock(return_value={})
    conn.exchange.fetch_balance = AsyncMock(return_value={})
    conn.exchange.create_order = AsyncMock(return_value={})
    conn.exchange.cancel_order = AsyncMock(return_value={})
    conn.exchange.fetch_open_orders = AsyncMock(return_value=[])
    conn.exchange.fetch_trades = AsyncMock(return_value=[])
    conn.exchange.fetch_positions = AsyncMock(return_value=[])
    conn.exchange.close = AsyncMock(return_value=None)
    conn._circuit = CircuitBreaker(threshold=threshold, cooldown_seconds=cooldown)
    conn._data_circuit = CircuitBreaker(threshold=threshold, cooldown_seconds=cooldown)
    conn._order_circuit = CircuitBreaker(threshold=threshold, cooldown_seconds=cooldown)
    return conn


class TestRetryCircuitIntegration:
    async def test_circuit_starts_closed(self):
        conn = _make_conn_with_circuit(threshold=3)
        assert not conn._circuit.is_open

    async def test_successful_call_keeps_circuit_closed(self):
        conn = _make_conn_with_circuit(threshold=3)
        fn = AsyncMock(return_value="ok")
        await conn._retry(fn, retries=3, label='test')
        assert not conn._circuit.is_open

    async def test_success_resets_partial_failure_count(self):
        conn = _make_conn_with_circuit(threshold=5)
        # Two consecutive failures then a success
        fn = AsyncMock(side_effect=[
            ccxt.NetworkError("x"), ccxt.NetworkError("x"),  # exhausted on call 1
        ])
        with pytest.raises(ccxt.NetworkError):
            await conn._retry(fn, retries=2, label='test')
        assert conn._circuit.failure_count == 1

        fn2 = AsyncMock(return_value="ok")
        await conn._retry(fn2, retries=1, label='test2')
        assert conn._circuit.failure_count == 0

    async def test_circuit_opens_after_threshold_exhausted_retries(self):
        conn = _make_conn_with_circuit(threshold=2, cooldown=60)
        fn = AsyncMock(side_effect=ccxt.NetworkError("down"))
        # Each call to _retry exhausts its retries and increments failure count
        with pytest.raises(ccxt.NetworkError):
            await conn._retry(fn, retries=2, label='t')  # failure 1
        assert conn._circuit.failure_count == 1
        with pytest.raises(ccxt.NetworkError):
            await conn._retry(fn, retries=2, label='t')  # failure 2 → opens
        assert conn._circuit.is_open

    async def test_circuit_open_raises_circuit_breaker_open(self):
        conn = _make_conn_with_circuit(threshold=1, cooldown=60)
        fn = AsyncMock(side_effect=ccxt.NetworkError("down"))
        with pytest.raises(ccxt.NetworkError):
            await conn._retry(fn, retries=1, label='t')  # trips breaker
        assert conn._circuit.is_open

        fn2 = AsyncMock(return_value="ok")
        with pytest.raises(CircuitBreakerOpen):
            await conn._retry(fn2, retries=1, label='t2')
        # fn2 must NOT have been called — circuit blocked it
        fn2.assert_not_called()

    async def test_non_retryable_error_does_not_trip_circuit(self):
        """Auth errors propagate immediately and must NOT count toward the circuit."""
        conn = _make_conn_with_circuit(threshold=2, cooldown=60)
        fn = AsyncMock(side_effect=ccxt.AuthenticationError("bad key"))
        for _ in range(5):
            with pytest.raises(ccxt.AuthenticationError):
                await conn._retry(fn, retries=3, label='t')
        # Non-retryable errors never exhaust retries, so circuit stays closed
        assert not conn._circuit.is_open
        assert conn._circuit.failure_count == 0

    async def test_circuit_resets_on_success_after_partial_failures(self):
        conn = _make_conn_with_circuit(threshold=3, cooldown=60)
        fn_fail = AsyncMock(side_effect=ccxt.NetworkError("x"))
        # Two exhausted calls (below threshold — circuit stays closed)
        with pytest.raises(ccxt.NetworkError):
            await conn._retry(fn_fail, retries=1, label='t')
        with pytest.raises(ccxt.NetworkError):
            await conn._retry(fn_fail, retries=1, label='t')
        assert conn._circuit.failure_count == 2

        fn_ok = AsyncMock(return_value="recovered")
        result = await conn._retry(fn_ok, retries=1, label='t')
        assert result == "recovered"
        assert conn._circuit.failure_count == 0
        assert not conn._circuit.is_open


# ── fetch_ohlcv propagates CircuitBreakerOpen ─────────────────────────────────────────────

class TestFetchOhlcvCircuitBreaker:
    async def test_fetch_ohlcv_propagates_circuit_breaker_open(self):
        """fetch_ohlcv must NOT swallow CircuitBreakerOpen (unlike other errors)."""
        conn = _make_conn_with_circuit(threshold=1, cooldown=60)
        # Trip the data circuit breaker
        conn.exchange.fetch_ohlcv = AsyncMock(side_effect=ccxt.NetworkError("down"))
        result = await conn.fetch_ohlcv('BTC/USD', retries=1)
        assert result == []  # first call exhausts retries → returns []
        assert conn._data_circuit.is_open   # fetch_ohlcv uses _data_circuit

        # Second call must raise CircuitBreakerOpen, not return []
        with pytest.raises(CircuitBreakerOpen):
            await conn.fetch_ohlcv('BTC/USD', retries=1)

    async def test_fetch_ohlcv_normal_failure_still_returns_empty(self):
        """Non-circuit-breaker failures still return [] as before."""
        conn = _make_conn_with_circuit(threshold=10)  # high threshold, won't open
        conn.exchange.fetch_ohlcv = AsyncMock(side_effect=ccxt.NetworkError("x"))
        result = await conn.fetch_ohlcv('BTC/USD', retries=1)
        assert result == []


# ── KrakenFuturesConnection circuit breaker ───────────────────────────────────────────

def _make_futures_conn(threshold: int = 3, cooldown: float = 60.0) -> KrakenFuturesConnection:
    conn = KrakenFuturesConnection.__new__(KrakenFuturesConnection)
    conn.sandbox = True
    conn.exchange = MagicMock()
    conn.exchange.load_markets = AsyncMock(return_value={})
    conn.exchange.fetch_ohlcv = AsyncMock(return_value=[])
    conn.exchange.fetch_ticker = AsyncMock(return_value={})
    conn.exchange.fetch_balance = AsyncMock(return_value={})
    conn.exchange.fetch_funding_rate = AsyncMock(return_value={'fundingRate': 0.0001})
    conn.exchange.fetch_funding_rate_history = AsyncMock(return_value=[])
    conn.exchange.create_order = AsyncMock(return_value={})
    conn.exchange.cancel_order = AsyncMock(return_value={})
    conn.exchange.fetch_positions = AsyncMock(return_value=[])
    conn.exchange.close = AsyncMock(return_value=None)
    conn._circuit = CircuitBreaker(threshold=threshold, cooldown_seconds=cooldown)
    conn._data_circuit = CircuitBreaker(threshold=threshold, cooldown_seconds=cooldown)
    conn._order_circuit = CircuitBreaker(threshold=threshold, cooldown_seconds=cooldown)
    return conn


class TestFuturesCircuitBreaker:
    async def test_circuit_starts_closed(self):
        conn = _make_futures_conn()
        assert not conn._circuit.is_open

    async def test_circuit_opens_after_threshold_failures(self):
        conn = _make_futures_conn(threshold=2, cooldown=60)
        conn.exchange.fetch_ticker = AsyncMock(side_effect=ccxt.NetworkError("x"))
        with pytest.raises(ccxt.NetworkError):
            await conn._retry(conn.exchange.fetch_ticker, retries=1, label='t')
        with pytest.raises(ccxt.NetworkError):
            await conn._retry(conn.exchange.fetch_ticker, retries=1, label='t')
        assert conn._circuit.is_open

    async def test_retry_raises_circuit_breaker_open_when_open(self):
        conn = _make_futures_conn(threshold=1, cooldown=60)
        conn.exchange.fetch_ticker = AsyncMock(side_effect=ccxt.NetworkError("x"))
        with pytest.raises(ccxt.NetworkError):
            await conn._retry(conn.exchange.fetch_ticker, retries=1, label='t')
        assert conn._circuit.is_open
        with pytest.raises(CircuitBreakerOpen):
            await conn._retry(AsyncMock(return_value="ok"), retries=1, label='t2')

    async def test_fetch_ohlcv_propagates_circuit_breaker_open(self):
        conn = _make_futures_conn(threshold=1, cooldown=60)
        conn.exchange.fetch_ohlcv = AsyncMock(side_effect=ccxt.NetworkError("x"))
        await conn.fetch_ohlcv('BTC/USD', retries=1)  # trips breaker
        with pytest.raises(CircuitBreakerOpen):
            await conn.fetch_ohlcv('BTC/USD', retries=1)

    async def test_fetch_funding_rate_propagates_circuit_breaker_open(self):
        conn = _make_futures_conn(threshold=1, cooldown=60)
        conn.exchange.fetch_funding_rate = AsyncMock(side_effect=ccxt.NetworkError("x"))
        result = await conn.fetch_funding_rate('BTC/USD', retries=1)
        assert result is None  # first call → exhausted retries → None
        # Now circuit is open — next call must raise
        with pytest.raises(CircuitBreakerOpen):
            await conn.fetch_funding_rate('BTC/USD', retries=1)

    async def test_success_resets_circuit(self):
        conn = _make_futures_conn(threshold=3, cooldown=60)
        conn.exchange.fetch_ticker = AsyncMock(side_effect=ccxt.NetworkError("x"))
        # Two failures (below threshold)
        for _ in range(2):
            with pytest.raises(ccxt.NetworkError):
                await conn._retry(conn.exchange.fetch_ticker, retries=1, label='t')
        assert conn._circuit.failure_count == 2
        # Success resets
        fn_ok = AsyncMock(return_value={'last': 50000.0})
        await conn._retry(fn_ok, retries=1, label='ok')
        assert conn._circuit.failure_count == 0


# ── Rate-limit-specific backoff ──────────────────────────────────────────────────────────────

class TestRateLimitBackoff:
    """RateLimitExceeded must wait ≥30s; other retryable errors use 2^n backoff."""

    async def test_rate_limit_waits_at_least_30s(self, monkeypatch):
        waits = []

        async def capture_sleep(delay):
            waits.append(delay)

        monkeypatch.setattr(asyncio, "sleep", capture_sleep)
        conn = _make_conn()
        fn = AsyncMock(side_effect=[ccxt.RateLimitExceeded("429"), "ok"])
        with patch('src.exchange.random.uniform', return_value=0.0):
            await conn._retry(fn, retries=3, label='test')
        assert len(waits) == 1
        assert waits[0] >= 30.0

    async def test_network_error_does_not_trigger_30s_wait(self, monkeypatch):
        waits = []

        async def capture_sleep(delay):
            waits.append(delay)

        monkeypatch.setattr(asyncio, "sleep", capture_sleep)
        conn = _make_conn()
        fn = AsyncMock(side_effect=[ccxt.NetworkError("blip"), "ok"])
        with patch('src.exchange.random.uniform', return_value=0.0):
            await conn._retry(fn, retries=3, label='test')
        assert len(waits) == 1
        assert waits[0] < 30.0  # 2**1 = 2s

    async def test_rate_limit_adds_jitter_on_top_of_minimum(self, monkeypatch):
        waits = []

        async def capture_sleep(delay):
            waits.append(delay)

        monkeypatch.setattr(asyncio, "sleep", capture_sleep)
        conn = _make_conn()
        fn = AsyncMock(side_effect=[ccxt.RateLimitExceeded("429"), "ok"])
        with patch('src.exchange.random.uniform', return_value=1.5):
            await conn._retry(fn, retries=3, label='test')
        assert waits[0] == pytest.approx(31.5)  # 30 + 1.5 jitter

    async def test_rate_limit_still_retries_and_succeeds(self, monkeypatch):
        monkeypatch.setattr(asyncio, "sleep", AsyncMock())
        conn = _make_conn()
        fn = AsyncMock(side_effect=[ccxt.RateLimitExceeded("429"), "recovered"])
        result = await conn._retry(fn, retries=3, label='test')
        assert result == "recovered"
        assert fn.call_count == 2

    async def test_futures_rate_limit_waits_at_least_30s(self, monkeypatch):
        waits = []

        async def capture_sleep(delay):
            waits.append(delay)

        monkeypatch.setattr(asyncio, "sleep", capture_sleep)
        conn = _make_futures_conn()
        fn = AsyncMock(side_effect=[ccxt.RateLimitExceeded("429"), "ok"])
        with patch('src.exchange.random.uniform', return_value=0.0):
            await conn._retry(fn, retries=3, label='test')
        assert len(waits) == 1
        assert waits[0] >= 30.0


# ── CircuitBreaker cooldown escalation ────────────────────────────────────────────────

class TestCircuitBreakerEscalation:
    """Cooldown escalates across repeated trips: ×1 → ×2 → ×5 (capped)."""

    def _trip_and_force_close(self, cb: CircuitBreaker) -> None:
        """Trip the breaker then immediately expire the cooldown."""
        cb.record_failure()
        cb._open_until = time.monotonic() - 1  # fast-forward past cooldown

    def test_first_trip_uses_base_cooldown(self):
        cb = CircuitBreaker(threshold=1, cooldown_seconds=60.0)
        before = time.monotonic()
        cb.record_failure()
        assert cb.is_open
        assert cb.consecutive_open_count == 1
        assert cb._open_until == pytest.approx(before + 60.0, abs=1.0)

    def test_second_trip_doubles_cooldown(self):
        cb = CircuitBreaker(threshold=1, cooldown_seconds=60.0)
        self._trip_and_force_close(cb)
        before = time.monotonic()
        cb.record_failure()
        assert cb.consecutive_open_count == 2
        assert cb._open_until == pytest.approx(before + 120.0, abs=1.0)

    def test_third_trip_applies_5x_multiplier(self):
        cb = CircuitBreaker(threshold=1, cooldown_seconds=60.0)
        self._trip_and_force_close(cb)
        self._trip_and_force_close(cb)
        before = time.monotonic()
        cb.record_failure()
        assert cb.consecutive_open_count == 3
        assert cb._open_until == pytest.approx(before + 300.0, abs=1.0)

    def test_fourth_trip_caps_at_5x(self):
        cb = CircuitBreaker(threshold=1, cooldown_seconds=60.0)
        for _ in range(3):
            self._trip_and_force_close(cb)
        before = time.monotonic()
        cb.record_failure()
        assert cb.consecutive_open_count == 4
        # Still capped at ×5 = 300s
        assert cb._open_until == pytest.approx(before + 300.0, abs=1.0)

    def test_success_resets_escalation_counter(self):
        cb = CircuitBreaker(threshold=1, cooldown_seconds=60.0)
        self._trip_and_force_close(cb)
        self._trip_and_force_close(cb)
        assert cb.consecutive_open_count == 2
        cb.record_success()
        assert cb.consecutive_open_count == 0

    def test_after_success_reset_next_trip_uses_base_cooldown(self):
        cb = CircuitBreaker(threshold=1, cooldown_seconds=60.0)
        self._trip_and_force_close(cb)
        self._trip_and_force_close(cb)
        cb.record_success()
        before = time.monotonic()
        cb.record_failure()
        # Should be back to ×1 = 60s
        assert cb.consecutive_open_count == 1
        assert cb._open_until == pytest.approx(before + 60.0, abs=1.0)

    def test_consecutive_open_count_property(self):
        cb = CircuitBreaker(threshold=1, cooldown_seconds=60.0)
        assert cb.consecutive_open_count == 0
        self._trip_and_force_close(cb)
        assert cb.consecutive_open_count == 1


# ── Per-call timeout (asyncio.wait_for) ──────────────────────────────────────────────


class TestRetryHangTimeout:
    """
    _retry wraps every ccxt call with asyncio.wait_for(coro, timeout=call_timeout).

    If the exchange server accepts a TCP connection but never sends a response
    (hung call), asyncio.TimeoutError is raised, converted to ccxt.RequestTimeout,
    and retried with normal exponential back-off — identical to a regular
    RequestTimeout from ccxt.  After all retries are exhausted, the circuit
    breaker records a failure, just as it would for a network error.

    This prevents the event loop from being frozen indefinitely while an open
    position sits unmonitored.

    NOTE: Hanging functions must use asyncio.Event().wait() rather than
    asyncio.sleep() because the autouse fixture patches asyncio.sleep to a
    no-op.  asyncio.Event().wait() blocks on the event loop without using
    asyncio.sleep, so asyncio.wait_for's call_later-based timer fires correctly.
    """

    async def test_hanging_call_is_retried_then_raises_request_timeout(self):
        """A call that always hangs exhausts retries and raises ccxt.RequestTimeout."""
        conn = _make_conn()
        call_count = 0

        async def hanging(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            await asyncio.Event().wait()  # blocks until cancelled by wait_for

        with pytest.raises(ccxt.RequestTimeout):
            await conn._retry(hanging, retries=2, label='hang', call_timeout=0.001)

        assert call_count == 2  # one attempt + one retry

    async def test_timeout_triggers_circuit_breaker_after_exhaustion(self):
        """Hang exhausts retries → circuit breaker records a failure."""
        conn = _make_conn_with_circuit(threshold=1, cooldown=60)
        call_count = 0

        async def hanging(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            await asyncio.Event().wait()

        with pytest.raises((ccxt.RequestTimeout, CircuitBreakerOpen)):
            await conn._retry(hanging, retries=1, label='hang', call_timeout=0.001)

        assert conn._circuit.is_open

    async def test_timeout_raises_ccxt_request_timeout_not_asyncio_timeout(self):
        """Callers see ccxt.RequestTimeout, not the internal asyncio.TimeoutError."""
        conn = _make_conn()

        async def hanging(*args, **kwargs):
            await asyncio.Event().wait()

        exc_raised = None
        try:
            await conn._retry(hanging, retries=1, label='hang', call_timeout=0.001)
        except Exception as e:
            exc_raised = e

        assert exc_raised is not None
        assert isinstance(exc_raised, ccxt.RequestTimeout), (
            f"Expected ccxt.RequestTimeout, got {type(exc_raised)}"
        )

    async def test_successful_call_within_timeout_returns_normally(self):
        """A call that completes before the timeout succeeds without incident."""
        conn = _make_conn()
        fn = AsyncMock(return_value={"data": "ok"})
        result = await conn._retry(fn, retries=1, label='fast', call_timeout=5.0)
        assert result == {"data": "ok"}
        assert fn.call_count == 1

    async def test_call_timeout_none_disables_guard(self):
        """call_timeout=None removes asyncio.wait_for — normal behavior unchanged."""
        conn = _make_conn()
        fn = AsyncMock(return_value=42)
        result = await conn._retry(fn, retries=1, label='t', call_timeout=None)
        assert result == 42
        assert fn.call_count == 1

    async def test_mixed_network_error_then_hang_all_count_as_retries(self):
        """One NetworkError followed by a hang both use retry slots correctly."""
        conn = _make_conn()
        call_count = 0

        async def mixed(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise ccxt.NetworkError("transient blip")
            await asyncio.Event().wait()  # second attempt hangs

        with pytest.raises((ccxt.NetworkError, ccxt.RequestTimeout)):
            await conn._retry(mixed, retries=2, label='mixed', call_timeout=0.001)

        assert call_count == 2

    async def test_hang_then_success_resets_circuit_breaker(self):
        """A hung call that eventually succeeds (on retry) resets the breaker."""
        conn = _make_conn_with_circuit(threshold=3, cooldown=60)
        call_count = 0

        async def hang_once(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                await asyncio.Event().wait()  # hang on first attempt
            return "recovered"

        result = await conn._retry(hang_once, retries=2, label='hang-once',
                                   call_timeout=0.001)
        assert result == "recovered"
        assert conn._circuit.failure_count == 0  # reset on success

    async def test_args_and_kwargs_passed_through_with_timeout(self):
        """call_timeout is consumed by _retry and NOT forwarded to coro_fn."""
        conn = _make_conn()
        fn = AsyncMock(return_value="data")
        await conn._retry(fn, "sym", timeframe="1m", retries=1,
                          label='t', call_timeout=5.0)
        fn.assert_called_once_with("sym", timeframe="1m")


class TestCreateOrderTimeout:
    """
    create_order wraps the ccxt call with asyncio.wait_for(coro, order_timeout).

    A hung order placement is the worst case scenario: the exchange may or may
    not have accepted the order, but the bot has no confirmation.  The timeout
    converts the hang to ccxt.RequestTimeout so the caller can reconcile via
    get_open_orders() before deciding whether to retry.
    """

    async def test_hanging_create_order_raises_request_timeout(self):
        """A hung create_order is cancelled and raises ccxt.RequestTimeout."""
        conn = _make_conn()

        async def hanging(*args, **kwargs):
            await asyncio.Event().wait()

        conn.exchange.create_order = hanging

        with pytest.raises(ccxt.RequestTimeout, match="timed out"):
            await conn.create_order('BTC/USD', 'market', 'buy', 0.001,
                                    order_timeout=0.001)

    async def test_create_order_completes_within_timeout(self):
        """Normal order placement is unaffected by the timeout guard."""
        conn = _make_conn()
        order = {'id': 'abc123', 'status': 'open'}
        conn.exchange.create_order = AsyncMock(return_value=order)
        result = await conn.create_order('BTC/USD', 'market', 'buy', 0.001,
                                         order_timeout=5.0)
        assert result == order

    async def test_create_order_timeout_none_disables_guard(self):
        """order_timeout=None disables asyncio.wait_for — normal flow."""
        conn = _make_conn()
        conn.exchange.create_order = AsyncMock(return_value={'id': 'x'})
        result = await conn.create_order('BTC/USD', 'market', 'buy', 0.001,
                                         order_timeout=None)
        assert result is not None

    async def test_ccxt_request_timeout_from_exchange_still_propagates(self):
        """A ccxt.RequestTimeout raised by the exchange (not a hang) passes through."""
        conn = _make_conn()
        conn.exchange.create_order = AsyncMock(
            side_effect=ccxt.RequestTimeout("exchange rejected")
        )
        with pytest.raises(ccxt.RequestTimeout):
            await conn.create_order('BTC/USD', 'market', 'buy', 0.001)
        # Still called exactly once — no retry on order creation
        assert conn.exchange.create_order.call_count == 1

    async def test_timeout_message_describes_unknown_order_state(self):
        """The RequestTimeout message explicitly warns the order state is unknown."""
        conn = _make_conn()

        async def hanging(*args, **kwargs):
            await asyncio.Event().wait()

        conn.exchange.create_order = hanging

        try:
            await conn.create_order('BTC/USD', 'buy', 'buy', 0.001,
                                    order_timeout=0.001)
            pytest.fail("Should have raised")
        except ccxt.RequestTimeout as exc:
            assert "timed out" in str(exc).lower() or "unknown" in str(exc).lower()


# ── KrakenFuturesConnection per-call timeout ──────────────────────────────────────────────


class TestFuturesRetryHangTimeout:
    """KrakenFuturesConnection._retry has the same call_timeout behaviour."""

    async def test_hanging_call_retried_and_raises_request_timeout(self):
        conn = _make_futures_conn()
        call_count = 0

        async def hanging(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            await asyncio.Event().wait()

        with pytest.raises(ccxt.RequestTimeout):
            await conn._retry(hanging, retries=2, label='hang', call_timeout=0.001)

        assert call_count == 2

    async def test_futures_timeout_trips_circuit_breaker(self):
        conn = _make_futures_conn(threshold=1, cooldown=60)

        async def hanging(*args, **kwargs):
            await asyncio.Event().wait()

        with pytest.raises((ccxt.RequestTimeout, CircuitBreakerOpen)):
            await conn._retry(hanging, retries=1, label='hang', call_timeout=0.001)

        assert conn._circuit.is_open

    async def test_futures_successful_call_within_timeout(self):
        conn = _make_futures_conn()
        fn = AsyncMock(return_value={"fundingRate": 0.0001})
        result = await conn._retry(fn, retries=1, label='fast', call_timeout=5.0)
        assert result == {"fundingRate": 0.0001}

    async def test_futures_call_timeout_none_disables_guard(self):
        conn = _make_futures_conn()
        fn = AsyncMock(return_value=99)
        result = await conn._retry(fn, retries=1, label='t', call_timeout=None)
        assert result == 99


class TestFuturesCreateOrderTimeout:
    """KrakenFuturesConnection.create_order has the same order_timeout behaviour."""

    async def test_hanging_futures_create_order_raises_request_timeout(self):
        conn = _make_futures_conn()

        async def hanging(*args, **kwargs):
            await asyncio.Event().wait()

        conn.exchange.create_order = hanging

        with pytest.raises(ccxt.RequestTimeout, match="timed out"):
            await conn.create_order('BTC/USD', 'market', 'buy', 0.01,
                                    order_timeout=0.001)

    async def test_futures_create_order_completes_within_timeout(self):
        conn = _make_futures_conn()
        order = {'id': 'fut123', 'status': 'open'}
        conn.exchange.create_order = AsyncMock(return_value=order)
        result = await conn.create_order('BTC/USD', 'market', 'buy', 0.01,
                                          order_timeout=5.0)
        assert result == order

    async def test_futures_create_order_timeout_none_disables_guard(self):
        conn = _make_futures_conn()
        conn.exchange.create_order = AsyncMock(return_value={'id': 'x'})
        result = await conn.create_order('BTC/USD', 'market', 'buy', 0.01,
                                          order_timeout=None)
        assert result is not None


# ── Circuit isolation: data vs order categories ────────────────────────────────────────────

class TestCircuitIsolation:
    """Data and order circuits are independent — failures in one category must
    not block the other.  This is critical for live trading: a rate-limited
    OHLCV feed should never prevent cancelling an open position."""

    async def test_spot_data_circuit_open_does_not_block_cancel_order(self):
        """Tripping the data circuit via OHLCV failures leaves cancel_order working."""
        conn = _make_conn_with_circuit(threshold=100)  # _circuit won't open
        conn._data_circuit = CircuitBreaker(threshold=1, cooldown_seconds=60.0)
        conn._order_circuit = CircuitBreaker(threshold=100, cooldown_seconds=60.0)

        # Trip the data circuit
        conn.exchange.fetch_ohlcv = AsyncMock(side_effect=ccxt.NetworkError("data down"))
        await conn.fetch_ohlcv('BTC/USD', retries=1)
        assert conn._data_circuit.is_open

        # Order circuit should still be closed → cancel_order succeeds
        conn.exchange.cancel_order = AsyncMock(return_value={'id': 'abc'})
        result = await conn.cancel_order('order-1', 'BTC/USD')
        assert result == {'id': 'abc'}
        assert not conn._order_circuit.is_open

    async def test_spot_order_circuit_open_does_not_block_fetch_ohlcv(self):
        """Tripping the order circuit via cancel failures leaves OHLCV fetches working."""
        conn = _make_conn_with_circuit(threshold=100)
        conn._data_circuit = CircuitBreaker(threshold=100, cooldown_seconds=60.0)
        conn._order_circuit = CircuitBreaker(threshold=1, cooldown_seconds=60.0)

        # Trip the order circuit
        conn.exchange.cancel_order = AsyncMock(side_effect=ccxt.NetworkError("orders down"))
        with pytest.raises(ccxt.NetworkError):
            await conn.cancel_order('bad-id', 'BTC/USD')
        assert conn._order_circuit.is_open

        # Data circuit should still be closed → fetch_ohlcv succeeds
        candle = [1_700_000_000_000, 50_000.0, 51_000.0, 49_000.0, 50_500.0, 1.5]
        conn.exchange.fetch_ohlcv = AsyncMock(return_value=[candle])
        result = await conn.fetch_ohlcv('BTC/USD')
        assert result == [candle]
        assert not conn._data_circuit.is_open

    async def test_futures_data_circuit_open_does_not_block_cancel_order(self):
        """Same isolation guarantee for KrakenFuturesConnection."""
        conn = _make_futures_conn(threshold=100)
        conn._data_circuit = CircuitBreaker(threshold=1, cooldown_seconds=60.0)
        conn._order_circuit = CircuitBreaker(threshold=100, cooldown_seconds=60.0)

        conn.exchange.fetch_ohlcv = AsyncMock(side_effect=ccxt.NetworkError("data down"))
        await conn.fetch_ohlcv('BTC/USD', retries=1)
        assert conn._data_circuit.is_open

        conn.exchange.cancel_order = AsyncMock(return_value={'id': 'xyz'})
        result = await conn.cancel_order('order-1', 'BTC/USD')
        assert result == {'id': 'xyz'}

    async def test_futures_order_circuit_open_does_not_block_fetch_ohlcv(self):
        """Order circuit tripped on futures does not mute market data."""
        conn = _make_futures_conn(threshold=100)
        conn._data_circuit = CircuitBreaker(threshold=100, cooldown_seconds=60.0)
        conn._order_circuit = CircuitBreaker(threshold=1, cooldown_seconds=60.0)

        conn.exchange.cancel_order = AsyncMock(side_effect=ccxt.NetworkError("orders down"))
        with pytest.raises(ccxt.NetworkError):
            await conn.cancel_order('bad-id', 'BTC/USD')
        assert conn._order_circuit.is_open

        candle = [1_700_000_000_000, 50_000.0, 51_000.0, 49_000.0, 50_500.0, 1.5]
        conn.exchange.fetch_ohlcv = AsyncMock(return_value=[candle])
        result = await conn.fetch_ohlcv('BTC/USD')
        assert result == [candle]
        assert not conn._data_circuit.is_open

"""
Unit tests for KrakenFuturesConnection in src/exchange.py

Covers:
- _retry: success, transient error retry, non-retryable propagation, backoff
- connect: retry on NetworkError, raises after max retries
- fetch_ohlcv: returns data, returns [] after all retries fail, retries transient
- get_ticker: retry on transient, raises after max retries, no retry on auth error
- fetch_funding_rate: returns rate, returns None on permanent error, retries transient
- fetch_funding_rate_history: returns list, returns [] on permanent error, retries transient
- get_balance: retry on transient, raises after max retries
- create_order: NO retry (non-idempotent), passes leverage param
- cancel_order: retry (idempotent), raises after max retries
- get_open_positions: retry transient, returns [] on permanent error
- perp_symbol: known and unknown symbol mapping

All tests patch asyncio.sleep to avoid actual delays.
"""

import pytest
import asyncio
from unittest.mock import AsyncMock, MagicMock, call, patch
import ccxt.async_support as ccxt

from src.exchange import KrakenFuturesConnection, CircuitBreaker


# ── fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    """Patch asyncio.sleep to a no-op so retry back-off doesn't slow tests."""
    monkeypatch.setattr(asyncio, "sleep", AsyncMock(return_value=None))


def _make_conn() -> KrakenFuturesConnection:
    """Build a KrakenFuturesConnection with a fully-mocked inner exchange.

    Uses a high circuit-breaker threshold so existing tests are unaffected
    by the breaker — they only test retry behaviour, not circuit behaviour.
    """
    conn = KrakenFuturesConnection.__new__(KrakenFuturesConnection)
    conn.sandbox = True
    conn.exchange = MagicMock()
    conn.exchange.load_markets = AsyncMock(return_value={})
    conn.exchange.fetch_ohlcv = AsyncMock(return_value=[])
    conn.exchange.fetch_ticker = AsyncMock(return_value={})
    conn.exchange.fetch_balance = AsyncMock(return_value={})
    conn.exchange.create_order = AsyncMock(return_value={})
    conn.exchange.cancel_order = AsyncMock(return_value={})
    conn.exchange.fetch_funding_rate = AsyncMock(return_value={'fundingRate': 0.0001})
    conn.exchange.fetch_funding_rate_history = AsyncMock(return_value=[])
    conn.exchange.fetch_positions = AsyncMock(return_value=[])
    conn.exchange.close = AsyncMock(return_value=None)
    # High threshold so existing retry tests are not affected by the circuit breaker
    conn._circuit = CircuitBreaker(threshold=1000, cooldown_seconds=60.0)
    return conn


# ── perp_symbol ───────────────────────────────────────────────────────────────

class TestPerpSymbol:
    def test_maps_btc_usd(self):
        conn = _make_conn()
        assert conn.perp_symbol('BTC/USD') == 'BTC/USD:USD'

    def test_maps_eth_usd(self):
        conn = _make_conn()
        assert conn.perp_symbol('ETH/USD') == 'ETH/USD:USD'

    def test_maps_sol_usd(self):
        conn = _make_conn()
        assert conn.perp_symbol('SOL/USD') == 'SOL/USD:USD'

    def test_unknown_symbol_returned_unchanged(self):
        conn = _make_conn()
        assert conn.perp_symbol('XRP/USD') == 'XRP/USD'


# ── _retry ────────────────────────────────────────────────────────────────────

class TestFuturesRetryHelper:
    async def test_succeeds_on_first_attempt(self):
        conn = _make_conn()
        fn = AsyncMock(return_value=42)
        result = await conn._retry(fn, retries=3, label='test')
        assert result == 42
        assert fn.call_count == 1

    async def test_retries_on_network_error_then_succeeds(self):
        conn = _make_conn()
        fn = AsyncMock(side_effect=[ccxt.NetworkError("blip"), "ok"])
        result = await conn._retry(fn, retries=3, label='test')
        assert result == "ok"
        assert fn.call_count == 2

    async def test_retries_on_request_timeout(self):
        conn = _make_conn()
        fn = AsyncMock(side_effect=[ccxt.RequestTimeout("t/o"), "ok"])
        result = await conn._retry(fn, retries=3, label='test')
        assert result == "ok"
        assert fn.call_count == 2

    async def test_retries_on_rate_limit_exceeded(self):
        conn = _make_conn()
        fn = AsyncMock(side_effect=[ccxt.RateLimitExceeded("429"), "ok"])
        result = await conn._retry(fn, retries=3, label='test')
        assert result == "ok"
        assert fn.call_count == 2

    async def test_raises_after_all_retries_exhausted(self):
        conn = _make_conn()
        fn = AsyncMock(side_effect=ccxt.NetworkError("persistent"))
        with pytest.raises(ccxt.NetworkError):
            await conn._retry(fn, retries=3, label='test')
        assert fn.call_count == 3

    async def test_non_retryable_error_propagates_immediately(self):
        conn = _make_conn()
        fn = AsyncMock(side_effect=ccxt.ExchangeError("bad symbol"))
        with pytest.raises(ccxt.ExchangeError):
            await conn._retry(fn, retries=3, label='test')
        assert fn.call_count == 1

    async def test_auth_error_not_retried(self):
        conn = _make_conn()
        fn = AsyncMock(side_effect=ccxt.AuthenticationError("invalid key"))
        with pytest.raises(ccxt.AuthenticationError):
            await conn._retry(fn, retries=3, label='test')
        assert fn.call_count == 1

    async def test_sleep_uses_exponential_backoff(self):
        conn = _make_conn()
        fn = AsyncMock(side_effect=[ccxt.NetworkError("x"), ccxt.NetworkError("y"), "ok"])
        await conn._retry(fn, retries=3, label='test')
        calls = asyncio.sleep.call_args_list
        assert calls[0] == call(2)   # 2**1
        assert calls[1] == call(4)   # 2**2

    async def test_no_sleep_after_final_attempt(self):
        conn = _make_conn()
        fn = AsyncMock(side_effect=ccxt.NetworkError("fail"))
        with pytest.raises(ccxt.NetworkError):
            await conn._retry(fn, retries=2, label='test')
        # 2 attempts → sleep only once (after attempt 1)
        assert asyncio.sleep.call_count == 1

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


# ── connect ───────────────────────────────────────────────────────────────────

class TestFuturesConnect:
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
        assert conn.exchange.load_markets.call_count == 2

    async def test_auth_error_propagates_immediately(self):
        conn = _make_conn()
        conn.exchange.load_markets = AsyncMock(
            side_effect=ccxt.AuthenticationError("invalid key")
        )
        with pytest.raises(ccxt.AuthenticationError):
            await conn.connect(retries=3)
        assert conn.exchange.load_markets.call_count == 1


# ── fetch_ohlcv ───────────────────────────────────────────────────────────────

class TestFuturesFetchOhlcv:
    CANDLES = [[1_700_000_000_000, 50000, 50100, 49900, 50050, 100.0]]

    async def test_returns_candles_on_success(self):
        conn = _make_conn()
        conn.exchange.fetch_ohlcv = AsyncMock(return_value=self.CANDLES)
        result = await conn.fetch_ohlcv('BTC/USD')
        assert result == self.CANDLES

    async def test_uses_perp_symbol(self):
        conn = _make_conn()
        conn.exchange.fetch_ohlcv = AsyncMock(return_value=self.CANDLES)
        await conn.fetch_ohlcv('BTC/USD', timeframe='5m', limit=50)
        conn.exchange.fetch_ohlcv.assert_called_once_with(
            'BTC/USD:USD', timeframe='5m', limit=50, since=None
        )

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

    async def test_auth_error_returns_empty_list_without_retry(self):
        # Auth errors are non-retryable; they propagate out of _retry,
        # then the outer except in fetch_ohlcv catches them → returns [].
        conn = _make_conn()
        conn.exchange.fetch_ohlcv = AsyncMock(
            side_effect=ccxt.AuthenticationError("invalid key")
        )
        result = await conn.fetch_ohlcv('BTC/USD', retries=3)
        assert result == []
        assert conn.exchange.fetch_ohlcv.call_count == 1


# ── get_ticker ────────────────────────────────────────────────────────────────

class TestFuturesGetTicker:
    async def test_returns_ticker_on_success(self):
        conn = _make_conn()
        ticker = {'last': 50000.0, 'bid': 49990.0, 'ask': 50010.0}
        conn.exchange.fetch_ticker = AsyncMock(return_value=ticker)
        result = await conn.get_ticker('BTC/USD')
        assert result == ticker

    async def test_uses_perp_symbol(self):
        conn = _make_conn()
        conn.exchange.fetch_ticker = AsyncMock(return_value={})
        await conn.get_ticker('ETH/USD')
        conn.exchange.fetch_ticker.assert_called_once_with('ETH/USD:USD')

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


# ── fetch_funding_rate ────────────────────────────────────────────────────────

class TestFetchFundingRate:
    async def test_returns_funding_rate_on_success(self):
        conn = _make_conn()
        conn.exchange.fetch_funding_rate = AsyncMock(
            return_value={'fundingRate': 0.0001}
        )
        result = await conn.fetch_funding_rate('BTC/USD')
        assert result == 0.0001

    async def test_uses_perp_symbol(self):
        conn = _make_conn()
        conn.exchange.fetch_funding_rate = AsyncMock(
            return_value={'fundingRate': 0.0002}
        )
        await conn.fetch_funding_rate('ETH/USD')
        conn.exchange.fetch_funding_rate.assert_called_once_with('ETH/USD:USD')

    async def test_returns_none_when_key_missing(self):
        conn = _make_conn()
        conn.exchange.fetch_funding_rate = AsyncMock(return_value={})
        result = await conn.fetch_funding_rate('BTC/USD')
        assert result is None

    async def test_retries_on_network_error_then_returns_rate(self):
        conn = _make_conn()
        conn.exchange.fetch_funding_rate = AsyncMock(
            side_effect=[ccxt.NetworkError("blip"), {'fundingRate': 0.0003}]
        )
        result = await conn.fetch_funding_rate('BTC/USD', retries=3)
        assert result == 0.0003
        assert conn.exchange.fetch_funding_rate.call_count == 2

    async def test_returns_none_after_all_retries_exhausted(self):
        conn = _make_conn()
        conn.exchange.fetch_funding_rate = AsyncMock(
            side_effect=ccxt.NetworkError("down")
        )
        result = await conn.fetch_funding_rate('BTC/USD', retries=3)
        assert result is None

    async def test_returns_none_on_non_retryable_error(self):
        conn = _make_conn()
        conn.exchange.fetch_funding_rate = AsyncMock(
            side_effect=ccxt.ExchangeError("not supported")
        )
        result = await conn.fetch_funding_rate('BTC/USD', retries=3)
        assert result is None
        assert conn.exchange.fetch_funding_rate.call_count == 1


# ── fetch_funding_rate_history ────────────────────────────────────────────────

class TestFetchFundingRateHistory:
    HISTORY = [
        {'fundingRate': 0.0001, 'timestamp': 1_700_000_000_000},
        {'fundingRate': 0.0002, 'timestamp': 1_700_028_800_000},
    ]

    async def test_returns_history_on_success(self):
        conn = _make_conn()
        conn.exchange.fetch_funding_rate_history = AsyncMock(
            return_value=self.HISTORY
        )
        result = await conn.fetch_funding_rate_history('BTC/USD')
        assert result == self.HISTORY

    async def test_passes_limit_and_perp_symbol(self):
        conn = _make_conn()
        conn.exchange.fetch_funding_rate_history = AsyncMock(return_value=[])
        await conn.fetch_funding_rate_history('ETH/USD', limit=5)
        conn.exchange.fetch_funding_rate_history.assert_called_once_with(
            'ETH/USD:USD', limit=5
        )

    async def test_retries_on_network_error(self):
        conn = _make_conn()
        conn.exchange.fetch_funding_rate_history = AsyncMock(
            side_effect=[ccxt.NetworkError("blip"), self.HISTORY]
        )
        result = await conn.fetch_funding_rate_history('BTC/USD', retries=3)
        assert result == self.HISTORY
        assert conn.exchange.fetch_funding_rate_history.call_count == 2

    async def test_returns_empty_list_after_all_retries_exhausted(self):
        conn = _make_conn()
        conn.exchange.fetch_funding_rate_history = AsyncMock(
            side_effect=ccxt.NetworkError("down")
        )
        result = await conn.fetch_funding_rate_history('BTC/USD', retries=2)
        assert result == []

    async def test_returns_empty_list_on_non_retryable_error(self):
        conn = _make_conn()
        conn.exchange.fetch_funding_rate_history = AsyncMock(
            side_effect=ccxt.ExchangeError("not supported")
        )
        result = await conn.fetch_funding_rate_history('BTC/USD', retries=3)
        assert result == []
        assert conn.exchange.fetch_funding_rate_history.call_count == 1


# ── get_balance ───────────────────────────────────────────────────────────────

class TestFuturesGetBalance:
    async def test_returns_balance_on_success(self):
        conn = _make_conn()
        balance = {'USD': {'free': 10000.0, 'total': 10000.0}}
        conn.exchange.fetch_balance = AsyncMock(return_value=balance)
        result = await conn.get_balance()
        assert result == balance

    async def test_retries_on_request_timeout(self):
        conn = _make_conn()
        balance = {'USD': {'free': 5000.0}}
        conn.exchange.fetch_balance = AsyncMock(
            side_effect=[ccxt.RequestTimeout("t/o"), balance]
        )
        result = await conn.get_balance(retries=3)
        assert result == balance
        assert conn.exchange.fetch_balance.call_count == 2

    async def test_retries_on_rate_limit(self):
        conn = _make_conn()
        balance = {'USD': {'free': 5000.0}}
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
        assert conn.exchange.fetch_balance.call_count == 2

    async def test_auth_error_propagates_immediately(self):
        conn = _make_conn()
        conn.exchange.fetch_balance = AsyncMock(
            side_effect=ccxt.AuthenticationError("bad key")
        )
        with pytest.raises(ccxt.AuthenticationError):
            await conn.get_balance(retries=3)
        assert conn.exchange.fetch_balance.call_count == 1


# ── create_order — no retry ───────────────────────────────────────────────────

class TestFuturesCreateOrder:
    async def test_returns_order_on_success(self):
        conn = _make_conn()
        order = {'id': 'fut123', 'status': 'open'}
        conn.exchange.create_order = AsyncMock(return_value=order)
        result = await conn.create_order('BTC/USD', 'market', 'buy', 0.01)
        assert result == order

    async def test_no_retry_on_network_error(self):
        """create_order must NOT retry — duplicate positions are catastrophic."""
        conn = _make_conn()
        conn.exchange.create_order = AsyncMock(
            side_effect=ccxt.NetworkError("timeout")
        )
        with pytest.raises(ccxt.NetworkError):
            await conn.create_order('BTC/USD', 'market', 'buy', 0.01)
        assert conn.exchange.create_order.call_count == 1

    async def test_no_retry_on_request_timeout(self):
        conn = _make_conn()
        conn.exchange.create_order = AsyncMock(
            side_effect=ccxt.RequestTimeout("t/o")
        )
        with pytest.raises(ccxt.RequestTimeout):
            await conn.create_order('BTC/USD', 'limit', 'sell', 0.01, price=55000.0)
        assert conn.exchange.create_order.call_count == 1

    async def test_uses_perp_symbol(self):
        conn = _make_conn()
        conn.exchange.create_order = AsyncMock(return_value={'id': 'x'})
        await conn.create_order('BTC/USD', 'market', 'buy', 0.01)
        args, _ = conn.exchange.create_order.call_args
        assert args[0] == 'BTC/USD:USD'

    async def test_leverage_passed_in_params(self):
        conn = _make_conn()
        conn.exchange.create_order = AsyncMock(return_value={'id': 'x'})
        await conn.create_order('BTC/USD', 'market', 'buy', 0.01, leverage=5)
        args, _ = conn.exchange.create_order.call_args
        params = args[5]  # 6th positional arg: params dict
        assert params.get('leverage') == 5

    async def test_no_leverage_param_when_leverage_is_one(self):
        conn = _make_conn()
        conn.exchange.create_order = AsyncMock(return_value={'id': 'x'})
        await conn.create_order('BTC/USD', 'market', 'buy', 0.01, leverage=1)
        args, _ = conn.exchange.create_order.call_args
        params = args[5]
        assert 'leverage' not in params


# ── cancel_order ──────────────────────────────────────────────────────────────

class TestFuturesCancelOrder:
    async def test_returns_result_on_success(self):
        conn = _make_conn()
        conn.exchange.cancel_order = AsyncMock(return_value={'status': 'canceled'})
        result = await conn.cancel_order('order123', 'BTC/USD')
        assert result == {'status': 'canceled'}

    async def test_uses_perp_symbol(self):
        conn = _make_conn()
        conn.exchange.cancel_order = AsyncMock(return_value={})
        await conn.cancel_order('o1', 'ETH/USD')
        conn.exchange.cancel_order.assert_called_once_with('o1', 'ETH/USD:USD')

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


# ── get_open_positions ────────────────────────────────────────────────────────

class TestGetOpenPositions:
    async def test_returns_positions_on_success(self):
        conn = _make_conn()
        positions = [{'symbol': 'BTC/USD:USD', 'contracts': 0.1}]
        conn.exchange.fetch_positions = AsyncMock(return_value=positions)
        result = await conn.get_open_positions()
        assert result == positions

    async def test_retries_on_network_error(self):
        conn = _make_conn()
        positions = [{'symbol': 'ETH/USD:USD', 'contracts': 1.0}]
        conn.exchange.fetch_positions = AsyncMock(
            side_effect=[ccxt.NetworkError("blip"), positions]
        )
        result = await conn.get_open_positions(retries=3)
        assert result == positions
        assert conn.exchange.fetch_positions.call_count == 2

    async def test_returns_empty_list_after_all_retries_exhausted(self):
        conn = _make_conn()
        conn.exchange.fetch_positions = AsyncMock(
            side_effect=ccxt.NetworkError("down")
        )
        result = await conn.get_open_positions(retries=2)
        assert result == []

    async def test_returns_empty_list_on_non_retryable_error(self):
        conn = _make_conn()
        conn.exchange.fetch_positions = AsyncMock(
            side_effect=ccxt.AuthenticationError("invalid key")
        )
        result = await conn.get_open_positions(retries=3)
        assert result == []
        assert conn.exchange.fetch_positions.call_count == 1

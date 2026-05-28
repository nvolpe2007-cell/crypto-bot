"""
Unit tests for src/kraken_ws.py

Covers:
- _kraken_sign: return types, nonce injection, valid base64 signature
- _fetch_ws_token: success path, exchange error body, request exception,
  invalid base64 secret
- KrakenPublicWS._handle: ticker snapshot/update updates prices, missing
  'last' field ignored, confirmed candles queued, unconfirmed candles
  discarded, QueueFull handled silently, invalid JSON handled, unknown
  channels ignored
- KrakenPublicWS.get_price / get_prices: None before first update, copy
  isolation, multi-symbol
- KrakenPublicWS.start: reconnects after _connect() exception; stop() exits
  the loop; sleep is called between reconnect attempts
- KrakenPrivateWS._handle: trade executions appended, non-trade exec_types
  ignored, on_fill callback called, balance snapshots/updates stored,
  invalid JSON handled
- KrakenPrivateWS.pop_fills: returns fills and clears the list; idempotent
- KrakenPrivateWS.get_balance: default 0.0 for unseen currency
- KrakenPrivateWS.start: skips _connect when token fetch returns None
  (sleeps 30s); reconnects after _connect() exception; stop() exits
"""

import asyncio
import base64
import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch, call

import aiohttp

from src.kraken_ws import (
    _kraken_sign,
    _fetch_ws_token,
    KrakenPublicWS,
    KrakenPrivateWS,
    CandleClose,
    Execution,
    _RECONNECT_DELAY,
)


# ── helpers ───────────────────────────────────────────────────────────────────

def _aiohttp_session_mock(json_body: dict):
    """Build a layered aiohttp mock: ClientSession → post → resp.json()."""
    mock_resp = MagicMock()
    mock_resp.json = AsyncMock(return_value=json_body)

    # session.post(...) used as 'async with'
    post_cm = MagicMock()
    post_cm.__aenter__ = AsyncMock(return_value=mock_resp)
    post_cm.__aexit__ = AsyncMock(return_value=False)

    mock_session = MagicMock()
    mock_session.post = MagicMock(return_value=post_cm)

    # aiohttp.ClientSession() used as 'async with'
    session_cm = MagicMock()
    session_cm.__aenter__ = AsyncMock(return_value=mock_session)
    session_cm.__aexit__ = AsyncMock(return_value=False)

    return session_cm


# ── _kraken_sign ─────────────────────────────────────────────────────────────

class TestKrakenSign:
    _SECRET = "dGVzdHNlY3JldA=="  # base64("testsecret")

    def test_returns_two_strings(self):
        nonce, sig = _kraken_sign(self._SECRET, "/0/private/GetWebSocketsToken", {})
        assert isinstance(nonce, str)
        assert isinstance(sig, str)

    def test_nonce_is_numeric_string(self):
        nonce, _ = _kraken_sign(self._SECRET, "/path", {})
        assert nonce.isdigit()

    def test_nonce_injected_into_data_dict(self):
        data = {}
        nonce, _ = _kraken_sign(self._SECRET, "/path", data)
        assert data.get("nonce") == nonce

    def test_signature_is_valid_base64(self):
        _, sig = _kraken_sign(self._SECRET, "/path", {})
        decoded = base64.b64decode(sig)
        assert len(decoded) == 64  # SHA-512 output is 64 bytes

    def test_different_url_paths_produce_different_signatures(self):
        # Two consecutive calls on the same second might share a nonce,
        # but different url_paths must still produce different HMACs.
        with patch("src.kraken_ws.time.time", return_value=1_000_000.0):
            _, sig1 = _kraken_sign(self._SECRET, "/path/one", {})
        with patch("src.kraken_ws.time.time", return_value=1_000_000.0):
            _, sig2 = _kraken_sign(self._SECRET, "/path/two", {})
        assert sig1 != sig2

    def test_signature_non_empty(self):
        _, sig = _kraken_sign(self._SECRET, "/path", {})
        assert len(sig) > 0


# ── _fetch_ws_token ───────────────────────────────────────────────────────────

class TestFetchWsToken:
    _KEY    = "apikey"
    _SECRET = "dGVzdHNlY3JldA=="

    async def test_returns_token_on_success(self):
        cm = _aiohttp_session_mock({"result": {"token": "tok123"}, "error": []})
        with patch("src.kraken_ws.aiohttp.ClientSession", return_value=cm):
            token = await _fetch_ws_token(self._KEY, self._SECRET)
        assert token == "tok123"

    async def test_returns_none_when_error_field_present(self):
        cm = _aiohttp_session_mock({"error": ["EGeneral:Invalid arguments"], "result": {}})
        with patch("src.kraken_ws.aiohttp.ClientSession", return_value=cm):
            token = await _fetch_ws_token(self._KEY, self._SECRET)
        assert token is None

    async def test_returns_none_on_network_exception(self):
        # Use a plain Exception — conftest installs a minimal aiohttp stub that
        # does not include ClientError, so we avoid importing it directly.
        with patch("src.kraken_ws.aiohttp.ClientSession", side_effect=Exception("connection refused")):
            token = await _fetch_ws_token(self._KEY, self._SECRET)
        assert token is None

    async def test_invalid_base64_secret_propagates(self):
        # _kraken_sign is called *outside* _fetch_ws_token's try/except, so a
        # malformed API secret raises instead of returning None.  Document the
        # actual behaviour so a future fix (wrapping the sign call) is visible.
        with pytest.raises(Exception):
            await _fetch_ws_token(self._KEY, "NOT_VALID_BASE64!!!")

    async def test_returns_none_when_result_missing_token_key(self):
        cm = _aiohttp_session_mock({"result": {}, "error": []})
        with patch("src.kraken_ws.aiohttp.ClientSession", return_value=cm):
            token = await _fetch_ws_token(self._KEY, self._SECRET)
        assert token is None


# ── KrakenPublicWS._handle ────────────────────────────────────────────────────

class TestKrakenPublicWSHandle:
    def _ws(self):
        return KrakenPublicWS(["BTC/USD", "ETH/USD"])

    def _ticker_msg(self, symbol: str, last: float, msg_type: str = "update") -> str:
        return json.dumps({
            "channel": "ticker",
            "type": msg_type,
            "data": [{"symbol": symbol, "last": last}],
        })

    def _ohlc_msg(self, symbol: str, confirm: bool, close: float = 50_000.0) -> str:
        return json.dumps({
            "channel": "ohlc",
            "type": "update",
            "data": [{
                "symbol": symbol,
                "confirm": confirm,
                "open": close * 0.99,
                "high": close * 1.01,
                "low": close * 0.98,
                "close": close,
                "volume": 1.5,
                "timestamp": "2024-01-01T00:01:00Z",
                "interval": 1,
            }],
        })

    # ticker ──────────────────────────────────────────────────────────────────

    def test_ticker_snapshot_sets_price(self):
        ws = self._ws()
        ws._handle(self._ticker_msg("BTC/USD", 50_000.0, "snapshot"))
        assert ws.get_price("BTC/USD") == 50_000.0

    def test_ticker_update_sets_price(self):
        ws = self._ws()
        ws._handle(self._ticker_msg("BTC/USD", 51_000.0, "update"))
        assert ws.get_price("BTC/USD") == 51_000.0

    def test_ticker_updates_correct_symbol(self):
        ws = self._ws()
        ws._handle(self._ticker_msg("ETH/USD", 3_000.0))
        assert ws.get_price("ETH/USD") == 3_000.0
        assert ws.get_price("BTC/USD") is None

    def test_ticker_overwrites_stale_price(self):
        ws = self._ws()
        ws._handle(self._ticker_msg("BTC/USD", 50_000.0))
        ws._handle(self._ticker_msg("BTC/USD", 49_000.0))
        assert ws.get_price("BTC/USD") == 49_000.0

    def test_ticker_missing_last_field_leaves_price_unchanged(self):
        ws = self._ws()
        ws._prices["BTC/USD"] = 50_000.0
        msg = json.dumps({"channel": "ticker", "type": "update",
                          "data": [{"symbol": "BTC/USD"}]})  # no 'last'
        ws._handle(msg)
        assert ws.get_price("BTC/USD") == 50_000.0

    def test_ticker_missing_symbol_field_does_not_crash(self):
        ws = self._ws()
        msg = json.dumps({"channel": "ticker", "type": "update",
                          "data": [{"last": 50_000.0}]})  # no 'symbol'
        ws._handle(msg)  # should not raise

    # ohlc ────────────────────────────────────────────────────────────────────

    def test_confirmed_candle_is_queued(self):
        ws = self._ws()
        ws._handle(self._ohlc_msg("BTC/USD", confirm=True, close=50_000.0))
        assert not ws.candle_queue.empty()
        candle = ws.candle_queue.get_nowait()
        assert isinstance(candle, CandleClose)
        assert candle.symbol == "BTC/USD"
        assert candle.close == 50_000.0

    def test_unconfirmed_candle_is_not_queued(self):
        ws = self._ws()
        ws._handle(self._ohlc_msg("BTC/USD", confirm=False))
        assert ws.candle_queue.empty()

    def test_confirm_false_explicitly_not_queued(self):
        ws = self._ws()
        msg = json.dumps({
            "channel": "ohlc", "type": "update",
            "data": [{"symbol": "BTC/USD", "confirm": False,
                      "open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0,
                      "volume": 0.0, "timestamp": "", "interval": 1}],
        })
        ws._handle(msg)
        assert ws.candle_queue.empty()

    def test_queue_full_does_not_raise(self):
        ws = self._ws()
        ws.candle_queue = asyncio.Queue(maxsize=1)
        ws.candle_queue.put_nowait(None)  # fill the single slot
        ws._handle(self._ohlc_msg("BTC/USD", confirm=True))  # should not raise

    def test_multiple_confirmed_candles_all_queued(self):
        ws = self._ws()
        for price in [50_000.0, 51_000.0, 52_000.0]:
            ws._handle(self._ohlc_msg("BTC/USD", confirm=True, close=price))
        assert ws.candle_queue.qsize() == 3

    # error handling ──────────────────────────────────────────────────────────

    def test_invalid_json_does_not_raise(self):
        ws = self._ws()
        ws._handle("{not valid json{{}")  # should not raise

    def test_empty_string_does_not_raise(self):
        ws = self._ws()
        ws._handle("")  # should not raise

    def test_unknown_channel_does_not_crash(self):
        ws = self._ws()
        msg = json.dumps({"channel": "heartbeat", "type": "update", "data": []})
        ws._handle(msg)  # should not raise

    def test_missing_channel_does_not_crash(self):
        ws = self._ws()
        ws._handle(json.dumps({"type": "update"}))  # no 'channel' key


# ── KrakenPublicWS get_price / get_prices ─────────────────────────────────────

class TestKrakenPublicWSGetPrice:
    def test_get_price_returns_none_before_any_update(self):
        ws = KrakenPublicWS(["BTC/USD"])
        assert ws.get_price("BTC/USD") is None

    def test_get_price_for_unknown_symbol_returns_none(self):
        ws = KrakenPublicWS(["BTC/USD"])
        assert ws.get_price("DOGE/USD") is None

    def test_get_prices_returns_copy_not_reference(self):
        ws = KrakenPublicWS(["BTC/USD"])
        ws._prices["BTC/USD"] = 50_000.0
        snapshot = ws.get_prices()
        snapshot["BTC/USD"] = 99_999.0
        assert ws._prices["BTC/USD"] == 50_000.0  # internal state unchanged

    def test_get_prices_reflects_multiple_symbols(self):
        ws = KrakenPublicWS(["BTC/USD", "ETH/USD"])
        ws._prices["BTC/USD"] = 50_000.0
        ws._prices["ETH/USD"] = 3_000.0
        prices = ws.get_prices()
        assert prices["BTC/USD"] == 50_000.0
        assert prices["ETH/USD"] == 3_000.0


# ── KrakenPublicWS.start reconnection ─────────────────────────────────────────

class TestKrakenPublicWSReconnect:
    async def test_reconnects_after_connect_exception(self, monkeypatch):
        monkeypatch.setattr(asyncio, "sleep", AsyncMock())
        ws = KrakenPublicWS(["BTC/USD"])
        ws._running = True

        call_count = 0

        async def fake_connect():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise aiohttp.ClientError("simulated disconnect")
            ws._running = False  # stop after second call

        monkeypatch.setattr(ws, "_connect", fake_connect)
        await ws.start()
        assert call_count == 2

    async def test_stop_exits_loop(self, monkeypatch):
        monkeypatch.setattr(asyncio, "sleep", AsyncMock())
        ws = KrakenPublicWS(["BTC/USD"])

        async def fake_connect():
            ws.stop()

        monkeypatch.setattr(ws, "_connect", fake_connect)
        await ws.start()  # must not hang

    async def test_sleep_called_with_reconnect_delay_on_exception(self, monkeypatch):
        sleep_mock = AsyncMock()
        monkeypatch.setattr(asyncio, "sleep", sleep_mock)
        ws = KrakenPublicWS(["BTC/USD"])
        ws._running = True

        call_count = 0

        async def fake_connect():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise aiohttp.ClientError("disconnect")
            ws._running = False

        monkeypatch.setattr(ws, "_connect", fake_connect)
        await ws.start()
        # Only one sleep: after the first exception (_RECONNECT_DELAY)
        sleep_mock.assert_called_once_with(_RECONNECT_DELAY)

    async def test_no_sleep_when_stop_called_cleanly(self, monkeypatch):
        sleep_mock = AsyncMock()
        monkeypatch.setattr(asyncio, "sleep", sleep_mock)
        ws = KrakenPublicWS(["BTC/USD"])

        async def fake_connect():
            ws.stop()

        monkeypatch.setattr(ws, "_connect", fake_connect)
        await ws.start()
        sleep_mock.assert_not_called()


# ── KrakenPrivateWS._handle ───────────────────────────────────────────────────

class TestKrakenPrivateWSHandle:
    def _ws(self, on_fill=None):
        return KrakenPrivateWS("key", "c2VjcmV0", on_fill=on_fill)

    def _exec_msg(self, exec_type: str = "trade", symbol: str = "BTC/USD",
                  qty: float = 0.001, price: float = 50_000.0) -> str:
        return json.dumps({
            "channel": "executions",
            "type": "update",
            "data": [{
                "exec_type": exec_type,
                "order_id": "ORD-001",
                "symbol": symbol,
                "side": "buy",
                "last_qty": qty,
                "avg_price": price,
                "timestamp": "2024-01-01T00:00:00Z",
            }],
        })

    def _balance_msg(self, asset: str, balance: float, msg_type: str = "snapshot") -> str:
        return json.dumps({
            "channel": "balances",
            "type": msg_type,
            "data": [{"asset": asset, "balance": balance}],
        })

    # executions ──────────────────────────────────────────────────────────────

    def test_trade_execution_appended_to_fills(self):
        ws = self._ws()
        ws._handle(self._exec_msg(exec_type="trade", qty=0.001, price=50_000.0))
        assert len(ws._fills) == 1
        fill = ws._fills[0]
        assert isinstance(fill, Execution)
        assert fill.order_id == "ORD-001"
        assert fill.qty == 0.001
        assert fill.avg_price == 50_000.0

    def test_pending_execution_not_appended(self):
        ws = self._ws()
        ws._handle(self._exec_msg(exec_type="pending"))
        assert len(ws._fills) == 0

    def test_canceled_execution_not_appended(self):
        ws = self._ws()
        ws._handle(self._exec_msg(exec_type="canceled"))
        assert len(ws._fills) == 0

    def test_multiple_trades_in_one_message(self):
        ws = self._ws()
        msg = json.dumps({
            "channel": "executions",
            "type": "update",
            "data": [
                {"exec_type": "trade", "order_id": "A", "symbol": "BTC/USD",
                 "side": "buy", "last_qty": 0.01, "avg_price": 50_000.0,
                 "timestamp": "2024-01-01T00:00:00Z"},
                {"exec_type": "trade", "order_id": "B", "symbol": "ETH/USD",
                 "side": "sell", "last_qty": 0.5, "avg_price": 3_000.0,
                 "timestamp": "2024-01-01T00:00:01Z"},
            ],
        })
        ws._handle(msg)
        assert len(ws._fills) == 2

    def test_on_fill_callback_called_for_each_trade(self):
        callback = MagicMock()
        ws = self._ws(on_fill=callback)
        ws._handle(self._exec_msg(exec_type="trade"))
        callback.assert_called_once()
        fill_arg = callback.call_args[0][0]
        assert isinstance(fill_arg, Execution)

    def test_on_fill_not_called_for_non_trade(self):
        callback = MagicMock()
        ws = self._ws(on_fill=callback)
        ws._handle(self._exec_msg(exec_type="pending"))
        callback.assert_not_called()

    def test_no_callback_does_not_crash(self):
        ws = self._ws(on_fill=None)
        ws._handle(self._exec_msg(exec_type="trade"))  # should not raise

    def test_fill_symbol_and_side_recorded(self):
        ws = self._ws()
        msg = json.dumps({
            "channel": "executions", "type": "update",
            "data": [{"exec_type": "trade", "order_id": "X",
                      "symbol": "SOL/USD", "side": "sell",
                      "last_qty": 10.0, "avg_price": 150.0,
                      "timestamp": ""}],
        })
        ws._handle(msg)
        fill = ws._fills[0]
        assert fill.symbol == "SOL/USD"
        assert fill.side == "sell"

    # balances ────────────────────────────────────────────────────────────────

    def test_balance_snapshot_sets_value(self):
        ws = self._ws()
        ws._handle(self._balance_msg("USD", 5_000.0, "snapshot"))
        assert ws.get_balance("USD") == 5_000.0

    def test_balance_update_overwrites_previous(self):
        ws = self._ws()
        ws._handle(self._balance_msg("USD", 5_000.0))
        ws._handle(self._balance_msg("USD", 6_000.0, "update"))
        assert ws.get_balance("USD") == 6_000.0

    def test_multiple_assets_stored_independently(self):
        ws = self._ws()
        ws._handle(self._balance_msg("USD", 1_000.0))
        ws._handle(self._balance_msg("BTC", 0.5))
        assert ws.get_balance("USD") == 1_000.0
        assert ws.get_balance("BTC") == 0.5

    # error handling ──────────────────────────────────────────────────────────

    def test_invalid_json_does_not_raise(self):
        ws = self._ws()
        ws._handle("{not valid")

    def test_unknown_channel_does_not_crash(self):
        ws = self._ws()
        ws._handle(json.dumps({"channel": "heartbeat", "type": "update", "data": []}))


# ── KrakenPrivateWS.get_balance ───────────────────────────────────────────────

class TestKrakenPrivateWSGetBalance:
    def test_returns_zero_for_unseen_currency(self):
        ws = KrakenPrivateWS("k", "s")
        assert ws.get_balance("DOGE") == 0.0

    def test_returns_stored_balance(self):
        ws = KrakenPrivateWS("k", "s")
        ws._balances["ETH"] = 2.5
        assert ws.get_balance("ETH") == 2.5


# ── KrakenPrivateWS.pop_fills ─────────────────────────────────────────────────

class TestKrakenPrivateWSPopFills:
    def test_pop_fills_returns_all_queued_fills(self):
        ws = KrakenPrivateWS("k", "s")
        ws._fills = [MagicMock(), MagicMock(), MagicMock()]
        fills = ws.pop_fills()
        assert len(fills) == 3

    def test_pop_fills_clears_internal_list(self):
        ws = KrakenPrivateWS("k", "s")
        ws._fills = [MagicMock()]
        ws.pop_fills()
        assert ws._fills == []

    def test_pop_fills_returns_empty_list_initially(self):
        ws = KrakenPrivateWS("k", "s")
        assert ws.pop_fills() == []

    def test_pop_fills_second_call_returns_empty(self):
        ws = KrakenPrivateWS("k", "s")
        ws._fills = [MagicMock()]
        ws.pop_fills()
        assert ws.pop_fills() == []

    def test_pop_fills_returns_fills_in_order(self):
        ws = KrakenPrivateWS("k", "s")
        f1, f2 = MagicMock(), MagicMock()
        ws._fills = [f1, f2]
        fills = ws.pop_fills()
        assert fills[0] is f1
        assert fills[1] is f2


# ── KrakenPrivateWS.start reconnection ───────────────────────────────────────

class TestKrakenPrivateWSReconnect:
    async def test_reconnects_after_connect_exception(self, monkeypatch):
        monkeypatch.setattr(asyncio, "sleep", AsyncMock())
        monkeypatch.setattr("src.kraken_ws._fetch_ws_token",
                            AsyncMock(return_value="tok"))
        ws = KrakenPrivateWS("key", "sec")
        ws._running = True

        call_count = 0

        async def fake_connect():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise aiohttp.ClientError("ws error")
            ws._running = False

        monkeypatch.setattr(ws, "_connect", fake_connect)
        await ws.start()
        assert call_count == 2

    async def test_skips_connect_when_token_fetch_returns_none(self, monkeypatch):
        sleep_mock = AsyncMock()
        monkeypatch.setattr(asyncio, "sleep", sleep_mock)

        token_call_count = 0

        async def fake_fetch_token(key, secret):
            nonlocal token_call_count
            token_call_count += 1
            if token_call_count == 1:
                return None
            return "tok"

        monkeypatch.setattr("src.kraken_ws._fetch_ws_token", fake_fetch_token)

        connect_call_count = 0

        async def fake_connect(self_ws):
            nonlocal connect_call_count
            connect_call_count += 1
            self_ws._running = False

        monkeypatch.setattr(KrakenPrivateWS, "_connect", fake_connect)
        ws = KrakenPrivateWS("key", "sec")
        ws._running = True
        await ws.start()

        assert token_call_count == 2
        assert connect_call_count == 1
        # Must have slept 30s on the failed token attempt
        sleep_calls = [c[0][0] for c in sleep_mock.call_args_list]
        assert 30 in sleep_calls

    async def test_stop_exits_loop(self, monkeypatch):
        monkeypatch.setattr(asyncio, "sleep", AsyncMock())
        monkeypatch.setattr("src.kraken_ws._fetch_ws_token",
                            AsyncMock(return_value="tok"))
        ws = KrakenPrivateWS("key", "sec")

        async def fake_connect():
            ws.stop()

        monkeypatch.setattr(ws, "_connect", fake_connect)
        await ws.start()  # must not hang

    async def test_sleep_called_with_reconnect_delay_after_exception(self, monkeypatch):
        sleep_mock = AsyncMock()
        monkeypatch.setattr(asyncio, "sleep", sleep_mock)
        monkeypatch.setattr("src.kraken_ws._fetch_ws_token",
                            AsyncMock(return_value="tok"))
        ws = KrakenPrivateWS("key", "sec")
        ws._running = True

        call_count = 0

        async def fake_connect():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise aiohttp.ClientError("err")
            ws._running = False

        monkeypatch.setattr(ws, "_connect", fake_connect)
        await ws.start()
        sleep_calls = [c[0][0] for c in sleep_mock.call_args_list]
        assert _RECONNECT_DELAY in sleep_calls

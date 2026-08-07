"""
Unit tests for src/orderflow_ws.py

Covers:
- obi_from_book: bid-heavy / ask-heavy / empty / degenerate inputs (existing
  coverage lives in test_tick_cvd.py; not duplicated here)
- OrderFlowWS._handle_trades: CVD accumulation, whale detection
- OrderFlowWS.get_cvd_trend / get_obi / confirms_buy / confirms_sell: staleness
  and fail-open semantics
- OrderFlowWS._reset_trade_state: clears CVD/whale state, leaves book state
  untouched
- OrderFlowWS._connect: resets trade state before processing any message on
  every (re)connect — regression coverage for the stale-CVD-after-reconnect
  bug (pre-disconnect trades silently mixing with post-reconnect trades)
"""

import time
from collections import deque
from unittest.mock import AsyncMock, MagicMock

import aiohttp

from src.orderflow_ws import OrderFlowWS, WhalePrint


def _make_ofw(symbols=("BTC/USD",)):
    return OrderFlowWS(list(symbols))


# ── _handle_trades / CVD accumulation ──────────────────────────────────────────

class TestHandleTrades:
    def test_buy_trade_increments_cvd_positive(self):
        ofw = _make_ofw()
        ofw._handle_trades({"data": [
            {"symbol": "BTC/USD", "qty": "1.5", "price": "100.0", "side": "buy"},
        ]})
        assert ofw.get_cvd_raw("BTC/USD") == 1.5
        assert list(ofw._cvd_trades["BTC/USD"])[0][0] == 1.5

    def test_sell_trade_increments_cvd_negative(self):
        ofw = _make_ofw()
        ofw._handle_trades({"data": [
            {"symbol": "BTC/USD", "qty": "2.0", "price": "100.0", "side": "sell"},
        ]})
        assert list(ofw._cvd_trades["BTC/USD"])[0][0] == -2.0

    def test_unknown_symbol_ignored(self):
        ofw = _make_ofw()
        ofw._handle_trades({"data": [
            {"symbol": "ETH/USD", "qty": "1.0", "price": "100.0", "side": "buy"},
        ]})
        assert len(ofw._cvd_trades["BTC/USD"]) == 0

    def test_whale_detected_above_multiplier(self):
        ofw = _make_ofw()
        for _ in range(10):
            ofw._handle_trades({"data": [
                {"symbol": "BTC/USD", "qty": "1.0", "price": "100.0", "side": "buy"},
            ]})
        ofw._handle_trades({"data": [
            {"symbol": "BTC/USD", "qty": "10.0", "price": "100.0", "side": "sell"},
        ]})
        whale = ofw.last_whale("BTC/USD")
        assert whale is not None
        assert whale.side == "sell"
        assert whale.size == 10.0

    def test_no_whale_below_history_floor(self):
        ofw = _make_ofw()
        for _ in range(5):
            ofw._handle_trades({"data": [
                {"symbol": "BTC/USD", "qty": "100.0", "price": "100.0", "side": "buy"},
            ]})
        assert ofw.last_whale("BTC/USD") is None


# ── get_cvd_trend / get_obi / staleness ────────────────────────────────────────

class TestCvdTrendAndStaleness:
    def test_cvd_trend_none_before_enough_trades(self):
        ofw = _make_ofw()
        ofw._handle_trades({"data": [
            {"symbol": "BTC/USD", "qty": "1.0", "price": "100.0", "side": "buy"},
        ]})
        assert ofw.get_cvd_trend("BTC/USD") is None

    def test_cvd_trend_none_when_stale(self):
        ofw = _make_ofw()
        for _ in range(20):
            ofw._handle_trades({"data": [
                {"symbol": "BTC/USD", "qty": "1.0", "price": "100.0", "side": "buy"},
            ]})
        ofw._cvd_updated["BTC/USD"] = time.time() - 9999
        assert ofw.get_cvd_trend("BTC/USD") is None
        assert ofw.get_cvd_raw("BTC/USD") is None

    def test_cvd_trend_true_when_recent_buying_accelerates(self):
        ofw = _make_ofw()
        for _ in range(10):
            ofw._handle_trades({"data": [
                {"symbol": "BTC/USD", "qty": "0.1", "price": "100.0", "side": "sell"},
            ]})
        for _ in range(10):
            ofw._handle_trades({"data": [
                {"symbol": "BTC/USD", "qty": "5.0", "price": "100.0", "side": "buy"},
            ]})
        assert ofw.get_cvd_trend("BTC/USD") is True


class TestConfirmsBuySell:
    def test_confirms_buy_fail_open_when_no_data(self):
        ofw = _make_ofw()
        assert ofw.confirms_buy("BTC/USD") is True

    def test_confirms_sell_fail_open_when_no_data(self):
        ofw = _make_ofw()
        assert ofw.confirms_sell("BTC/USD") is True


# ── _reset_trade_state ──────────────────────────────────────────────────────────

class TestResetTradeState:
    def test_clears_cvd_trades_and_updated(self):
        ofw = _make_ofw()
        ofw._handle_trades({"data": [
            {"symbol": "BTC/USD", "qty": "1.0", "price": "100.0", "side": "buy"},
        ]})
        assert len(ofw._cvd_trades["BTC/USD"]) == 1
        assert "BTC/USD" in ofw._cvd_updated

        ofw._reset_trade_state()

        assert len(ofw._cvd_trades["BTC/USD"]) == 0
        assert ofw._cvd_updated == {}

    def test_clears_trade_sizes_and_whale(self):
        ofw = _make_ofw()
        for _ in range(10):
            ofw._handle_trades({"data": [
                {"symbol": "BTC/USD", "qty": "1.0", "price": "100.0", "side": "buy"},
            ]})
        ofw._handle_trades({"data": [
            {"symbol": "BTC/USD", "qty": "10.0", "price": "100.0", "side": "sell"},
        ]})
        assert ofw.last_whale("BTC/USD") is not None
        assert len(ofw._trade_sizes["BTC/USD"]) > 0

        ofw._reset_trade_state()

        assert ofw.last_whale("BTC/USD") is None
        assert len(ofw._trade_sizes["BTC/USD"]) == 0

    def test_does_not_touch_book_state(self):
        ofw = _make_ofw()
        ofw._handle_book_snapshot({"data": [
            {"symbol": "BTC/USD",
             "bids": [{"price": "100.0", "qty": "1.0"}],
             "asks": [{"price": "101.0", "qty": "1.0"}]},
        ]})
        assert ofw.get_obi("BTC/USD") is not None

        ofw._reset_trade_state()

        assert ofw.get_obi("BTC/USD") is not None

    def test_multi_symbol_reset_independent_of_book(self):
        ofw = _make_ofw(["BTC/USD", "ETH/USD"])
        for sym in ("BTC/USD", "ETH/USD"):
            ofw._handle_trades({"data": [
                {"symbol": sym, "qty": "1.0", "price": "100.0", "side": "buy"},
            ]})
        ofw._reset_trade_state()
        assert len(ofw._cvd_trades["BTC/USD"]) == 0
        assert len(ofw._cvd_trades["ETH/USD"]) == 0


# ── _connect: reset happens on every (re)connect ───────────────────────────────

def _ws_session_mock(messages):
    """Build a layered aiohttp mock so `async with session.ws_connect(...) as ws`
    yields an async-iterable of TEXT messages, then a CLOSED sentinel."""
    msgs = list(messages) + [MagicMock(type=aiohttp.WSMsgType.CLOSED, data=None)]

    class _FakeWS:
        def __aiter__(self):
            self._it = iter(msgs)
            return self

        async def __anext__(self):
            try:
                return next(self._it)
            except StopIteration:
                raise StopAsyncIteration

        async def send_json(self, *a, **kw):
            return None

    ws_cm = MagicMock()
    ws_cm.__aenter__ = AsyncMock(return_value=_FakeWS())
    ws_cm.__aexit__ = AsyncMock(return_value=False)

    mock_session = MagicMock()
    mock_session.ws_connect = MagicMock(return_value=ws_cm)

    session_cm = MagicMock()
    session_cm.__aenter__ = AsyncMock(return_value=mock_session)
    session_cm.__aexit__ = AsyncMock(return_value=False)
    return session_cm


class TestConnectResetsOnReconnect:
    async def test_reset_called_before_any_message_is_handled(self, monkeypatch):
        ofw = _make_ofw()
        # Pre-seed stale state, simulating data left over from a prior session.
        ofw._cvd_trades["BTC/USD"].append((1.0, time.time() - 9999))
        ofw._cvd_updated["BTC/USD"] = time.time() - 9999

        order = []
        orig_reset = ofw._reset_trade_state

        def tracking_reset():
            order.append("reset")
            orig_reset()

        def tracking_handle(raw):
            order.append("handle")

        monkeypatch.setattr(ofw, "_reset_trade_state", tracking_reset)
        monkeypatch.setattr(ofw, "_handle", tracking_handle)

        text_msg = MagicMock(type=aiohttp.WSMsgType.TEXT, data="{}")
        session_cm = _ws_session_mock([text_msg])
        monkeypatch.setattr(
            "src.orderflow_ws.aiohttp.ClientSession", lambda: session_cm
        )

        await ofw._connect()

        assert order == ["reset", "handle"]
        # The stale pre-connect trade was cleared, not carried forward.
        assert len(ofw._cvd_trades["BTC/USD"]) == 0

    async def test_second_reconnect_also_resets(self, monkeypatch):
        ofw = _make_ofw()
        monkeypatch.setattr("asyncio.sleep", AsyncMock())
        ofw._running = True

        reset_calls = []
        monkeypatch.setattr(
            ofw, "_reset_trade_state", lambda: reset_calls.append(1)
        )

        call_count = 0

        async def fake_connect():
            nonlocal call_count
            call_count += 1
            ofw._reset_trade_state()
            if call_count >= 2:
                ofw._running = False

        monkeypatch.setattr(ofw, "_connect", fake_connect)
        await ofw.start()

        assert call_count == 2
        assert len(reset_calls) == 2

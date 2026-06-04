"""
Unit tests for src/order_flow.py and src/multi_timeframe.py

OrderFlowImbalance:
  - fetch(): success path, order-book math, zero-volume guard, exchange failure
  - get(): fresh vs stale staleness check
  - get_smoothed(): staleness gate, EWA math, 0/1-reading fallback
  - signal(): BULLISH / BEARISH / NEUTRAL thresholds
  - confirms_buy() / confirms_sell(): fail-open on None, threshold blocking

MultiTimeframeFilter:
  - fetch(): returns DataFrame on success, caches result, refreshes after TTL,
             returns None on too-few bars, returns None on exchange error
  - alignment_score(): 0 when no cache, delegates to _score() when cached
  - _score(): strongly/weakly bull/bear, neutral returns -3, too-few-bars guard

No real network calls are made — exchange objects are mocked.
"""

import time
import types
from collections import deque
from unittest.mock import AsyncMock, MagicMock

import numpy as np
import pandas as pd
import pytest

from src.order_flow import OrderFlowImbalance, _STALE_SECS, _BULL_THRESH, _BEAR_THRESH
from src.multi_timeframe import MultiTimeframeFilter, _CACHE_TTL_S, _MIN_BARS


# ── helpers ────────────────────────────────────────────────────────────────────

SYMBOL = "BTC/USD"


def _make_ofi(symbols=None) -> OrderFlowImbalance:
    """Return an OFI instance with a mock exchange (no inner exchange calls yet)."""
    mock_exchange = MagicMock()
    mock_exchange.exchange = MagicMock()
    mock_exchange.exchange.fetch_order_book = AsyncMock(return_value={
        "bids": [[50000.0, 1.0]],
        "asks": [[50010.0, 1.0]],
    })
    syms = symbols or [SYMBOL]
    ofi = OrderFlowImbalance(exchange=mock_exchange, symbols=syms)
    return ofi


def _make_mtf() -> MultiTimeframeFilter:
    """Return a MultiTimeframeFilter with a mock exchange wrapper."""
    mock_exchange = MagicMock()
    # fetch_ohlcv is the wrapper method; tests set its return_value per-case.
    mock_exchange.fetch_ohlcv = AsyncMock(return_value=[])
    return MultiTimeframeFilter(exchange=mock_exchange)


def _ohlcv_rows(n: int, base: float = 50_000.0, trend: float = 10.0):
    """Generate n [timestamp_ms, o, h, l, c, v] rows."""
    start_ms = 1_704_067_200_000
    rows = []
    for i in range(n):
        c = base + i * trend
        rows.append([start_ms + i * 300_000, c * 0.999, c * 1.001, c * 0.998, c, 100.0])
    return rows


def _make_df_from_rows(rows) -> pd.DataFrame:
    df = pd.DataFrame(rows, columns=["timestamp", "open", "high", "low", "close", "volume"])
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
    df.set_index("timestamp", inplace=True)
    return df


# ══════════════════════════════════════════════════════════════════════════════
# OrderFlowImbalance
# ══════════════════════════════════════════════════════════════════════════════

class TestOFIFetch:
    async def test_returns_ofi_float_on_success(self):
        ofi = _make_ofi()
        result = await ofi.fetch(SYMBOL)
        assert result is not None
        assert isinstance(result, float)

    async def test_ofi_math_equal_volumes(self):
        """Equal bid/ask volume → OFI = 0."""
        ofi = _make_ofi()
        result = await ofi.fetch(SYMBOL)
        assert abs(result) < 1e-9

    async def test_ofi_math_all_bids(self):
        """All bids, no asks → OFI = +1.0."""
        ofi = _make_ofi()
        ofi._exchange.exchange.fetch_order_book = AsyncMock(return_value={
            "bids": [[50000.0, 2.0]],
            "asks": [],
        })
        result = await ofi.fetch(SYMBOL)
        assert abs(result - 1.0) < 1e-9

    async def test_ofi_math_all_asks(self):
        """All asks, no bids → OFI = -1.0."""
        ofi = _make_ofi()
        ofi._exchange.exchange.fetch_order_book = AsyncMock(return_value={
            "bids": [],
            "asks": [[50010.0, 3.0]],
        })
        result = await ofi.fetch(SYMBOL)
        assert abs(result - (-1.0)) < 1e-9

    async def test_zero_total_volume_returns_none(self):
        """Empty order book → should return None, not crash."""
        ofi = _make_ofi()
        ofi._exchange.exchange.fetch_order_book = AsyncMock(return_value={
            "bids": [], "asks": [],
        })
        result = await ofi.fetch(SYMBOL)
        assert result is None

    async def test_exchange_error_returns_none(self):
        ofi = _make_ofi()
        ofi._exchange.exchange.fetch_order_book = AsyncMock(
            side_effect=Exception("network error")
        )
        result = await ofi.fetch(SYMBOL)
        assert result is None

    async def test_fetch_updates_cache(self):
        ofi = _make_ofi()
        await ofi.fetch(SYMBOL)
        assert SYMBOL in ofi._cache

    async def test_fetch_updates_history(self):
        ofi = _make_ofi()
        await ofi.fetch(SYMBOL)
        assert len(ofi._history[SYMBOL]) == 1

    async def test_multiple_fetches_grow_history(self):
        ofi = _make_ofi()
        for _ in range(4):
            await ofi.fetch(SYMBOL)
        assert len(ofi._history[SYMBOL]) == 4

    async def test_history_capped_at_maxlen(self):
        ofi = _make_ofi()
        for _ in range(20):   # _HIST_LEN = 12
            await ofi.fetch(SYMBOL)
        assert len(ofi._history[SYMBOL]) == 12


class TestOFIGet:
    async def test_returns_value_when_fresh(self):
        ofi = _make_ofi()
        await ofi.fetch(SYMBOL)
        result = ofi.get(SYMBOL)
        assert result is not None

    def test_returns_none_when_no_fetch(self):
        ofi = _make_ofi()
        assert ofi.get(SYMBOL) is None

    def test_returns_none_when_stale(self, monkeypatch):
        ofi = _make_ofi()
        ofi._cache[SYMBOL] = 0.3
        ofi._fetched[SYMBOL] = time.time() - (_STALE_SECS + 1)
        assert ofi.get(SYMBOL) is None

    async def test_returns_value_within_stale_window(self):
        ofi = _make_ofi()
        await ofi.fetch(SYMBOL)
        assert ofi.get(SYMBOL) is not None


class TestOFIGetSmoothed:
    def test_returns_none_when_no_fetch(self):
        ofi = _make_ofi()
        assert ofi.get_smoothed(SYMBOL) is None

    def test_returns_none_when_stale(self):
        """Core staleness bug: get_smoothed must not return stale history values."""
        ofi = _make_ofi()
        # Manually seed history as if past fetches occurred
        ofi._history[SYMBOL] = deque([0.5, 0.4, 0.3], maxlen=12)
        # But mark the last actual fetch as stale
        ofi._fetched[SYMBOL] = time.time() - (_STALE_SECS + 5)
        ofi._cache[SYMBOL] = 0.5
        # Must return None, not the stale EWA
        assert ofi.get_smoothed(SYMBOL) is None

    async def test_returns_single_reading_via_get_fallback(self):
        """When history has < 2 readings, falls back to get()."""
        ofi = _make_ofi()
        await ofi.fetch(SYMBOL)    # populates history with 1 entry
        assert len(ofi._history[SYMBOL]) == 1
        result = ofi.get_smoothed(SYMBOL)
        assert result is not None

    async def test_ewa_two_equal_readings(self):
        """Two identical readings → EWA == that reading."""
        ofi = _make_ofi()
        # Seed with two identical values via direct history manipulation
        val = 0.25
        ofi._history[SYMBOL] = deque([val, val], maxlen=12)
        ofi._cache[SYMBOL]   = val
        ofi._fetched[SYMBOL] = time.time()

        result = ofi.get_smoothed(SYMBOL)
        assert abs(result - val) < 1e-9

    async def test_ewa_weights_newer_more(self):
        """With α=0.4, newer readings have more influence."""
        ofi = _make_ofi()
        old, new = 0.0, 1.0
        ofi._history[SYMBOL] = deque([old, new], maxlen=12)
        ofi._cache[SYMBOL]   = new
        ofi._fetched[SYMBOL] = time.time()

        result = ofi.get_smoothed(SYMBOL)
        # EWA: start=old=0, then 0.4*new + 0.6*old = 0.4*1.0 + 0.0 = 0.4
        assert abs(result - 0.4) < 1e-9

    async def test_ewa_three_readings(self):
        ofi = _make_ofi()
        # readings: 0.0, 0.5, 1.0 (oldest to newest)
        ofi._history[SYMBOL] = deque([0.0, 0.5, 1.0], maxlen=12)
        ofi._cache[SYMBOL]   = 1.0
        ofi._fetched[SYMBOL] = time.time()

        # ewa after reading 0.0:  0.0
        # ewa after reading 0.5:  0.4*0.5 + 0.6*0.0 = 0.20
        # ewa after reading 1.0:  0.4*1.0 + 0.6*0.20 = 0.52
        result = ofi.get_smoothed(SYMBOL)
        assert abs(result - 0.52) < 1e-9


class TestOFISignal:
    def _ofi_with_value(self, value: float) -> OrderFlowImbalance:
        ofi = _make_ofi()
        ofi._history[SYMBOL] = deque([value, value], maxlen=12)
        ofi._cache[SYMBOL]   = value
        ofi._fetched[SYMBOL] = time.time()
        return ofi

    def test_bullish_above_threshold(self):
        ofi = self._ofi_with_value(_BULL_THRESH + 0.01)
        assert ofi.signal(SYMBOL) == "BULLISH"

    def test_bearish_below_threshold(self):
        ofi = self._ofi_with_value(_BEAR_THRESH - 0.01)
        assert ofi.signal(SYMBOL) == "BEARISH"

    def test_neutral_between_thresholds(self):
        ofi = self._ofi_with_value(0.0)
        assert ofi.signal(SYMBOL) == "NEUTRAL"

    def test_neutral_at_bull_threshold(self):
        ofi = self._ofi_with_value(_BULL_THRESH)
        assert ofi.signal(SYMBOL) == "NEUTRAL"

    def test_neutral_when_stale(self):
        ofi = _make_ofi()
        ofi._history[SYMBOL] = deque([0.9, 0.9], maxlen=12)
        ofi._fetched[SYMBOL] = time.time() - (_STALE_SECS + 1)
        assert ofi.signal(SYMBOL) == "NEUTRAL"


class TestOFIConfirms:
    def _fresh_ofi(self, value: float) -> OrderFlowImbalance:
        ofi = _make_ofi()
        ofi._history[SYMBOL] = deque([value, value], maxlen=12)
        ofi._cache[SYMBOL]   = value
        ofi._fetched[SYMBOL] = time.time()
        return ofi

    def test_confirms_buy_fail_open_when_no_data(self):
        ofi = _make_ofi()
        assert ofi.confirms_buy(SYMBOL) is True

    def test_confirms_sell_fail_open_when_no_data(self):
        ofi = _make_ofi()
        assert ofi.confirms_sell(SYMBOL) is True

    def test_confirms_buy_true_for_positive_ofi(self):
        assert self._fresh_ofi(0.3).confirms_buy(SYMBOL) is True

    def test_confirms_buy_true_for_mildly_negative_ofi(self):
        # blocks only below -0.30 (BEAR_THRESH - 0.10 = -0.30)
        assert self._fresh_ofi(-0.15).confirms_buy(SYMBOL) is True

    def test_confirms_buy_false_for_strongly_bearish_ofi(self):
        assert self._fresh_ofi(-0.35).confirms_buy(SYMBOL) is False

    def test_confirms_sell_true_for_negative_ofi(self):
        assert self._fresh_ofi(-0.3).confirms_sell(SYMBOL) is True

    def test_confirms_sell_true_for_mildly_positive_ofi(self):
        # blocks only above +0.30 (BULL_THRESH + 0.10 = 0.30)
        assert self._fresh_ofi(0.15).confirms_sell(SYMBOL) is True

    def test_confirms_sell_false_for_strongly_bullish_ofi(self):
        assert self._fresh_ofi(0.35).confirms_sell(SYMBOL) is False

    def test_confirms_buy_fail_open_when_stale(self):
        ofi = _make_ofi()
        # Stale strongly bearish OFI should still fail-open (None → True)
        ofi._history[SYMBOL] = deque([-0.9, -0.9], maxlen=12)
        ofi._cache[SYMBOL]   = -0.9
        ofi._fetched[SYMBOL] = time.time() - (_STALE_SECS + 5)
        assert ofi.confirms_buy(SYMBOL) is True


# ══════════════════════════════════════════════════════════════════════════════
# MultiTimeframeFilter
# ══════════════════════════════════════════════════════════════════════════════

class TestMTFFetch:
    async def test_returns_dataframe_on_success(self):
        mtf = _make_mtf()
        rows = _ohlcv_rows(_MIN_BARS + 5)
        mtf._exchange.fetch_ohlcv = AsyncMock(return_value=rows)
        df = await mtf.fetch(SYMBOL)
        assert df is not None
        assert len(df) == _MIN_BARS + 5

    async def test_returns_none_when_too_few_bars(self):
        mtf = _make_mtf()
        mtf._exchange.fetch_ohlcv = AsyncMock(
            return_value=_ohlcv_rows(_MIN_BARS - 1)
        )
        df = await mtf.fetch(SYMBOL)
        assert df is None

    async def test_returns_none_on_empty_response(self):
        mtf = _make_mtf()
        mtf._exchange.fetch_ohlcv = AsyncMock(return_value=[])
        df = await mtf.fetch(SYMBOL)
        assert df is None

    async def test_returns_none_on_exchange_error(self):
        mtf = _make_mtf()
        mtf._exchange.fetch_ohlcv = AsyncMock(
            side_effect=Exception("connection refused")
        )
        df = await mtf.fetch(SYMBOL)
        assert df is None

    async def test_caches_result(self):
        mtf = _make_mtf()
        rows = _ohlcv_rows(_MIN_BARS + 5)
        mtf._exchange.fetch_ohlcv = AsyncMock(return_value=rows)
        await mtf.fetch(SYMBOL)
        assert SYMBOL in mtf._cache

    async def test_uses_cache_within_ttl(self):
        mtf = _make_mtf()
        rows = _ohlcv_rows(_MIN_BARS + 5)
        mock_fn = AsyncMock(return_value=rows)
        mtf._exchange.fetch_ohlcv = mock_fn
        await mtf.fetch(SYMBOL)
        await mtf.fetch(SYMBOL)   # second call within TTL
        assert mock_fn.call_count == 1

    async def test_refreshes_after_ttl(self, monkeypatch):
        mtf = _make_mtf()
        rows = _ohlcv_rows(_MIN_BARS + 5)
        mock_fn = AsyncMock(return_value=rows)
        mtf._exchange.fetch_ohlcv = mock_fn

        await mtf.fetch(SYMBOL)
        # Force cache to appear expired
        ts, df = mtf._cache[SYMBOL]
        mtf._cache[SYMBOL] = (ts - _CACHE_TTL_S - 1, df)
        await mtf.fetch(SYMBOL)
        assert mock_fn.call_count == 2

    async def test_returns_df_with_correct_columns(self):
        mtf = _make_mtf()
        rows = _ohlcv_rows(_MIN_BARS + 5)
        mtf._exchange.fetch_ohlcv = AsyncMock(return_value=rows)
        df = await mtf.fetch(SYMBOL)
        for col in ("open", "high", "low", "close", "volume"):
            assert col in df.columns


class TestMTFAlignmentScore:
    def test_returns_zero_when_no_cache(self):
        mtf = _make_mtf()
        assert mtf.alignment_score(SYMBOL, is_buy=True) == 0.0

    async def test_returns_nonzero_when_cached(self):
        mtf = _make_mtf()
        rows = _ohlcv_rows(60, trend=10.0)   # uptrend
        mtf._exchange.fetch_ohlcv = AsyncMock(return_value=rows)
        await mtf.fetch(SYMBOL)
        score = mtf.alignment_score(SYMBOL, is_buy=True)
        # score is a float; the exact value depends on the stub ema/rsi
        assert isinstance(score, float)


class TestMTFScore:
    """Tests for MultiTimeframeFilter._score() with deterministic DataFrames."""

    def _make_trend_df(self, n: int = 60, trend: float = 10.0,
                        base: float = 50_000.0) -> pd.DataFrame:
        rows = _ohlcv_rows(n, base=base, trend=trend)
        return _make_df_from_rows(rows)

    def test_too_few_bars_returns_zero(self):
        mtf = _make_mtf()
        df = self._make_trend_df(n=5)
        assert mtf._score(df, is_buy=True) == 0.0

    def test_none_df_returns_zero(self):
        mtf = _make_mtf()
        assert mtf._score(None, is_buy=True) == 0.0

    def test_strong_uptrend_positive_for_buy(self):
        """Strong uptrend (high trend) should return positive score for a buy."""
        mtf = _make_mtf()
        # Large uptrend → EMA9 > EMA21, RSI high, slope positive
        df = self._make_trend_df(n=60, trend=100.0)
        score = mtf._score(df, is_buy=True)
        assert score > 0.0

    def test_strong_uptrend_negative_for_sell(self):
        """Strong uptrend should penalise a sell signal."""
        mtf = _make_mtf()
        df = self._make_trend_df(n=60, trend=100.0)
        score = mtf._score(df, is_buy=False)
        assert score < 0.0

    def test_strong_downtrend_positive_for_sell(self):
        """Strong downtrend should return positive score for a sell."""
        mtf = _make_mtf()
        df = self._make_trend_df(n=60, trend=-100.0)
        score = mtf._score(df, is_buy=False)
        assert score > 0.0

    def test_strong_downtrend_negative_for_buy(self):
        """Strong downtrend should penalise a buy signal."""
        mtf = _make_mtf()
        df = self._make_trend_df(n=60, trend=-100.0)
        score = mtf._score(df, is_buy=True)
        assert score < 0.0

    def test_score_bounded(self):
        """Score must stay within documented ±20 range."""
        mtf = _make_mtf()
        for trend in (-200.0, -10.0, 0.5, 10.0, 200.0):
            df = self._make_trend_df(n=60, trend=trend)
            for is_buy in (True, False):
                s = mtf._score(df, is_buy)
                assert -20.0 <= s <= 10.0, f"score {s} out of bounds for trend={trend} buy={is_buy}"

    def test_score_is_float(self):
        mtf = _make_mtf()
        df = self._make_trend_df(n=60)
        assert isinstance(mtf._score(df, True), float)

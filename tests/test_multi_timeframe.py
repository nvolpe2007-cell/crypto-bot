"""
Unit tests for src/multi_timeframe.py

Covers:
- MultiTimeframeFilter.fetch: uses the ExchangeConnection wrapper (not raw ccxt),
  caches results within TTL, refreshes after TTL, returns None on missing/short data
- MultiTimeframeFilter.alignment_score: returns 0.0 when no cached data, non-zero
  when cache is populated
- MultiTimeframeFilter._score: all scoring branches for is_buy=True and is_sell=False,
  too-few-bars guard, None-df guard

The strongly-bull/bear/neutral cases use real price sequences fed through the
pandas_ta stub in conftest.py (real EMA/RSI math).  The weakly-bull/bear cases
mock ta directly to pin the EMA and RSI values to the exact boundary needed.
"""

import time
import pytest
import pandas as pd
import numpy as np
from unittest.mock import AsyncMock, MagicMock, patch

from src.multi_timeframe import MultiTimeframeFilter, _CACHE_TTL_S, _MIN_BARS


# ── helpers ───────────────────────────────────────────────────────────────────

def _make_ohlcv_rows(n: int = 35, price: float = 50_000.0) -> list:
    """Return n OHLCV rows as [[ts_ms, o, h, l, c, v], ...]."""
    start_ms = 1_700_000_000_000
    return [[start_ms + i * 300_000, price, price, price, price, 100.0]
            for i in range(n)]


def _make_exchange(ohlcv_rows=None) -> MagicMock:
    """Mock that represents an ExchangeConnection wrapper with fetch_ohlcv."""
    exc = MagicMock()
    exc.fetch_ohlcv = AsyncMock(return_value=ohlcv_rows or [])
    # Poison the raw ccxt handle so any accidental bypass fails loudly.
    exc.exchange = MagicMock()
    exc.exchange.fetch_ohlcv = AsyncMock(
        side_effect=AssertionError("bug: called raw ccxt instead of the wrapper")
    )
    return exc


def _uptrend_df(n: int = 40) -> pd.DataFrame:
    """40 bars rising 200 USD/bar — EMA9 >> EMA21, RSI >> 50, slope > 0."""
    prices = [50_000.0 + i * 200.0 for i in range(n)]
    return pd.DataFrame(
        {"close": prices},
        index=pd.date_range("2024-01-01", periods=n, freq="5min"),
    )


def _downtrend_df(n: int = 40) -> pd.DataFrame:
    """40 bars falling 200 USD/bar — EMA9 << EMA21, RSI << 50, slope < 0."""
    prices = [58_000.0 - i * 200.0 for i in range(n)]
    return pd.DataFrame(
        {"close": prices},
        index=pd.date_range("2024-01-01", periods=n, freq="5min"),
    )


def _flat_df(n: int = 40, price: float = 50_000.0) -> pd.DataFrame:
    """40 flat bars — EMA9 == EMA21, RSI == 50, slope == 0."""
    return pd.DataFrame(
        {"close": [price] * n},
        index=pd.date_range("2024-01-01", periods=n, freq="5min"),
    )


def _pinned_series(val: float, n: int = 40, val_minus4: float = None) -> pd.Series:
    """Series of length n where iloc[-1]=val and optionally iloc[-4]=val_minus4."""
    data = np.ones(n) * val
    if val_minus4 is not None and n >= 4:
        data[-4] = val_minus4
    return pd.Series(data)


# ── TestFetch ─────────────────────────────────────────────────────────────────

class TestFetch:
    async def test_uses_wrapper_not_raw_exchange(self):
        """Regression: fetch() must call self._exchange.fetch_ohlcv (wrapper),
        not self._exchange.exchange.fetch_ohlcv (raw ccxt)."""
        rows = _make_ohlcv_rows(35)
        exchange = _make_exchange(rows)
        mtf = MultiTimeframeFilter(exchange)

        result = await mtf.fetch("BTC/USD")

        assert result is not None
        exchange.fetch_ohlcv.assert_called_once_with("BTC/USD", "5m", limit=60)
        exchange.exchange.fetch_ohlcv.assert_not_called()

    async def test_returns_none_when_fewer_than_min_bars(self):
        """Fewer than _MIN_BARS rows → return None without caching."""
        exchange = _make_exchange(_make_ohlcv_rows(n=_MIN_BARS - 1))
        mtf = MultiTimeframeFilter(exchange)
        result = await mtf.fetch("ETH/USD")
        assert result is None
        assert "ETH/USD" not in mtf._cache

    async def test_returns_none_on_empty_response(self):
        """Empty exchange response → None."""
        exchange = _make_exchange([])
        mtf = MultiTimeframeFilter(exchange)
        assert await mtf.fetch("BTC/USD") is None

    async def test_caches_result_within_ttl(self):
        """Second call within TTL hits the cache — no additional API request."""
        rows = _make_ohlcv_rows(35)
        exchange = _make_exchange(rows)
        mtf = MultiTimeframeFilter(exchange)

        df1 = await mtf.fetch("BTC/USD")
        df2 = await mtf.fetch("BTC/USD")

        assert df1 is df2           # exact same object from cache
        assert exchange.fetch_ohlcv.call_count == 1

    async def test_refreshes_after_ttl_expires(self):
        """Call after TTL expiry triggers a new API fetch."""
        rows = _make_ohlcv_rows(35)
        exchange = _make_exchange(rows)
        mtf = MultiTimeframeFilter(exchange)

        with patch("src.multi_timeframe.time") as mock_time:
            mock_time.time.return_value = 1000.0
            await mtf.fetch("BTC/USD")
            assert exchange.fetch_ohlcv.call_count == 1

            # Still within TTL — no refresh
            mock_time.time.return_value = 1000.0 + _CACHE_TTL_S - 1
            await mtf.fetch("BTC/USD")
            assert exchange.fetch_ohlcv.call_count == 1

            # Past TTL — refresh required
            mock_time.time.return_value = 1000.0 + _CACHE_TTL_S + 1
            await mtf.fetch("BTC/USD")
            assert exchange.fetch_ohlcv.call_count == 2

    async def test_returns_none_on_api_exception(self):
        """If the exchange wrapper raises unexpectedly, return None gracefully."""
        exchange = MagicMock()
        exchange.fetch_ohlcv = AsyncMock(side_effect=RuntimeError("network failure"))
        exchange.exchange = MagicMock()
        mtf = MultiTimeframeFilter(exchange)
        result = await mtf.fetch("SOL/USD")
        assert result is None

    async def test_returned_dataframe_has_close_column(self):
        """Returned DataFrame must include a 'close' column."""
        rows = _make_ohlcv_rows(35)
        exchange = _make_exchange(rows)
        mtf = MultiTimeframeFilter(exchange)
        df = await mtf.fetch("BTC/USD")
        assert df is not None
        assert "close" in df.columns


# ── TestAlignmentScore ────────────────────────────────────────────────────────

class TestAlignmentScore:
    def test_returns_zero_with_no_cached_data(self):
        mtf = MultiTimeframeFilter(MagicMock())
        assert mtf.alignment_score("BTC/USD", is_buy=True) == 0.0
        assert mtf.alignment_score("BTC/USD", is_buy=False) == 0.0

    def test_returns_nonzero_when_cache_is_populated(self):
        mtf = MultiTimeframeFilter(MagicMock())
        df = _uptrend_df()
        mtf._cache["BTC/USD"] = (time.time(), df)
        score_buy = mtf.alignment_score("BTC/USD", is_buy=True)
        assert score_buy != 0.0

    def test_different_symbols_use_independent_caches(self):
        mtf = MultiTimeframeFilter(MagicMock())
        mtf._cache["BTC/USD"] = (time.time(), _uptrend_df())
        assert mtf.alignment_score("ETH/USD", is_buy=True) == 0.0
        assert mtf.alignment_score("BTC/USD", is_buy=True) != 0.0


# ── TestScoreRealData — real price sequences via conftest pandas_ta stub ───────

class TestScoreRealData:
    """Use genuine uptrend / downtrend / flat price series to exercise the
    scoring branches end-to-end (real EMA + RSI math from the conftest stub)."""

    def test_strongly_bullish_buy_returns_plus10(self):
        """Strong uptrend + buying in same direction → +10."""
        mtf = MultiTimeframeFilter(MagicMock())
        assert mtf._score(_uptrend_df(), is_buy=True) == pytest.approx(10.0)

    def test_strongly_bullish_sell_returns_minus20(self):
        """Strong uptrend but trying to sell → -20 (opposes the trend)."""
        mtf = MultiTimeframeFilter(MagicMock())
        assert mtf._score(_uptrend_df(), is_buy=False) == pytest.approx(-20.0)

    def test_strongly_bearish_buy_returns_minus20(self):
        """Strong downtrend + buying → -20."""
        mtf = MultiTimeframeFilter(MagicMock())
        assert mtf._score(_downtrend_df(), is_buy=True) == pytest.approx(-20.0)

    def test_strongly_bearish_sell_returns_plus10(self):
        """Strong downtrend + selling in same direction → +10."""
        mtf = MultiTimeframeFilter(MagicMock())
        assert mtf._score(_downtrend_df(), is_buy=False) == pytest.approx(10.0)

    def test_neutral_buy_returns_minus3(self):
        """Flat market (EMA9 == EMA21, RSI==50): neutral → -3 mild penalty."""
        mtf = MultiTimeframeFilter(MagicMock())
        assert mtf._score(_flat_df(), is_buy=True) == pytest.approx(-3.0)

    def test_neutral_sell_returns_minus3(self):
        mtf = MultiTimeframeFilter(MagicMock())
        assert mtf._score(_flat_df(), is_buy=False) == pytest.approx(-3.0)

    def test_too_few_bars_returns_zero(self):
        """Fewer than _MIN_BARS rows → 0 (guard at top of _score)."""
        mtf = MultiTimeframeFilter(MagicMock())
        short_df = _uptrend_df(n=_MIN_BARS - 1)
        assert mtf._score(short_df, is_buy=True) == pytest.approx(0.0)

    def test_none_df_returns_zero(self):
        mtf = MultiTimeframeFilter(MagicMock())
        assert mtf._score(None, is_buy=True) == pytest.approx(0.0)


# ── TestScoreMocked — mock ta for boundary/weakly conditions ──────────────────

class TestScoreMocked:
    """Pin EMA9, EMA21, RSI, and slope to specific values to exercise the
    weakly-bull and weakly-bear branches without needing carefully crafted prices.

    Weakly bullish  = e9>e21 AND rsi∈(45,50]: not all of {rsi>50, slope>0}
    Weakly bearish  = e9<e21 AND rsi∈[50,55): not all of {rsi<50, slope<0}
    """

    def _score_with_pinned(self, ema9_last, ema21_last, rsi_last,
                           ema9_prev4, is_buy: bool) -> float:
        """Call _score() with ta mocked to return pinned indicator values."""
        n = 40
        df = _flat_df(n=n)  # content doesn't matter — ta is fully mocked
        mtf = MultiTimeframeFilter(MagicMock())

        ema9 = _pinned_series(ema9_last, n=n, val_minus4=ema9_prev4)
        ema21 = _pinned_series(ema21_last, n=n)
        rsi = _pinned_series(rsi_last, n=n)

        with patch("src.multi_timeframe.ta") as mock_ta:
            mock_ta.ema.side_effect = [ema9, ema21]
            mock_ta.rsi.return_value = rsi
            return mtf._score(df, is_buy=is_buy)

    # Weakly bullish: e9>e21, rsi=48 (>45 not >50), slope<0 (not strongly bull)
    def test_weakly_bullish_buy_returns_plus5(self):
        score = self._score_with_pinned(
            ema9_last=51_000, ema21_last=50_000, rsi_last=48.0,
            ema9_prev4=52_000,   # slope = 51k−52k = −1000 < 0 → not strongly bull
            is_buy=True,
        )
        assert score == pytest.approx(5.0)

    def test_weakly_bullish_sell_returns_minus10(self):
        score = self._score_with_pinned(
            ema9_last=51_000, ema21_last=50_000, rsi_last=48.0,
            ema9_prev4=52_000,
            is_buy=False,
        )
        assert score == pytest.approx(-10.0)

    # Weakly bearish: e9<e21, rsi=52 (<55 not <50), slope>0 (not strongly bear)
    def test_weakly_bearish_buy_returns_minus10(self):
        score = self._score_with_pinned(
            ema9_last=49_000, ema21_last=50_000, rsi_last=52.0,
            ema9_prev4=48_000,   # slope = 49k−48k = +1000 > 0 → not strongly bear
            is_buy=True,
        )
        assert score == pytest.approx(-10.0)

    def test_weakly_bearish_sell_returns_plus5(self):
        score = self._score_with_pinned(
            ema9_last=49_000, ema21_last=50_000, rsi_last=52.0,
            ema9_prev4=48_000,
            is_buy=False,
        )
        assert score == pytest.approx(5.0)

    def test_ta_ema_returns_none_falls_back_to_zero(self):
        """If ta.ema returns None (thin data), _score gracefully returns 0."""
        df = _flat_df()
        mtf = MultiTimeframeFilter(MagicMock())
        with patch("src.multi_timeframe.ta") as mock_ta:
            mock_ta.ema.return_value = None
            mock_ta.rsi.return_value = _pinned_series(55.0)
            result = mtf._score(df, is_buy=True)
        assert result == pytest.approx(0.0)

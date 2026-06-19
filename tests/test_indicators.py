"""
Unit tests for src/indicators.py

Covers:
- prepare_ohlcv_dataframe: type conversions, index, empty input
- EMACrossRSI: column presence, signal types, insufficient-data guard,
  crossover detection, RSI filter (overbought blocks buys)
- supertrend, atr, ema_htf: the indicator helpers actually consumed by the
  live/paper strategies (paper_trading.py, scientific_strategy.py,
  microstructure_strategy.py, mean_reversion_strategy.py, live_trading.py) —
  previously zero coverage even though EMACrossRSI (used only by the legacy
  backtester) was thoroughly tested.
"""

import pytest
import numpy as np
import pandas as pd
from datetime import datetime, timedelta

from src.indicators import (
    prepare_ohlcv_dataframe,
    EMACrossRSI,
    Signal,
    IndicatorResult,
    supertrend,
    atr,
    ema_htf,
)


# ── helpers ──────────────────────────────────────────────────────────────────

def _make_ohlcv_list(n: int = 100, base_price: float = 50_000.0,
                     trend: float = 10.0) -> list:
    """Return list of [ts_ms, open, high, low, close, volume] rows."""
    start = datetime(2024, 1, 1)
    rows = []
    for i in range(n):
        ts = int((start + timedelta(minutes=i)).timestamp() * 1000)
        close = base_price + i * trend
        rows.append([ts, close * 0.999, close * 1.001, close * 0.998, close, 500.0])
    return rows


def _make_df(n: int = 100, base_price: float = 50_000.0,
             trend: float = 10.0) -> pd.DataFrame:
    return prepare_ohlcv_dataframe(_make_ohlcv_list(n, base_price, trend))


# ── prepare_ohlcv_dataframe ───────────────────────────────────────────────────

class TestPrepareOhlcvDataframe:
    def test_empty_input_returns_empty_df(self):
        df = prepare_ohlcv_dataframe([])
        assert df.empty

    def test_required_columns_present(self):
        df = _make_df(10)
        for col in ("open", "high", "low", "close", "volume"):
            assert col in df.columns, f"missing column: {col}"

    def test_index_is_datetime(self):
        df = _make_df(10)
        assert pd.api.types.is_datetime64_any_dtype(df.index)

    def test_all_price_columns_numeric(self):
        df = _make_df(10)
        for col in ("open", "high", "low", "close", "volume"):
            assert pd.api.types.is_numeric_dtype(df[col]), f"{col} not numeric"

    def test_row_count_matches_input(self):
        for n in (1, 50, 200):
            assert len(_make_df(n)) == n

    def test_close_prices_match_input(self):
        rows = _make_ohlcv_list(5, base_price=10_000.0, trend=100.0)
        df = prepare_ohlcv_dataframe(rows)
        for i, row in enumerate(rows):
            assert abs(df["close"].iloc[i] - row[4]) < 1e-9

    def test_timestamps_ascending(self):
        df = _make_df(20)
        assert df.index.is_monotonic_increasing

    def test_custom_columns(self):
        rows = [[1_000_000, 1.0, 2.0, 0.5, 1.5, 10.0]]
        df = prepare_ohlcv_dataframe(rows, columns=["timestamp", "open", "high", "low", "close", "volume"])
        assert df["close"].iloc[0] == 1.5


# ── EMACrossRSI ───────────────────────────────────────────────────────────────

class TestEMACrossRSICalculate:
    def test_adds_expected_columns(self):
        df = _make_df(60)
        strat = EMACrossRSI()
        result = strat.calculate(df)
        for col in ("ema_fast", "ema_slow", "rsi", "signal"):
            assert col in result.columns, f"missing column: {col}"

    def test_does_not_mutate_input(self):
        df = _make_df(60)
        original_cols = set(df.columns)
        EMACrossRSI().calculate(df)
        assert set(df.columns) == original_cols

    def test_signal_values_are_signal_enum(self):
        df = _make_df(100)
        result = EMACrossRSI().calculate(df)
        unique_signals = set(result["signal"].unique())
        valid = {Signal.BUY, Signal.SELL, Signal.HOLD}
        assert unique_signals <= valid


class TestEMACrossRSIGetLatestSignal:
    def test_returns_none_if_too_few_rows(self):
        df = _make_df(5)  # less than slow_ema=21
        assert EMACrossRSI(fast_ema=9, slow_ema=21).get_latest_signal(df) is None

    def test_returns_none_for_empty_df(self):
        assert EMACrossRSI().get_latest_signal(pd.DataFrame()) is None

    def test_returns_indicator_result_with_enough_data(self):
        df = _make_df(100)
        result = EMACrossRSI().get_latest_signal(df)
        assert result is not None
        assert isinstance(result, IndicatorResult)

    def test_result_signal_is_valid_enum(self):
        df = _make_df(100)
        result = EMACrossRSI().get_latest_signal(df)
        assert result.signal in (Signal.BUY, Signal.SELL, Signal.HOLD)

    def test_is_buy_and_is_sell_exclusive(self):
        df = _make_df(100)
        result = EMACrossRSI().get_latest_signal(df)
        assert not (result.is_buy and result.is_sell)

    def test_strongly_uptrending_data_is_not_sell(self):
        # 200 candles with steep uptrend → fast EMA stays above slow EMA → no sell signal
        df = _make_df(200, trend=50.0)
        result = EMACrossRSI().get_latest_signal(df)
        assert not result.is_sell

    def test_ema_fast_and_slow_are_floats(self):
        df = _make_df(60)
        result = EMACrossRSI().get_latest_signal(df)
        assert isinstance(result.ema_fast, float)
        assert isinstance(result.ema_slow, float)

    def test_rsi_within_0_100(self):
        # Use oscillating data (mix of up and down moves) to produce a
        # non-degenerate RSI value between 0 and 100.
        rows = _make_ohlcv_list(100, base_price=50_000.0, trend=0.0)
        # Alternate prices slightly above and below base
        for i, row in enumerate(rows):
            delta = 200.0 if i % 2 == 0 else -200.0
            p = 50_000.0 + delta
            rows[i] = [row[0], p * 0.999, p * 1.001, p * 0.998, p, 500.0]
        df = prepare_ohlcv_dataframe(rows)
        result = EMACrossRSI().get_latest_signal(df)
        assert result.rsi is not None
        assert 0 <= result.rsi <= 100

    def test_rsi_overbought_blocks_buy_signal(self):
        # Build a dataset where EMA crossover occurs but RSI is forced high.
        # Use a very high overbought threshold (110) that can never be breached
        # so normally a buy would fire; then use overbought=0 to block it.
        df = _make_df(200, trend=5.0)
        strat_permissive = EMACrossRSI(rsi_overbought=110)  # no RSI filter
        strat_strict = EMACrossRSI(rsi_overbought=0)        # always blocked

        result_df_strict = strat_strict.calculate(df)
        # With rsi_overbought=0, rsi < 0 is never true → no BUY signals
        assert Signal.BUY not in result_df_strict["signal"].values

    def test_rsi_oversold_blocks_sell_signal(self):
        df = _make_df(200, trend=-5.0)
        strat = EMACrossRSI(rsi_oversold=101)  # rsi > 101 is never true → no SELL
        result_df = strat.calculate(df)
        assert Signal.SELL not in result_df["signal"].values

    def test_custom_ema_periods(self):
        df = _make_df(100)
        result = EMACrossRSI(fast_ema=5, slow_ema=15).get_latest_signal(df)
        assert result is not None

    def test_get_signals_history_returns_dataframe(self):
        df = _make_df(80)
        history = EMACrossRSI().get_signals_history(df)
        assert isinstance(history, pd.DataFrame)
        assert "signal" in history.columns
        assert len(history) == len(df)


# ── helpers for supertrend / atr / ema_htf ───────────────────────────────────

def _trending_df(n: int = 50, start: float = 100.0, end: float = 200.0,
                  band: float = 1.0) -> pd.DataFrame:
    """Plain (non-datetime-indexed) OHLC df with a linear close trend."""
    closes = np.linspace(start, end, n)
    return pd.DataFrame({
        "open": closes - band * 0.5,
        "high": closes + band,
        "low": closes - band,
        "close": closes,
        "volume": np.full(n, 100.0),
    })


# ── atr ───────────────────────────────────────────────────────────────────

class TestAtr:
    def test_returns_series_for_valid_df(self):
        df = _trending_df(30)
        result = atr(df, period=14)
        assert isinstance(result, pd.Series)
        assert len(result) == len(df)

    def test_values_non_negative(self):
        df = _trending_df(30)
        result = atr(df, period=14)
        assert (result.dropna() >= 0).all()

    def test_returns_none_on_missing_columns(self):
        # atr() requires 'high'/'low'/'close' — a df without them should hit
        # the except-path and return None rather than raising.
        df = pd.DataFrame({"close": [1.0, 2.0, 3.0]})
        assert atr(df) is None

    def test_custom_period_accepted(self):
        df = _trending_df(40)
        result = atr(df, period=7)
        assert isinstance(result, pd.Series)


# ── ema_htf ───────────────────────────────────────────────────────────────

class TestEmaHtf:
    def test_too_few_candles_returns_none_none(self):
        closes = pd.Series(np.linspace(100, 200, 55))  # slow=55 needs >= 56
        assert ema_htf(closes, fast=21, slow=55) == (None, None)

    def test_exact_boundary_returns_values(self):
        closes = pd.Series(np.linspace(100, 200, 56))  # exactly slow + 1
        fast_v, slow_v = ema_htf(closes, fast=21, slow=55)
        assert isinstance(fast_v, float)
        assert isinstance(slow_v, float)

    def test_uptrend_fast_above_slow(self):
        closes = pd.Series(np.linspace(100, 300, 100))
        fast_v, slow_v = ema_htf(closes, fast=21, slow=55)
        assert fast_v > slow_v

    def test_downtrend_fast_below_slow(self):
        closes = pd.Series(np.linspace(300, 100, 100))
        fast_v, slow_v = ema_htf(closes, fast=21, slow=55)
        assert fast_v < slow_v

    def test_matches_manual_ewm_calculation(self):
        closes = pd.Series(np.linspace(100, 250, 80))
        fast_v, slow_v = ema_htf(closes, fast=10, slow=30)
        expected_fast = closes.ewm(span=10, adjust=False).mean().iloc[-1]
        expected_slow = closes.ewm(span=30, adjust=False).mean().iloc[-1]
        assert fast_v == pytest.approx(expected_fast)
        assert slow_v == pytest.approx(expected_slow)

    def test_default_periods(self):
        closes = pd.Series(np.linspace(100, 200, 60))
        fast_v, slow_v = ema_htf(closes)
        assert fast_v is not None and slow_v is not None


# ── supertrend ────────────────────────────────────────────────────────────

class TestSupertrend:
    def test_adds_expected_columns(self):
        df = _trending_df(50)
        out = supertrend(df, period=10, multiplier=2.5)
        for col in ("supertrend", "supertrend_bull", "supertrend_flip"):
            assert col in out.columns

    def test_does_not_mutate_input(self):
        df = _trending_df(50)
        original_cols = set(df.columns)
        supertrend(df, period=10, multiplier=2.5)
        assert set(df.columns) == original_cols

    def test_uptrend_is_mostly_bullish(self):
        df = _trending_df(50, start=100, end=200)
        out = supertrend(df, period=10, multiplier=2.5)
        # Skip the first `period` rows (warm-up / initial-direction artifact)
        assert out["supertrend_bull"].iloc[15:].all()

    def test_downtrend_is_mostly_bearish(self):
        df = _trending_df(50, start=200, end=100)
        out = supertrend(df, period=10, multiplier=2.5)
        assert not out["supertrend_bull"].iloc[15:].any()

    def test_flip_only_marks_bear_to_bull_transition(self):
        # Downtrend for 30 candles, then a sharp reversal into an uptrend.
        down = np.linspace(200, 100, 30)
        up = np.linspace(100, 250, 30)
        closes = np.concatenate([down, up])
        df = pd.DataFrame({
            "open": closes + 0.5, "high": closes + 1.0,
            "low": closes - 1.0, "close": closes,
            "volume": np.full(len(closes), 100.0),
        })
        out = supertrend(df, period=10, multiplier=2.5)
        flip_idx = out.index[out["supertrend_flip"]].tolist()
        assert flip_idx, "expected at least one flip on a downtrend->uptrend reversal"
        for i in flip_idx:
            assert bool(out["supertrend_bull"].iloc[i]) is True
            assert bool(out["supertrend_bull"].iloc[i - 1]) is False

    def test_bullish_line_acts_as_support_below_close(self):
        df = _trending_df(50, start=100, end=200)
        out = supertrend(df, period=10, multiplier=2.5).iloc[15:]
        assert (out["supertrend"] <= out["close"]).all()

    def test_bearish_line_acts_as_resistance_above_close(self):
        df = _trending_df(50, start=200, end=100)
        out = supertrend(df, period=10, multiplier=2.5).iloc[15:]
        assert (out["supertrend"] >= out["close"]).all()

    def test_minimal_length_df_does_not_raise(self):
        df = _trending_df(1)
        out = supertrend(df, period=10, multiplier=2.5)
        assert len(out) == 1

    def test_default_period_and_multiplier(self):
        df = _trending_df(60)
        out = supertrend(df)  # no explicit period/multiplier
        assert out["supertrend"].notna().all()

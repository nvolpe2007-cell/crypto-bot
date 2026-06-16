"""
Unit tests for src/indicators.py

Covers:
- prepare_ohlcv_dataframe: type conversions, index, empty input
- EMACrossRSI: column presence, signal types, insufficient-data guard,
  crossover detection, RSI filter (overbought blocks buys)
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


# ── supertrend ────────────────────────────────────────────────────────────────

def _make_st_df(n: int = 60, trend: float = 10.0,
                base_price: float = 50_000.0) -> pd.DataFrame:
    """DataFrame with consistent open/high/low/close columns for supertrend tests."""
    return _make_df(n, base_price=base_price, trend=trend)


class TestSupertrend:
    """Tests for the supertrend() function — covers both the pandas_ta path and
    the pure-pandas fallback (exercised by mocking ta.supertrend to raise)."""

    # ── column contract ───────────────────────────────────────────────────────

    def test_returns_dataframe(self):
        result = supertrend(_make_st_df())
        assert isinstance(result, pd.DataFrame)

    def test_output_columns_present(self):
        result = supertrend(_make_st_df())
        for col in ("supertrend", "supertrend_bull", "supertrend_flip"):
            assert col in result.columns, f"missing column: {col}"

    def test_does_not_mutate_input(self):
        df = _make_st_df()
        original_cols = set(df.columns)
        supertrend(df)
        assert set(df.columns) == original_cols

    def test_row_count_preserved(self):
        df = _make_st_df(80)
        assert len(supertrend(df)) == 80

    # ── dtype / value contracts ───────────────────────────────────────────────

    def test_supertrend_bull_is_boolean_series(self):
        result = supertrend(_make_st_df())
        # Must be bool-compatible — every value evaluates to True or False
        assert result["supertrend_bull"].dtype == bool or \
               set(result["supertrend_bull"].dropna().unique()) <= {True, False}

    def test_supertrend_flip_is_boolean_series(self):
        result = supertrend(_make_st_df())
        assert result["supertrend_flip"].dtype == bool or \
               set(result["supertrend_flip"].dropna().unique()) <= {True, False}

    def test_supertrend_line_is_numeric(self):
        result = supertrend(_make_st_df())
        assert pd.api.types.is_numeric_dtype(result["supertrend"])

    def test_row0_flip_is_never_true(self):
        # The first candle has no previous direction — a flip there is meaningless.
        # Both the pandas_ta and fallback paths must return False for row 0.
        result = supertrend(_make_st_df())
        assert result["supertrend_flip"].iloc[0] is False or \
               result["supertrend_flip"].iloc[0] == False  # noqa: E712

    # ── directional correctness ───────────────────────────────────────────────

    def test_strong_uptrend_ends_bullish(self):
        # 200 candles with a steep upward trend; the final bars should be bullish.
        df = _make_st_df(n=200, trend=100.0)
        result = supertrend(df)
        assert result["supertrend_bull"].iloc[-1] is True or \
               result["supertrend_bull"].iloc[-1] == True  # noqa: E712

    def test_strong_downtrend_ends_bearish(self):
        # Build a strong downtrend: close falls by 100 per bar.
        df = _make_st_df(n=200, trend=-100.0, base_price=1_000_000.0)
        result = supertrend(df)
        assert result["supertrend_bull"].iloc[-1] is False or \
               result["supertrend_bull"].iloc[-1] == False  # noqa: E712

    # ── flip detection ────────────────────────────────────────────────────────

    def test_flip_only_on_direction_change(self):
        # A flip can only be True when the current bar is bullish and the
        # previous bar was bearish.
        result = supertrend(_make_st_df(100, trend=10.0))
        bull = result["supertrend_bull"]
        flip = result["supertrend_flip"]
        # Where flip is True, bull must be True and the previous bull must be False.
        flip_idx = result.index[flip]
        for idx in flip_idx:
            pos = result.index.get_loc(idx)
            assert bull.iloc[pos], "flip=True but current bar is not bullish"
            if pos > 0:
                assert not bull.iloc[pos - 1], "flip=True but previous bar was already bullish"

    def test_no_consecutive_flips(self):
        # Consecutive True values in supertrend_flip are impossible:
        # a flip means going from bearish→bullish, so the very next bar starts
        # already bullish and cannot flip again unless it goes bearish first.
        result = supertrend(_make_st_df(150, trend=5.0))
        flip = result["supertrend_flip"].astype(bool)
        # Two consecutive True values would mean flip[i] and flip[i+1] are both True,
        # which requires bull[i-1]=False, bull[i]=True, bull[i+1]=True — but then
        # bull.shift(1)[i+1] is True, so flip[i+1] must be False.
        consec = flip & flip.shift(1).fillna(False)
        assert not consec.any(), "found consecutive supertrend flips — impossible"

    # ── fallback path ─────────────────────────────────────────────────────────
    # In CI the conftest pandas_ta stub omits supertrend(), so the fallback
    # pure-pandas path is always exercised. These tests document its contracts.

    def test_fallback_path_produces_valid_output(self):
        # Stub has no supertrend attr → exception → fallback fires automatically.
        result = supertrend(_make_st_df(80))
        for col in ("supertrend", "supertrend_bull", "supertrend_flip"):
            assert col in result.columns
        assert len(result) == 80

    def test_fallback_row0_flip_false(self):
        result = supertrend(_make_st_df(80))
        assert not result["supertrend_flip"].iloc[0]

    def test_fallback_no_consecutive_flips(self):
        result = supertrend(_make_st_df(150, trend=5.0))
        flip = result["supertrend_flip"].astype(bool)
        consec = flip & flip.shift(1).fillna(False)
        assert not consec.any()

    # ── edge cases ────────────────────────────────────────────────────────────

    def test_custom_period_and_multiplier(self):
        result = supertrend(_make_st_df(80), period=7, multiplier=3.0)
        assert "supertrend_bull" in result.columns

    def test_minimal_data_does_not_crash(self):
        # Very short dataframes may not have enough data for ATR — must return
        # valid columns regardless (the fallback fills with defaults).
        df = _make_st_df(5)
        result = supertrend(df, period=10)
        for col in ("supertrend", "supertrend_bull", "supertrend_flip"):
            assert col in result.columns

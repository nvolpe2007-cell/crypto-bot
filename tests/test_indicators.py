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

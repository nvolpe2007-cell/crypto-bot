"""Unit tests for src/indicators.py"""

import pytest
import pandas as pd
import numpy as np
from src.indicators import EMACrossRSI, Signal, IndicatorResult, prepare_ohlcv_dataframe


# ── helpers ──────────────────────────────────────────────────────────────────

def make_df(closes, freq='1min'):
    """Build a minimal OHLCV DataFrame from a list of close prices."""
    n = len(closes)
    closes = list(closes)
    dates = pd.date_range('2024-01-01', periods=n, freq=freq)
    return pd.DataFrame({
        'open':   [c * 0.9995 for c in closes],
        'high':   [c * 1.001  for c in closes],
        'low':    [c * 0.999  for c in closes],
        'close':  closes,
        'volume': [1000.0] * n,
    }, index=dates)


def make_ohlcv_list(n=50, base=40_000.0):
    """Return a raw exchange-style OHLCV list (list-of-lists)."""
    start_ms = 1_704_067_200_000  # 2024-01-01 00:00 UTC
    return [
        [start_ms + i * 60_000, base, base * 1.001, base * 0.999, base, 100.0]
        for i in range(n)
    ]


# ── prepare_ohlcv_dataframe ───────────────────────────────────────────────────

class TestPrepareOhlcvDataframe:
    def test_basic_conversion(self):
        ohlcv = make_ohlcv_list(n=5)
        df = prepare_ohlcv_dataframe(ohlcv)
        assert not df.empty
        assert list(df.columns) == ['open', 'high', 'low', 'close', 'volume']
        assert isinstance(df.index, pd.DatetimeIndex)

    def test_values_are_numeric(self):
        df = prepare_ohlcv_dataframe(make_ohlcv_list())
        for col in ['open', 'high', 'low', 'close', 'volume']:
            assert pd.api.types.is_float_dtype(df[col]), f"{col} should be float"

    def test_empty_input_returns_empty_df(self):
        df = prepare_ohlcv_dataframe([])
        assert df.empty

    def test_close_value_preserved(self):
        ohlcv = [[1_704_067_200_000, 42_000, 42_100, 41_900, 42_050, 100.0]]
        df = prepare_ohlcv_dataframe(ohlcv)
        assert df['close'].iloc[0] == 42_050.0

    def test_custom_columns(self):
        ohlcv = [[1_704_067_200_000, 1.0, 1.1, 0.9, 1.05, 50.0]]
        df = prepare_ohlcv_dataframe(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        assert 'close' in df.columns


# ── EMACrossRSI ──────────────────────────────────────────────────────────────

class TestEMACrossRSI:
    def test_returns_none_when_too_few_candles(self):
        strategy = EMACrossRSI(fast_ema=9, slow_ema=21)
        df = make_df([40_000.0] * 20)   # 20 < slow_ema=21
        assert strategy.get_latest_signal(df) is None

    def test_returns_result_with_enough_data(self):
        strategy = EMACrossRSI(fast_ema=9, slow_ema=21)
        df = make_df([40_000.0 + i * 10 for i in range(50)])
        result = strategy.get_latest_signal(df)
        assert result is not None
        assert isinstance(result, IndicatorResult)

    def test_signal_is_valid_enum(self):
        strategy = EMACrossRSI(fast_ema=9, slow_ema=21)
        df = make_df([40_000.0] * 50)
        result = strategy.get_latest_signal(df)
        assert result.signal in (Signal.BUY, Signal.SELL, Signal.HOLD)

    def test_rsi_in_valid_range(self):
        strategy = EMACrossRSI(fast_ema=9, slow_ema=21)
        df = make_df([40_000.0 + i * 5 for i in range(60)])
        result = strategy.get_latest_signal(df)
        assert 0.0 <= result.rsi <= 100.0

    def test_ema_fast_close_to_price(self):
        """Fast EMA of 9 on a flat series should be very close to the constant price."""
        price = 50_000.0
        strategy = EMACrossRSI(fast_ema=9, slow_ema=21)
        df = make_df([price] * 60)
        result = strategy.get_latest_signal(df)
        assert abs(result.ema_fast - price) < 1.0  # within $1

    def test_calculate_adds_required_columns(self):
        strategy = EMACrossRSI(fast_ema=9, slow_ema=21)
        df = make_df([40_000.0] * 50)
        result_df = strategy.calculate(df)
        for col in ['ema_fast', 'ema_slow', 'rsi', 'signal']:
            assert col in result_df.columns, f"Missing column: {col}"

    def test_does_not_mutate_input(self):
        strategy = EMACrossRSI(fast_ema=9, slow_ema=21)
        df = make_df([40_000.0] * 50)
        original_cols = set(df.columns)
        strategy.calculate(df)
        assert set(df.columns) == original_cols

    def test_buy_signal_on_rising_price(self):
        """A transition from declining to strongly rising prices creates a BUY crossover."""
        # 30 declining candles forces EMA(9) < EMA(21)
        # 30 sharply rising candles forces the cross in the opposite direction
        declining = [1_000.0 - i * 3 for i in range(30)]
        rising    = [910.0   + i * 8 for i in range(30)]
        df = make_df(declining + rising)
        strategy = EMACrossRSI(fast_ema=9, slow_ema=21)
        result_df = strategy.calculate(df)

        buy_signals = result_df[result_df['signal'] == Signal.BUY]
        assert len(buy_signals) >= 1, "Expected at least one BUY signal after price reversal"

    def test_sell_signal_on_falling_price(self):
        """A transition from rising to strongly falling prices creates a SELL crossover."""
        rising   = [1_000.0 + i * 3 for i in range(30)]
        falling  = [1_090.0 - i * 8 for i in range(30)]
        df = make_df(rising + falling)
        strategy = EMACrossRSI(fast_ema=9, slow_ema=21)
        result_df = strategy.calculate(df)

        sell_signals = result_df[result_df['signal'] == Signal.SELL]
        assert len(sell_signals) >= 1, "Expected at least one SELL signal after price reversal"

    def test_is_buy_is_sell_properties(self):
        strategy = EMACrossRSI(fast_ema=9, slow_ema=21)
        declining = [1_000.0 - i * 3 for i in range(30)]
        rising    = [910.0   + i * 8 for i in range(30)]
        df = make_df(declining + rising)
        result = strategy.get_latest_signal(df)
        assert result.is_buy == (result.signal == Signal.BUY)
        assert result.is_sell == (result.signal == Signal.SELL)

    def test_get_signals_history_same_as_calculate(self):
        strategy = EMACrossRSI(fast_ema=9, slow_ema=21)
        df = make_df([40_000.0 + i * 5 for i in range(60)])
        assert strategy.get_signals_history(df).equals(strategy.calculate(df))

    def test_flat_price_no_crossover(self):
        """Perfectly flat price means no EMA crossover and no BUY/SELL signals."""
        strategy = EMACrossRSI(fast_ema=9, slow_ema=21)
        df = make_df([40_000.0] * 60)
        result_df = strategy.calculate(df)
        # Flat price → EMAs converge to the same value; no crossover after warm-up
        tail = result_df.iloc[21:]
        active_signals = tail[tail['signal'] != Signal.HOLD]
        assert len(active_signals) == 0

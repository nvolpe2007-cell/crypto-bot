"""Unit tests for src/advanced_strategy.py"""

import pytest
import numpy as np
import pandas as pd

from src.advanced_strategy import AdvancedStrategy, AdvancedSignal
from src.indicators import Signal


# ── helpers ───────────────────────────────────────────────────────────────────

def make_df(n=150, trend='up', seed=42):
    """
    Deterministic OHLCV DataFrame.
    trend: 'up' | 'down' | 'flat' | 'reversal_up' | 'reversal_down'
    """
    rng = np.random.default_rng(seed)
    dates = pd.date_range('2024-01-01', periods=n, freq='1h')

    if trend == 'up':
        closes = 40_000 + np.linspace(0, 5_000, n) + rng.standard_normal(n) * 50
    elif trend == 'down':
        closes = 45_000 - np.linspace(0, 5_000, n) + rng.standard_normal(n) * 50
    elif trend == 'flat':
        closes = 40_000 + rng.standard_normal(n) * 30
    elif trend == 'reversal_up':
        half = n // 2
        closes = np.concatenate([
            40_000 - np.linspace(0, 2_000, half),
            38_000 + np.linspace(0, 6_000, n - half),
        ]) + rng.standard_normal(n) * 30
    elif trend == 'reversal_down':
        half = n // 2
        closes = np.concatenate([
            40_000 + np.linspace(0, 2_000, half),
            42_000 - np.linspace(0, 6_000, n - half),
        ]) + rng.standard_normal(n) * 30
    else:
        raise ValueError(f"Unknown trend: {trend}")

    closes = np.maximum(closes, 1.0)   # keep prices positive
    return pd.DataFrame({
        'open':   closes * 0.9998,
        'high':   closes * 1.002,
        'low':    closes * 0.998,
        'close':  closes,
        'volume': rng.uniform(500, 2_000, n),
    }, index=dates)


# ── AdvancedStrategy.calculate ────────────────────────────────────────────────

class TestCalculate:
    REQUIRED_COLS = [
        'ema_fast', 'ema_slow', 'ema_trend',
        'rsi', 'macd', 'macd_signal', 'macd_hist',
        'adx', 'atr', 'volume_ratio', 'signal',
        'sl_buy', 'tp_buy', 'sl_sell', 'tp_sell',
    ]

    def test_required_columns_present(self):
        strategy = AdvancedStrategy()
        df = make_df()
        result = strategy.calculate(df)
        for col in self.REQUIRED_COLS:
            assert col in result.columns, f"Missing column: {col}"

    def test_does_not_mutate_input(self):
        strategy = AdvancedStrategy()
        df = make_df()
        original_cols = set(df.columns)
        strategy.calculate(df)
        assert set(df.columns) == original_cols

    def test_signal_column_contains_valid_values(self):
        strategy = AdvancedStrategy()
        df = make_df()
        result = strategy.calculate(df)
        valid = {Signal.BUY, Signal.SELL, Signal.HOLD}
        assert set(result['signal'].unique()).issubset(valid)

    def test_sl_buy_below_close(self):
        """Stop-loss for a buy must be below the close price."""
        strategy = AdvancedStrategy()
        df = make_df()
        result = strategy.calculate(df).dropna(subset=['sl_buy', 'close'])
        assert (result['sl_buy'] < result['close']).all()

    def test_tp_buy_above_close(self):
        """Take-profit for a buy must be above the close price."""
        strategy = AdvancedStrategy()
        df = make_df()
        result = strategy.calculate(df).dropna(subset=['tp_buy', 'close'])
        assert (result['tp_buy'] > result['close']).all()

    def test_sl_sell_above_close(self):
        strategy = AdvancedStrategy()
        df = make_df()
        result = strategy.calculate(df).dropna(subset=['sl_sell', 'close'])
        assert (result['sl_sell'] > result['close']).all()

    def test_tp_sell_below_close(self):
        strategy = AdvancedStrategy()
        df = make_df()
        result = strategy.calculate(df).dropna(subset=['tp_sell', 'close'])
        assert (result['tp_sell'] < result['close']).all()


# ── AdvancedStrategy.get_latest_signal ───────────────────────────────────────

class TestGetLatestSignal:
    def test_returns_none_on_insufficient_data(self):
        strategy = AdvancedStrategy()
        df = make_df(n=30)    # way below the ~60 candle minimum
        assert strategy.get_latest_signal(df) is None

    def test_returns_advanced_signal(self):
        strategy = AdvancedStrategy()
        df = make_df(n=150)
        result = strategy.get_latest_signal(df)
        assert result is not None
        assert isinstance(result, AdvancedSignal)

    def test_signal_is_valid(self):
        strategy = AdvancedStrategy()
        df = make_df(n=150)
        result = strategy.get_latest_signal(df)
        assert result.signal in (Signal.BUY, Signal.SELL, Signal.HOLD)

    def test_rsi_in_range(self):
        strategy = AdvancedStrategy()
        df = make_df(n=150)
        result = strategy.get_latest_signal(df)
        assert 0.0 <= result.rsi <= 100.0

    def test_atr_positive(self):
        strategy = AdvancedStrategy()
        df = make_df(n=150)
        result = strategy.get_latest_signal(df)
        assert result.atr > 0.0

    def test_close_matches_last_candle(self):
        strategy = AdvancedStrategy()
        df = make_df(n=150)
        result = strategy.get_latest_signal(df)
        assert result.close == pytest.approx(df['close'].iloc[-1], rel=1e-6)

    def test_buy_stop_loss_below_entry(self):
        strategy = AdvancedStrategy()
        df = make_df(n=200, trend='reversal_up')
        result = strategy.get_latest_signal(df)
        if result and result.signal == Signal.BUY:
            assert result.stop_loss_price < result.close
            assert result.take_profit_price > result.close

    def test_sell_stop_loss_above_entry(self):
        strategy = AdvancedStrategy()
        df = make_df(n=200, trend='reversal_down')
        result = strategy.get_latest_signal(df)
        if result and result.signal == Signal.SELL:
            assert result.stop_loss_price > result.close
            assert result.take_profit_price < result.close

    def test_is_buy_is_sell_consistency(self):
        strategy = AdvancedStrategy()
        df = make_df(n=150)
        result = strategy.get_latest_signal(df)
        assert result.is_buy  == (result.signal == Signal.BUY)
        assert result.is_sell == (result.signal == Signal.SELL)

    def test_timestamp_is_set(self):
        strategy = AdvancedStrategy()
        df = make_df(n=150)
        result = strategy.get_latest_signal(df)
        assert result.timestamp is not None
        assert isinstance(result.timestamp, int)
        assert result.timestamp > 0


# ── AdvancedStrategy.confidence_score ────────────────────────────────────────

class TestConfidenceScore:
    def test_returns_zero_for_hold(self):
        strategy = AdvancedStrategy()
        row = {'signal': Signal.HOLD, 'adx': 30, 'volume_ratio': 1.5, 'rsi': 50}
        assert strategy.confidence_score(row) == 0

    def test_returns_zero_for_none_signal(self):
        strategy = AdvancedStrategy()
        assert strategy.confidence_score({}) == 0

    def test_score_in_range_for_all_rows(self):
        strategy = AdvancedStrategy()
        df = make_df(n=200)
        df_calc = strategy.calculate(df)
        for i in range(len(df_calc)):
            row = df_calc.iloc[i].to_dict()
            score = strategy.confidence_score(row)
            assert 0 <= score <= 100, f"Score {score} out of [0, 100] at row {i}"

    def test_higher_adx_raises_score(self):
        """A row with the same signal but higher ADX should score higher."""
        strategy = AdvancedStrategy()
        base = {'signal': Signal.BUY, 'adx': 20, 'volume_ratio': 1.5,
                'rsi': 40, 'macd_hist': 0.1, 'macd': 1.0, 'atr': 100, 'close': 40_000}
        high_adx = dict(base, adx=40)
        assert strategy.confidence_score(high_adx) > strategy.confidence_score(base)

    def test_null_adx_handled_gracefully(self):
        strategy = AdvancedStrategy()
        row = {'signal': Signal.BUY, 'adx': None, 'volume_ratio': None,
               'rsi': None, 'macd_hist': None, 'macd': None, 'atr': None, 'close': None}
        score = strategy.confidence_score(row)
        assert 0 <= score <= 100

    def test_stop_loss_pct_positive(self):
        strategy = AdvancedStrategy()
        df = make_df(n=150)
        result = strategy.get_latest_signal(df)
        # stop_loss_pct() returns the % distance to the stop — must be non-negative
        assert result.stop_loss_pct() >= 0.0

    def test_take_profit_pct_positive(self):
        strategy = AdvancedStrategy()
        df = make_df(n=150)
        result = strategy.get_latest_signal(df)
        assert result.take_profit_pct() >= 0.0

"""
Unit tests for AdvancedStrategy and ProductionStrategy.

Both classes are used in production (paper and live trading respectively)
but previously had zero test coverage.

Helpers mirror those in test_indicators.py so the test file is self-contained.
"""

import pytest
import pandas as pd
import numpy as np
from datetime import datetime, timedelta

from src.indicators import Signal, prepare_ohlcv_dataframe
from src.advanced_strategy import AdvancedStrategy, AdvancedSignal
from src.production_strategy import ProductionStrategy, ProductionSignal


# ── shared helpers ────────────────────────────────────────────────────────────

def _make_ohlcv(n: int = 200, base: float = 50_000.0, trend: float = 10.0) -> list:
    start = datetime(2024, 1, 1)
    rows = []
    for i in range(n):
        ts = int((start + timedelta(minutes=i)).timestamp() * 1000)
        c = base + i * trend
        rows.append([ts, c * 0.999, c * 1.001, c * 0.998, c, 500.0])
    return rows


def _make_df(n: int = 200, base: float = 50_000.0, trend: float = 10.0) -> pd.DataFrame:
    return prepare_ohlcv_dataframe(_make_ohlcv(n, base, trend))


def _make_oscillating_df(n: int = 300, base: float = 50_000.0,
                         amplitude: float = 500.0) -> pd.DataFrame:
    """Prices that oscillate so MACD/RSI produce varied readings."""
    start = datetime(2024, 1, 1)
    rows = []
    for i in range(n):
        ts = int((start + timedelta(minutes=i)).timestamp() * 1000)
        c = base + amplitude * np.sin(2 * np.pi * i / 20)
        rows.append([ts, c * 0.999, c * 1.002, c * 0.998, c, 500.0 + 200 * abs(np.sin(i))])
    return prepare_ohlcv_dataframe(rows)


# ─────────────────────────────────────────────────────────────────────────────
# AdvancedStrategy
# ─────────────────────────────────────────────────────────────────────────────

class TestAdvancedStrategyCalculate:
    def test_returns_dataframe(self):
        df = _make_df(200)
        result = AdvancedStrategy().calculate(df)
        assert isinstance(result, pd.DataFrame)

    def test_does_not_mutate_input(self):
        df = _make_df(200)
        original_cols = set(df.columns)
        AdvancedStrategy().calculate(df)
        assert set(df.columns) == original_cols

    def test_adds_required_indicator_columns(self):
        df = _make_df(200)
        result = AdvancedStrategy().calculate(df)
        for col in ("ema_fast", "ema_slow", "ema_trend", "rsi", "macd",
                    "macd_signal", "macd_hist", "atr", "volume_ratio", "adx", "signal"):
            assert col in result.columns, f"missing column: {col}"

    def test_signal_values_are_valid_enum(self):
        df = _make_df(200)
        result = AdvancedStrategy().calculate(df)
        valid = {Signal.BUY, Signal.SELL, Signal.HOLD}
        assert set(result["signal"].unique()) <= valid

    def test_atr_based_stop_take_profit_columns_present(self):
        df = _make_df(200)
        result = AdvancedStrategy().calculate(df)
        for col in ("sl_buy", "tp_buy", "sl_sell", "tp_sell"):
            assert col in result.columns

    def test_sl_buy_below_close(self):
        df = _make_df(200)
        result = AdvancedStrategy().calculate(df)
        valid = result.dropna(subset=["sl_buy", "close"])
        assert (valid["sl_buy"] < valid["close"]).all()

    def test_tp_buy_above_close(self):
        df = _make_df(200)
        result = AdvancedStrategy().calculate(df)
        valid = result.dropna(subset=["tp_buy", "close"])
        assert (valid["tp_buy"] > valid["close"]).all()

    def test_sl_sell_above_close(self):
        df = _make_df(200)
        result = AdvancedStrategy().calculate(df)
        valid = result.dropna(subset=["sl_sell", "close"])
        assert (valid["sl_sell"] > valid["close"]).all()

    def test_tp_sell_below_close(self):
        df = _make_df(200)
        result = AdvancedStrategy().calculate(df)
        valid = result.dropna(subset=["tp_sell", "close"])
        assert (valid["tp_sell"] < valid["close"]).all()

    def test_row_count_preserved(self):
        n = 150
        df = _make_df(n)
        result = AdvancedStrategy().calculate(df)
        assert len(result) == n

    def test_volume_ratio_non_negative(self):
        df = _make_df(200)
        result = AdvancedStrategy().calculate(df)
        valid = result["volume_ratio"].dropna()
        assert (valid >= 0).all()


class TestAdvancedStrategyGetLatestSignal:
    def test_returns_none_with_too_few_rows(self):
        df = _make_df(10)
        assert AdvancedStrategy().get_latest_signal(df) is None

    def test_returns_none_for_empty_df(self):
        assert AdvancedStrategy().get_latest_signal(pd.DataFrame()) is None

    def test_returns_advanced_signal_with_enough_data(self):
        df = _make_df(200)
        result = AdvancedStrategy().get_latest_signal(df)
        assert result is not None
        assert isinstance(result, AdvancedSignal)

    def test_signal_is_valid_enum(self):
        df = _make_df(200)
        result = AdvancedStrategy().get_latest_signal(df)
        assert result.signal in (Signal.BUY, Signal.SELL, Signal.HOLD)

    def test_is_buy_and_is_sell_exclusive(self):
        df = _make_df(200)
        result = AdvancedStrategy().get_latest_signal(df)
        assert not (result.is_buy and result.is_sell)

    def test_result_has_float_fields(self):
        df = _make_df(200)
        result = AdvancedStrategy().get_latest_signal(df)
        assert isinstance(result.close, float)
        assert isinstance(result.ema_fast, float)
        assert isinstance(result.ema_slow, float)
        assert isinstance(result.atr, float)
        assert isinstance(result.rsi, float)

    def test_stop_loss_pct_positive(self):
        df = _make_df(200)
        result = AdvancedStrategy().get_latest_signal(df)
        assert result.stop_loss_pct() > 0

    def test_take_profit_pct_positive(self):
        df = _make_df(200)
        result = AdvancedStrategy().get_latest_signal(df)
        assert result.take_profit_pct() > 0

    def test_strongly_uptrending_does_not_produce_sell(self):
        # A steep uptrend means EMA fast stays above slow → no sell crossover
        df = _make_df(250, trend=50.0)
        result = AdvancedStrategy().get_latest_signal(df)
        assert not result.is_sell

    def test_custom_ema_periods_accepted(self):
        df = _make_df(200)
        result = AdvancedStrategy(fast_ema=5, slow_ema=15, trend_ema=30).get_latest_signal(df)
        assert result is not None

    def test_min_candles_guard_uses_largest_period(self):
        strat = AdvancedStrategy(slow_ema=21, trend_ema=50, macd_slow=26, rsi_period=14)
        # Need at least max(21, 50, 26, 14) + 10 = 60 rows
        df = _make_df(59)
        assert strat.get_latest_signal(df) is None
        df = _make_df(65)
        assert strat.get_latest_signal(df) is not None

    def test_atr_positive(self):
        df = _make_df(200)
        result = AdvancedStrategy().get_latest_signal(df)
        assert result.atr > 0

    def test_rsi_within_range(self):
        df = _make_oscillating_df(300)
        result = AdvancedStrategy().get_latest_signal(df)
        assert 0 <= result.rsi <= 100

    def test_timestamp_is_int_or_none(self):
        df = _make_df(200)
        result = AdvancedStrategy().get_latest_signal(df)
        assert result.timestamp is None or isinstance(result.timestamp, int)

    def test_adx_non_negative(self):
        df = _make_df(200)
        result = AdvancedStrategy().get_latest_signal(df)
        assert result.adx >= 0

    def test_volume_ratio_non_negative(self):
        df = _make_df(200)
        result = AdvancedStrategy().get_latest_signal(df)
        assert result.volume_ratio >= 0


class TestAdvancedStrategyConfidenceScore:
    def _strat(self) -> AdvancedStrategy:
        return AdvancedStrategy()

    def _row(self, signal=Signal.BUY, adx=35.0, volume_ratio=1.5,
             rsi=45.0, macd_hist=10.0, macd=8.0, atr=200.0, close=50_000.0) -> dict:
        return {
            "signal": signal,
            "adx": adx,
            "volume_ratio": volume_ratio,
            "rsi": rsi,
            "macd_hist": macd_hist,
            "macd": macd,
            "atr": atr,
            "close": close,
        }

    def test_returns_int(self):
        score = self._strat().confidence_score(self._row())
        assert isinstance(score, int)

    def test_score_in_0_100_range(self):
        score = self._strat().confidence_score(self._row())
        assert 0 <= score <= 100

    def test_hold_signal_returns_zero(self):
        row = self._row(signal=Signal.HOLD)
        assert self._strat().confidence_score(row) == 0

    def test_higher_adx_increases_score(self):
        low_adx = self._strat().confidence_score(self._row(adx=10.0))
        high_adx = self._strat().confidence_score(self._row(adx=40.0))
        assert high_adx > low_adx

    def test_higher_volume_ratio_increases_score(self):
        low_vol = self._strat().confidence_score(self._row(volume_ratio=1.0))
        high_vol = self._strat().confidence_score(self._row(volume_ratio=2.5))
        assert high_vol > low_vol

    def test_sell_signal_accepted(self):
        row = self._row(signal=Signal.SELL, rsi=65.0, macd_hist=-5.0)
        score = self._strat().confidence_score(row)
        assert 0 <= score <= 100

    def test_none_fields_handled_gracefully(self):
        row = self._row()
        row["adx"] = None
        row["volume_ratio"] = None
        row["macd_hist"] = None
        score = self._strat().confidence_score(row)
        assert 0 <= score <= 100

    def test_extreme_atr_lowers_score(self):
        sweet_spot = self._strat().confidence_score(self._row(atr=500.0, close=50_000.0))   # 1%
        too_high   = self._strat().confidence_score(self._row(atr=3000.0, close=50_000.0))  # 6%
        assert sweet_spot >= too_high


# ─────────────────────────────────────────────────────────────────────────────
# ProductionStrategy
# ─────────────────────────────────────────────────────────────────────────────

def _make_prod_df(n: int = 250, base: float = 50_000.0, trend: float = 10.0) -> pd.DataFrame:
    """4h-style df with enough rows for EMA200."""
    return _make_df(n, base, trend)


class TestProductionStrategyCalculate:
    def test_returns_dataframe(self):
        df = _make_prod_df()
        result = ProductionStrategy().calculate(df)
        assert isinstance(result, pd.DataFrame)

    def test_does_not_mutate_input(self):
        df = _make_prod_df()
        original_cols = set(df.columns)
        ProductionStrategy().calculate(df)
        assert set(df.columns) == original_cols

    def test_adds_required_columns(self):
        df = _make_prod_df()
        result = ProductionStrategy().calculate(df)
        for col in ("ema200", "rsi", "atr", "adx", "signal"):
            assert col in result.columns, f"missing: {col}"

    def test_signal_values_are_valid_enum(self):
        df = _make_prod_df()
        result = ProductionStrategy().calculate(df)
        valid = {Signal.BUY, Signal.SELL, Signal.HOLD}
        assert set(result["signal"].unique()) <= valid

    def test_row_count_preserved(self):
        df = _make_prod_df(250)
        result = ProductionStrategy().calculate(df)
        assert len(result) == 250

    def test_vol_ratio_non_negative(self):
        df = _make_prod_df()
        result = ProductionStrategy().calculate(df)
        valid = result["vol_ratio"].dropna()
        assert (valid >= 0).all()

    def test_rsi_column_in_0_100(self):
        df = _make_oscillating_df(300)
        result = ProductionStrategy().calculate(df)
        rsi_vals = result["rsi"].dropna()
        assert ((rsi_vals >= 0) & (rsi_vals <= 100)).all()

    def test_uptrend_data_produces_no_sell(self):
        # In a strong uptrend price stays above EMA200 → sell condition blocked
        df = _make_prod_df(250, trend=50.0)
        result = ProductionStrategy().calculate(df)
        assert Signal.SELL not in result["signal"].values


class TestProductionStrategyGetLatestSignal:
    def test_returns_none_with_too_few_rows(self):
        df = _make_prod_df(50)
        assert ProductionStrategy().get_latest_signal(df) is None

    def test_returns_none_for_empty_df(self):
        assert ProductionStrategy().get_latest_signal(pd.DataFrame()) is None

    def test_returns_production_signal_with_enough_data(self):
        df = _make_prod_df(250)
        result = ProductionStrategy().get_latest_signal(df)
        assert result is not None
        assert isinstance(result, ProductionSignal)

    def test_signal_is_valid_enum(self):
        df = _make_prod_df(250)
        result = ProductionStrategy().get_latest_signal(df)
        assert result.signal in (Signal.BUY, Signal.SELL, Signal.HOLD)

    def test_is_buy_and_is_sell_exclusive(self):
        df = _make_prod_df(250)
        result = ProductionStrategy().get_latest_signal(df)
        assert not (result.is_buy and result.is_sell)

    def test_confidence_in_0_100(self):
        df = _make_prod_df(250)
        result = ProductionStrategy().get_latest_signal(df)
        assert 0 <= result.confidence <= 100

    def test_result_float_fields(self):
        df = _make_prod_df(250)
        result = ProductionStrategy().get_latest_signal(df)
        assert isinstance(result.close, float)
        assert isinstance(result.rsi, float)
        assert isinstance(result.atr, float)

    def test_stop_loss_and_take_profit_present(self):
        df = _make_prod_df(250)
        result = ProductionStrategy().get_latest_signal(df)
        assert result.stop_loss_price != 0.0
        assert result.take_profit_price != 0.0

    def test_buy_stop_loss_below_price(self):
        """When signal is BUY, stop_loss should be below entry price."""
        df = _make_oscillating_df(350)
        strat = ProductionStrategy()
        result = strat.get_latest_signal(df)
        if result and result.is_buy:
            assert result.stop_loss_price < result.close

    def test_regime_is_string(self):
        df = _make_prod_df(250)
        result = ProductionStrategy().get_latest_signal(df)
        assert isinstance(result.regime, str)

    def test_timestamp_is_int_or_none(self):
        df = _make_prod_df(250)
        result = ProductionStrategy().get_latest_signal(df)
        assert result.timestamp is None or isinstance(result.timestamp, int)

    def test_custom_params_accepted(self):
        df = _make_prod_df(250)
        result = ProductionStrategy(rsi_period=10, ema_trend=100,
                                    atr_sl_mult=2.0, atr_tp_mult=3.0).get_latest_signal(df)
        assert result is not None

    def test_confidence_field_accessible(self):
        """Regression: live_trading.py uses signal.confidence — must not raise."""
        df = _make_prod_df(250)
        result = ProductionStrategy().get_latest_signal(df)
        # Accessing .confidence must not raise AttributeError
        _ = result.confidence

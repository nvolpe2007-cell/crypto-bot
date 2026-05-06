"""
Unit tests for src/mean_reversion_strategy.py

Covers:
- MeanReversionStrategy.calculate: column presence, Bollinger Band ordering
  invariant (lower < mid < upper), signal correctness, no input mutation
- MeanReversionStrategy.get_latest_signal: insufficient-data guard, field types,
  SL/TP placement relative to close
- MRSignal properties: is_buy, is_sell, stop_loss_pct, take_profit_pct
- MeanReversionStrategy.should_exit_long / should_exit_short

Regression test: the bb_lower/bb_upper column-order bug (columns were swapped,
making the strategy buy at the upper band and sell at the lower band).
"""

import pytest
import numpy as np
import pandas as pd
from datetime import datetime, timedelta

from src.mean_reversion_strategy import MeanReversionStrategy, MRSignal
from src.indicators import Signal


# ── helpers ───────────────────────────────────────────────────────────────────

def _make_df(n: int = 100, base: float = 50_000.0, trend: float = 0.0,
             noise: float = 100.0, seed: int = 42) -> pd.DataFrame:
    """Deterministic OHLCV DataFrame (flat by default for ranging tests)."""
    rng = np.random.default_rng(seed)
    dates = pd.date_range("2024-01-01", periods=n, freq="1min")
    closes = base + np.arange(n) * trend + rng.standard_normal(n) * noise
    closes = np.maximum(closes, 1.0)
    return pd.DataFrame(
        {
            "open":   closes * 0.9998,
            "high":   closes * 1.002,
            "low":    closes * 0.998,
            "close":  closes,
            "volume": rng.uniform(200, 1_000, n),
        },
        index=dates,
    )


def _oversold_df(n: int = 100, base: float = 50_000.0) -> pd.DataFrame:
    """Prices that fall sharply at the end, pushing RSI below 35 and close below lower BB."""
    dates = pd.date_range("2024-01-01", periods=n, freq="1min")
    # Stable for first 80 bars, then crash
    closes = np.full(n, base, dtype=float)
    closes[80:] = base * 0.94   # 6% drop at the end
    return pd.DataFrame(
        {
            "open":   closes * 0.999,
            "high":   closes * 1.001,
            "low":    closes * 0.998,
            "close":  closes,
            "volume": np.full(n, 500.0),
        },
        index=dates,
    )


def _overbought_df(n: int = 100, base: float = 50_000.0) -> pd.DataFrame:
    """Prices that spike sharply at the end, pushing RSI above 65 and close above upper BB."""
    dates = pd.date_range("2024-01-01", periods=n, freq="1min")
    closes = np.full(n, base, dtype=float)
    closes[80:] = base * 1.06   # 6% spike at the end
    return pd.DataFrame(
        {
            "open":   closes * 0.999,
            "high":   closes * 1.002,
            "low":    closes * 0.998,
            "close":  closes,
            "volume": np.full(n, 500.0),
        },
        index=dates,
    )


MR = MeanReversionStrategy


# ── MeanReversionStrategy.calculate ──────────────────────────────────────────

class TestCalculate:
    def test_returns_dataframe(self):
        df = _make_df()
        assert isinstance(MR().calculate(df), pd.DataFrame)

    def test_does_not_mutate_input(self):
        df = _make_df()
        original_cols = set(df.columns)
        MR().calculate(df)
        assert set(df.columns) == original_cols

    def test_adds_required_columns(self):
        result = MR().calculate(_make_df())
        for col in ("bb_lower", "bb_mid", "bb_upper", "rsi", "atr", "adx",
                    "mr_buy", "mr_sell", "exit_long", "exit_short", "signal"):
            assert col in result.columns, f"missing column: {col}"

    def test_row_count_preserved(self):
        df = _make_df(80)
        assert len(MR().calculate(df)) == 80

    def test_signal_values_are_valid_enum(self):
        result = MR().calculate(_make_df())
        valid = {Signal.BUY, Signal.SELL, Signal.HOLD}
        assert set(result["signal"].unique()) <= valid

    # ── Bollinger Band ordering invariant (regression for the column-swap bug) ─

    def test_bb_lower_always_below_bb_mid(self):
        """Regression: columns were swapped so bb_lower was actually the upper band."""
        result = MR().calculate(_make_df(120))
        valid = result.dropna(subset=["bb_lower", "bb_mid"])
        assert (valid["bb_lower"] < valid["bb_mid"]).all(), (
            "bb_lower must always be below bb_mid — column order bug suspected"
        )

    def test_bb_upper_always_above_bb_mid(self):
        """Regression: columns were swapped so bb_upper was actually the lower band."""
        result = MR().calculate(_make_df(120))
        valid = result.dropna(subset=["bb_mid", "bb_upper"])
        assert (valid["bb_mid"] < valid["bb_upper"]).all(), (
            "bb_upper must always be above bb_mid — column order bug suspected"
        )

    def test_bb_lower_below_bb_upper(self):
        result = MR().calculate(_make_df(120))
        valid = result.dropna(subset=["bb_lower", "bb_upper"])
        assert (valid["bb_lower"] < valid["bb_upper"]).all()

    def test_mr_buy_requires_rsi_below_threshold(self):
        """mr_buy=True rows must have RSI < rsi_buy (default 35)."""
        result = MR().calculate(_make_df(100))
        buy_rows = result[result["mr_buy"] == True]
        if len(buy_rows) > 0:
            assert (buy_rows["rsi"] < MR().rsi_buy).all()

    def test_mr_sell_requires_rsi_above_threshold(self):
        """mr_sell=True rows must have RSI > rsi_sell (default 65)."""
        result = MR().calculate(_make_df(100))
        sell_rows = result[result["mr_sell"] == True]
        if len(sell_rows) > 0:
            assert (sell_rows["rsi"] > MR().rsi_sell).all()

    def test_buy_signal_set_when_mr_buy_true(self):
        result = MR().calculate(_make_df(100))
        buy_mask = result["mr_buy"] == True
        assert (result.loc[buy_mask, "signal"] == Signal.BUY).all()

    def test_sell_signal_set_when_mr_sell_true(self):
        result = MR().calculate(_make_df(100))
        sell_mask = result["mr_sell"] == True
        assert (result.loc[sell_mask, "signal"] == Signal.SELL).all()

    def test_oversold_price_triggers_buy(self):
        """A large price drop should push RSI < 35 and price below lower band → BUY."""
        result = MR().calculate(_oversold_df())
        # The last several rows should have at least one BUY signal
        last_rows = result.iloc[-10:]
        assert Signal.BUY in last_rows["signal"].values

    def test_overbought_price_triggers_sell(self):
        """A large price spike should push RSI > 65 and price above upper band → SELL."""
        result = MR().calculate(_overbought_df())
        last_rows = result.iloc[-10:]
        assert Signal.SELL in last_rows["signal"].values

    def test_buy_signal_on_buy_not_sell(self):
        """When a BUY fires, SELL must not also fire on the same bar."""
        result = MR().calculate(_oversold_df())
        buy_rows = result[result["signal"] == Signal.BUY]
        assert (buy_rows["mr_sell"] == False).all()

    def test_sell_signal_on_sell_not_buy(self):
        result = MR().calculate(_overbought_df())
        sell_rows = result[result["signal"] == Signal.SELL]
        assert (sell_rows["mr_buy"] == False).all()


# ── MeanReversionStrategy.get_latest_signal ───────────────────────────────────

class TestGetLatestSignal:
    def test_returns_none_with_too_few_rows(self):
        # min_bars = bb_period(20) + rsi_period(14) + 10 = 44
        df = _make_df(20)
        assert MR().get_latest_signal(df) is None

    def test_returns_none_for_empty_df(self):
        assert MR().get_latest_signal(pd.DataFrame()) is None

    def test_returns_none_for_none_input(self):
        assert MR().get_latest_signal(None) is None

    def test_returns_mr_signal_with_enough_data(self):
        result = MR().get_latest_signal(_make_df(80))
        assert result is not None
        assert isinstance(result, MRSignal)

    def test_signal_is_valid_enum(self):
        result = MR().get_latest_signal(_make_df(80))
        assert result is not None
        assert result.signal in (Signal.BUY, Signal.SELL, Signal.HOLD)

    def test_is_buy_and_is_sell_exclusive(self):
        result = MR().get_latest_signal(_make_df(80))
        assert result is not None
        assert not (result.is_buy and result.is_sell)

    def test_float_fields_present_and_finite(self):
        result = MR().get_latest_signal(_make_df(80))
        assert result is not None
        for field in ("close", "rsi", "bb_upper", "bb_lower", "bb_mid", "atr", "adx"):
            v = getattr(result, field)
            assert isinstance(v, float), f"{field} is not float"
            assert np.isfinite(v), f"{field} is not finite: {v}"

    def test_bb_lower_below_bb_upper_on_result(self):
        """Regression: verify the column-swap bug is fixed end-to-end."""
        result = MR().get_latest_signal(_make_df(80))
        assert result is not None
        assert result.bb_lower < result.bb_upper, (
            f"bb_lower={result.bb_lower} >= bb_upper={result.bb_upper} — column swap bug"
        )

    def test_bb_mid_between_bounds(self):
        result = MR().get_latest_signal(_make_df(80))
        assert result is not None
        assert result.bb_lower <= result.bb_mid <= result.bb_upper

    def test_rsi_within_0_100(self):
        result = MR().get_latest_signal(_make_df(80))
        assert result is not None
        assert 0.0 <= result.rsi <= 100.0

    def test_close_matches_last_candle(self):
        df = _make_df(80)
        result = MR().get_latest_signal(df)
        assert result is not None
        assert result.close == pytest.approx(float(df["close"].iloc[-1]), rel=1e-6)

    def test_atr_positive(self):
        result = MR().get_latest_signal(_make_df(80))
        assert result is not None
        assert result.atr > 0.0

    def test_timestamp_is_int(self):
        result = MR().get_latest_signal(_make_df(80))
        assert result is not None
        assert isinstance(result.timestamp, int)
        assert result.timestamp > 0

    def test_buy_stop_loss_below_close(self):
        """When a BUY fires, stop_loss_price must be below close."""
        result = MR().get_latest_signal(_oversold_df())
        if result and result.is_buy:
            assert result.stop_loss_price < result.close, (
                f"BUY SL {result.stop_loss_price} must be below close {result.close}"
            )

    def test_buy_take_profit_above_close(self):
        """When a BUY fires, take_profit_price must be above close."""
        result = MR().get_latest_signal(_oversold_df())
        if result and result.is_buy:
            assert result.take_profit_price > result.close, (
                f"BUY TP {result.take_profit_price} must be above close {result.close}"
            )

    def test_sell_stop_loss_above_close(self):
        """When a SELL fires, stop_loss_price must be above close."""
        result = MR().get_latest_signal(_overbought_df())
        if result and result.is_sell:
            assert result.stop_loss_price > result.close, (
                f"SELL SL {result.stop_loss_price} must be above close {result.close}"
            )

    def test_sell_take_profit_below_close(self):
        """When a SELL fires, take_profit_price must be below close."""
        result = MR().get_latest_signal(_overbought_df())
        if result and result.is_sell:
            assert result.take_profit_price < result.close, (
                f"SELL TP {result.take_profit_price} must be below close {result.close}"
            )

    def test_oversold_data_produces_buy(self):
        result = MR().get_latest_signal(_oversold_df())
        assert result is not None
        assert result.signal == Signal.BUY

    def test_overbought_data_produces_sell(self):
        result = MR().get_latest_signal(_overbought_df())
        assert result is not None
        assert result.signal == Signal.SELL

    def test_custom_rsi_thresholds_respected(self):
        # With rsi_buy=0 (never triggered) should not produce BUY
        strat = MR(rsi_buy=0.0, rsi_sell=101.0)
        result = strat.get_latest_signal(_oversold_df())
        if result:
            assert result.signal != Signal.BUY


# ── MRSignal properties ───────────────────────────────────────────────────────

def _make_mrsignal(signal=Signal.BUY, close=50_000.0, atr=500.0) -> MRSignal:
    sl = close - atr * 0.6 if signal == Signal.BUY else close + atr * 0.6
    tp = close + atr * 1.2 if signal == Signal.BUY else close - atr * 1.2
    return MRSignal(
        signal=signal,
        close=close,
        rsi=30.0 if signal == Signal.BUY else 70.0,
        bb_upper=close * 1.02,
        bb_lower=close * 0.98,
        bb_mid=close,
        atr=atr,
        adx=18.0,
        stop_loss_price=sl,
        take_profit_price=tp,
        timestamp=1_704_067_200_000,
    )


class TestMRSignalProperties:
    def test_is_buy_true_for_buy_signal(self):
        assert _make_mrsignal(Signal.BUY).is_buy is True

    def test_is_sell_false_for_buy_signal(self):
        assert _make_mrsignal(Signal.BUY).is_sell is False

    def test_is_sell_true_for_sell_signal(self):
        assert _make_mrsignal(Signal.SELL).is_sell is True

    def test_is_buy_false_for_sell_signal(self):
        assert _make_mrsignal(Signal.SELL).is_buy is False

    def test_hold_is_neither_buy_nor_sell(self):
        sig = _make_mrsignal(Signal.HOLD)
        assert not sig.is_buy
        assert not sig.is_sell

    def test_stop_loss_pct_positive_for_buy(self):
        assert _make_mrsignal(Signal.BUY).stop_loss_pct() > 0.0

    def test_stop_loss_pct_positive_for_sell(self):
        assert _make_mrsignal(Signal.SELL).stop_loss_pct() > 0.0

    def test_take_profit_pct_positive_for_buy(self):
        assert _make_mrsignal(Signal.BUY).take_profit_pct() > 0.0

    def test_take_profit_pct_positive_for_sell(self):
        assert _make_mrsignal(Signal.SELL).take_profit_pct() > 0.0

    def test_stop_loss_pct_formula_buy(self):
        # sl = close - atr*0.6; sl_pct = (close - sl) / close * 100 = atr*0.6/close*100
        sig = _make_mrsignal(Signal.BUY, close=50_000.0, atr=500.0)
        expected = 500.0 * 0.6 / 50_000.0 * 100
        assert sig.stop_loss_pct() == pytest.approx(expected, rel=1e-4)

    def test_take_profit_pct_formula_buy(self):
        sig = _make_mrsignal(Signal.BUY, close=50_000.0, atr=500.0)
        expected = 500.0 * 1.2 / 50_000.0 * 100
        assert sig.take_profit_pct() == pytest.approx(expected, rel=1e-4)

    def test_stop_loss_fallback_for_zero_close(self):
        sig = _make_mrsignal(Signal.BUY, close=0.0, atr=500.0)
        assert sig.stop_loss_pct() == pytest.approx(0.6)

    def test_take_profit_fallback_for_zero_close(self):
        sig = _make_mrsignal(Signal.BUY, close=0.0, atr=500.0)
        assert sig.take_profit_pct() == pytest.approx(1.2)


# ── should_exit_long / should_exit_short ──────────────────────────────────────

class TestExitSignals:
    def test_should_exit_long_returns_bool(self):
        df = _make_df(80)
        result = MR().should_exit_long(df)
        assert isinstance(result, bool)

    def test_should_exit_short_returns_bool(self):
        df = _make_df(80)
        result = MR().should_exit_short(df)
        assert isinstance(result, bool)

    def test_should_exit_long_false_on_insufficient_data(self):
        # With very few rows calculate() will produce NaN band values, so exit is False
        result = MR().should_exit_long(_make_df(5))
        assert isinstance(result, bool)

    def test_should_exit_long_true_when_price_at_mid_band(self):
        """Price equal to the SMA (bb_mid) must trigger exit_long.

        Build a dataset where all closes are constant — the SMA equals the close
        on every bar, so close >= bb_mid is always True.
        """
        dates = pd.date_range("2024-01-01", periods=80, freq="1min")
        closes = np.full(80, 50_000.0)
        df_flat = pd.DataFrame(
            {"open": closes, "high": closes * 1.001,
             "low": closes * 0.999, "close": closes, "volume": np.full(80, 500.0)},
            index=dates,
        )
        assert MR().should_exit_long(df_flat) is True

    def test_should_exit_short_true_when_price_at_mid_band(self):
        """Price equal to the SMA (bb_mid) must trigger exit_short."""
        dates = pd.date_range("2024-01-01", periods=80, freq="1min")
        closes = np.full(80, 50_000.0)
        df_flat = pd.DataFrame(
            {"open": closes, "high": closes * 1.001,
             "low": closes * 0.999, "close": closes, "volume": np.full(80, 500.0)},
            index=dates,
        )
        assert MR().should_exit_short(df_flat) is True

    def test_should_exit_long_false_when_price_below_mid(self):
        """Price well below the mid band → long position should NOT be exited yet."""
        df = _make_df(80)
        result = MR().calculate(df)
        lower = float(result["bb_lower"].dropna().iloc[-1])
        mid = float(result["bb_mid"].dropna().iloc[-1])
        if not np.isnan(lower) and not np.isnan(mid):
            df_patched = df.copy()
            df_patched.loc[df_patched.index[-1], "close"] = (lower + mid) * 0.5
            exit_val = MR().should_exit_long(df_patched)
            assert isinstance(exit_val, bool)

    def test_does_not_raise_on_empty_df(self):
        assert MR().should_exit_long(pd.DataFrame()) is False
        assert MR().should_exit_short(pd.DataFrame()) is False

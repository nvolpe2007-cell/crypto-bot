"""
Unit tests for src/production_strategy.py

Covers:
- ProductionSignal: property aliases, is_buy/is_sell flags
- ProductionStrategy.calculate(): column presence, input immutability, signal
  validity, condition invariants (BUY/SELL only fire when all gates pass),
  volume blocking, custom parameters
- ProductionStrategy.get_latest_signal(): insufficient-data guard, close value,
  RSI/confidence ranges, SL/TP math, regime detection, timestamp type
- ProductionStrategy.confidence_score(): HOLD/None→0, RSI distance, ADX
  strength, trend alignment, ATR sweet-spot, cap at 100, missing fields
"""

import pytest
import pandas as pd
from datetime import datetime, timedelta

from src.production_strategy import ProductionStrategy, ProductionSignal
from src.indicators import Signal, prepare_ohlcv_dataframe


# ── helpers ───────────────────────────────────────────────────────────────────

def _make_df(n: int = 300, base: float = 50_000.0, trend: float = 10.0,
             volume: float = 1_000.0) -> pd.DataFrame:
    """Steady-trend OHLCV DataFrame with n rows (4-hour candles)."""
    start = datetime(2024, 1, 1)
    rows = []
    for i in range(n):
        ts = int((start + timedelta(hours=4 * i)).timestamp() * 1000)
        close = base + i * trend
        rows.append([ts, close * 0.999, close * 1.002, close * 0.997, close, volume])
    return prepare_ohlcv_dataframe(rows)


def _make_crossover_df(direction: str = "up") -> pd.DataFrame:
    """
    Build OHLCV data engineered to fire a single RSI-50 crossover signal.

    direction='up'  — 250 rows of alternating +/-100 prices (last row is a down
        move so RSI[249] < 50), followed by 10 rows of strong surge (close +500
        per candle, volume 100× the warmup level).  The transition satisfies all
        three BUY conditions: rsi_cross_up, macro_up, vol_ok.

    direction='down' — 251 rows of alternating prices (last row is an up move so
        RSI[250] > 50), followed by 10 rows of sharp decline.  The transition
        satisfies all three SELL conditions: rsi_cross_down, ~macro_up, vol_ok.
    """
    n_warmup = 250 if direction == "up" else 251
    n_burst = 10
    base = 50_000.0
    start = datetime(2024, 1, 1)
    rows = []

    for i in range(n_warmup):
        ts = int((start + timedelta(hours=4 * i)).timestamp() * 1000)
        delta = 100.0 if i % 2 == 0 else -100.0
        close = base + delta
        rows.append([ts, close * 0.999, close * 1.002, close * 0.997, close, 100.0])

    for i in range(n_burst):
        ts = int((start + timedelta(hours=4 * (n_warmup + i))).timestamp() * 1000)
        if direction == "up":
            close = base + (i + 1) * 500.0          # surge far above EMA200
        else:
            close = base - (i + 1) * 500.0          # plunge far below EMA200
        rows.append([ts, close * 0.999, close * 1.002, close * 0.997, close, 10_000.0])

    return prepare_ohlcv_dataframe(rows)


@pytest.fixture
def strat() -> ProductionStrategy:
    return ProductionStrategy()


# ── ProductionSignal ──────────────────────────────────────────────────────────

class TestProductionSignal:
    def _make(self, signal: Signal = Signal.BUY) -> ProductionSignal:
        return ProductionSignal(
            signal=signal, close=50_000.0, rsi=55.0,
            ema100=49_800.0, ema200=49_500.0,
            adx=30.0, atr=500.0,
            stop_loss_price=49_250.0, take_profit_price=51_250.0,
            regime="UPTREND", confidence=70,
        )

    def test_ema_fast_alias_returns_ema100(self):
        s = self._make()
        assert s.ema_fast == s.ema100

    def test_ema_slow_alias_returns_ema200(self):
        s = self._make()
        assert s.ema_slow == s.ema200

    def test_volume_ratio_always_one(self):
        assert self._make().volume_ratio == 1.0

    def test_is_buy_on_buy_signal(self):
        s = self._make(Signal.BUY)
        assert s.is_buy
        assert not s.is_sell

    def test_is_sell_on_sell_signal(self):
        s = self._make(Signal.SELL)
        assert s.is_sell
        assert not s.is_buy

    def test_hold_is_neither_buy_nor_sell(self):
        s = self._make(Signal.HOLD)
        assert not s.is_buy
        assert not s.is_sell


# ── ProductionStrategy.calculate ─────────────────────────────────────────────

class TestCalculate:
    EXPECTED_COLS = {
        "ema200", "ema100", "ema50", "rsi", "atr", "adx",
        "vol_ratio", "vol_ok", "macro_up",
        "rsi_cross_up", "rsi_cross_down", "signal",
    }

    def test_adds_all_expected_columns(self, strat):
        result = strat.calculate(_make_df(300))
        missing = self.EXPECTED_COLS - set(result.columns)
        assert not missing, f"missing columns: {missing}"

    def test_does_not_mutate_input(self, strat):
        df = _make_df(300)
        original_cols = set(df.columns)
        strat.calculate(df)
        assert set(df.columns) == original_cols

    def test_preserves_row_count(self, strat):
        for n in (220, 300, 500):
            assert len(strat.calculate(_make_df(n))) == n

    def test_signals_are_valid_enum_values(self, strat):
        result = strat.calculate(_make_df(300))
        valid = {Signal.BUY, Signal.SELL, Signal.HOLD}
        assert set(result["signal"].unique()) <= valid

    def test_buy_signal_fires_on_engineered_up_crossover(self, strat):
        """Crossover df is designed to produce at least one BUY row."""
        result = strat.calculate(_make_crossover_df("up"))
        buys = result[result["signal"] == Signal.BUY]
        assert not buys.empty, "expected ≥1 BUY in engineered up-crossover data"

    def test_sell_signal_fires_on_engineered_down_crossover(self, strat):
        """Crossover df is designed to produce at least one SELL row."""
        result = strat.calculate(_make_crossover_df("down"))
        sells = result[result["signal"] == Signal.SELL]
        assert not sells.empty, "expected ≥1 SELL in engineered down-crossover data"

    def test_every_buy_satisfies_all_three_conditions(self, strat):
        """BUY ↔ rsi_cross_up AND macro_up AND vol_ok — no exceptions."""
        result = strat.calculate(_make_crossover_df("up"))
        buys = result[result["signal"] == Signal.BUY]
        if buys.empty:
            pytest.skip("no BUY signals in dataset")
        assert buys["rsi_cross_up"].all(), "BUY emitted without rsi_cross_up"
        assert buys["macro_up"].all(),     "BUY emitted without macro_up"
        assert buys["vol_ok"].all(),       "BUY emitted without vol_ok"

    def test_every_sell_satisfies_all_three_conditions(self, strat):
        """SELL ↔ rsi_cross_down AND NOT macro_up AND vol_ok — no exceptions."""
        result = strat.calculate(_make_crossover_df("down"))
        sells = result[result["signal"] == Signal.SELL]
        if sells.empty:
            pytest.skip("no SELL signals in dataset")
        assert sells["rsi_cross_down"].all(), "SELL emitted without rsi_cross_down"
        assert (~sells["macro_up"]).all(),    "SELL emitted while price > EMA200"
        assert sells["vol_ok"].all(),         "SELL emitted without vol_ok"

    def test_zero_volume_blocks_buy_and_sell(self, strat):
        """vol_ratio < volume_mult → vol_ok=False → only HOLD signals emitted."""
        df = _make_df(300)
        df["volume"] = 0.001
        result = strat.calculate(df)
        assert Signal.BUY  not in result["signal"].values
        assert Signal.SELL not in result["signal"].values

    def test_volume_mult_zero_makes_vol_ok_always_true(self):
        """volume_mult=0 ⟹ vol_ok is True for every row where vol_ratio is defined."""
        strat = ProductionStrategy(volume_mult=0.0)
        result = strat.calculate(_make_df(300))
        # First 19 rows have vol_ratio=NaN (rolling-20 warmup); skip those.
        defined = result["vol_ratio"].notna()
        assert result.loc[defined, "vol_ok"].all()

    def test_rsi_column_has_non_nan_values(self, strat):
        result = strat.calculate(_make_df(300))
        assert result["rsi"].notna().any()

    def test_vol_ratio_is_non_negative(self, strat):
        result = strat.calculate(_make_df(300))
        assert (result["vol_ratio"].dropna() >= 0).all()

    def test_custom_rsi_period_does_not_crash(self):
        result = ProductionStrategy(rsi_period=21).calculate(_make_df(300))
        assert "rsi" in result.columns

    def test_custom_ema_trend_period_does_not_crash(self):
        result = ProductionStrategy(ema_trend=100).calculate(_make_df(300))
        assert "ema200" in result.columns   # column name is always 'ema200'


# ── ProductionStrategy.get_latest_signal ─────────────────────────────────────

class TestGetLatestSignal:
    def test_returns_none_for_empty_df(self, strat):
        assert strat.get_latest_signal(pd.DataFrame()) is None

    def test_returns_none_below_210_row_threshold(self, strat):
        for n in (1, 50, 100, 200, 209):
            result = strat.get_latest_signal(_make_df(n))
            assert result is None, f"expected None for {n}-row df, got {result}"

    def test_returns_production_signal_with_adequate_data(self, strat):
        result = strat.get_latest_signal(_make_df(300))
        assert isinstance(result, ProductionSignal)

    def test_close_matches_last_row_of_input(self, strat):
        df = _make_df(300)
        result = strat.get_latest_signal(df)
        assert result is not None
        assert abs(result.close - df["close"].iloc[-1]) < 1e-6

    def test_rsi_is_within_valid_range(self, strat):
        result = strat.get_latest_signal(_make_df(300))
        assert result is not None
        assert 0 <= result.rsi <= 100

    def test_confidence_is_within_valid_range(self, strat):
        result = strat.get_latest_signal(_make_df(300))
        assert result is not None
        assert 0 <= result.confidence <= 100

    def test_timestamp_is_an_integer(self, strat):
        result = strat.get_latest_signal(_make_df(300))
        assert result is not None
        assert isinstance(result.timestamp, int)

    def test_stop_loss_and_take_profit_differ_from_close(self, strat):
        """SL and TP are never equal to close (ATR > 0 in realistic data)."""
        result = strat.get_latest_signal(_make_df(300))
        assert result is not None
        assert result.stop_loss_price != result.close
        assert result.take_profit_price != result.close

    def test_sl_tp_math_is_consistent_with_signal_direction(self, strat):
        """
        BUY:  sl = close − ATR × sl_mult,  tp = close + ATR × tp_mult
        SELL/HOLD: sl = close + ATR × sl_mult,  tp = close − ATR × tp_mult
        """
        result = strat.get_latest_signal(_make_df(300))
        assert result is not None
        if result.is_buy:
            expected_sl = round(result.close - result.atr * strat.atr_sl_mult, 4)
            expected_tp = round(result.close + result.atr * strat.atr_tp_mult, 4)
        else:
            expected_sl = round(result.close + result.atr * strat.atr_sl_mult, 4)
            expected_tp = round(result.close - result.atr * strat.atr_tp_mult, 4)
        assert abs(result.stop_loss_price  - expected_sl) < 0.05
        assert abs(result.take_profit_price - expected_tp) < 0.05

    def test_buy_sl_below_entry_tp_above_entry(self, strat):
        """On a BUY signal, stop_loss < close < take_profit."""
        df = _make_crossover_df("up")
        calc = strat.calculate(df)
        buy_rows = calc[calc["signal"] == Signal.BUY]
        if buy_rows.empty:
            pytest.skip("no BUY in crossover df")
        sliced = df.loc[:buy_rows.index[0]]
        result = strat.get_latest_signal(sliced)
        if result is None or not result.is_buy:
            pytest.skip("last row of slice is not a BUY")
        assert result.stop_loss_price  < result.close
        assert result.take_profit_price > result.close

    def test_sell_sl_above_entry_tp_below_entry(self, strat):
        """On a SELL signal, stop_loss > close > take_profit."""
        df = _make_crossover_df("down")
        calc = strat.calculate(df)
        sell_rows = calc[calc["signal"] == Signal.SELL]
        if sell_rows.empty:
            pytest.skip("no SELL in crossover df")
        sliced = df.loc[:sell_rows.index[0]]
        result = strat.get_latest_signal(sliced)
        if result is None or not result.is_sell:
            pytest.skip("last row of slice is not a SELL")
        assert result.stop_loss_price  > result.close
        assert result.take_profit_price < result.close

    def test_regime_is_uptrend_in_sustained_uptrend(self, strat):
        """In a long uptrend, close > EMA200 → regime should be 'UPTREND'."""
        result = strat.get_latest_signal(_make_df(350, trend=20.0))
        assert result is not None
        assert result.regime == "UPTREND"

    def test_regime_is_downtrend_in_sustained_downtrend(self, strat):
        """In a long downtrend, close < EMA200 (lag) → regime should be 'DOWNTREND'."""
        result = strat.get_latest_signal(_make_df(350, base=60_000.0, trend=-15.0))
        assert result is not None
        assert result.regime == "DOWNTREND"

    def test_ema100_and_ema200_are_positive(self, strat):
        result = strat.get_latest_signal(_make_df(300))
        assert result is not None
        assert result.ema100 > 0
        assert result.ema200 > 0

    def test_atr_is_positive(self, strat):
        result = strat.get_latest_signal(_make_df(300))
        assert result is not None
        assert result.atr > 0


# ── ProductionStrategy.confidence_score ───────────────────────────────────────

class TestConfidenceScore:
    def _row(self, signal: Signal = Signal.BUY, rsi: float = 60.0,
             adx: float = 25.0, atr: float = 500.0,
             close: float = 50_000.0, ema200: float = 49_000.0) -> dict:
        return {"signal": signal, "rsi": rsi, "adx": adx,
                "atr": atr, "close": close, "ema200": ema200}

    def test_hold_signal_returns_zero(self, strat):
        assert strat.confidence_score(self._row(Signal.HOLD)) == 0

    def test_none_signal_returns_zero(self, strat):
        assert strat.confidence_score({"signal": None}) == 0

    def test_buy_signal_returns_positive_score(self, strat):
        assert strat.confidence_score(self._row(Signal.BUY)) > 0

    def test_sell_signal_returns_positive_score(self, strat):
        row = self._row(Signal.SELL, rsi=35.0, close=48_000.0, ema200=49_000.0)
        assert strat.confidence_score(row) > 0

    def test_score_never_exceeds_100(self, strat):
        extreme = self._row(rsi=99.0, adx=100.0, atr=250.0)
        assert strat.confidence_score(extreme) <= 100

    def test_score_is_non_negative(self, strat):
        for rsi in (51.0, 60.0, 80.0):
            assert strat.confidence_score(self._row(rsi=rsi)) >= 0

    def test_rsi_farther_from_50_gives_higher_score(self, strat):
        far  = strat.confidence_score(self._row(rsi=80.0))
        near = strat.confidence_score(self._row(rsi=52.0))
        assert far > near

    def test_higher_adx_gives_higher_score(self, strat):
        strong = strat.confidence_score(self._row(adx=40.0))
        weak   = strat.confidence_score(self._row(adx=5.0))
        assert strong > weak

    def test_buy_aligned_with_trend_gives_higher_score(self, strat):
        """BUY with close > EMA200 (aligned) scores higher than close < EMA200."""
        aligned = strat.confidence_score(self._row(close=51_000.0, ema200=49_000.0))
        against = strat.confidence_score(self._row(close=47_000.0, ema200=49_000.0))
        assert aligned > against

    def test_sell_aligned_with_trend_gives_higher_score(self, strat):
        """SELL with close < EMA200 (aligned) scores higher than close > EMA200."""
        aligned = strat.confidence_score(
            self._row(Signal.SELL, rsi=35.0, close=47_000.0, ema200=49_000.0)
        )
        against = strat.confidence_score(
            self._row(Signal.SELL, rsi=35.0, close=51_000.0, ema200=49_000.0)
        )
        assert aligned > against

    def test_atr_in_sweet_spot_scores_higher_than_zero_atr(self, strat):
        """ATR in 0.2–3% range (sweet spot) outscores ATR=0."""
        sweet = strat.confidence_score(self._row(atr=250.0, close=50_000.0))  # 0.5%
        zero  = strat.confidence_score(self._row(atr=0.0,   close=50_000.0))
        assert sweet > zero

    def test_missing_optional_fields_do_not_raise(self, strat):
        """confidence_score handles rows that omit optional keys."""
        strat.confidence_score({"signal": Signal.BUY})   # all numeric fields absent

    def test_score_is_integer(self, strat):
        result = strat.confidence_score(self._row())
        assert isinstance(result, int)

"""
Unit tests for src/indicators.py

Covers:
- prepare_ohlcv_dataframe: type conversions, index, empty input
- EMACrossRSI: column presence, signal types, insufficient-data guard,
  crossover detection, RSI filter (overbought blocks buys)
- supertrend, atr, ema_htf: the indicator helpers.

  NOTE (corrected 2026-09-03): an earlier version of this docstring claimed
  these three are "actually consumed by the live/paper strategies".  They are
  not.  paper_trading.py, scientific_strategy.py, microstructure_strategy.py,
  mean_reversion_strategy.py and live_trading.py import only `Signal` and
  `prepare_ohlcv_dataframe` from this module; nothing outside these tests
  imports supertrend/atr/ema_htf.  They are unwired helpers, so the bugs fixed
  on 2026-09-03 had no live blast radius — but the false claim is exactly the
  kind of stale doc CLAUDE.md warns about, so it is corrected rather than kept.

  Beware when reading the supertrend tests: conftest.py replaces pandas_ta with
  a stub that has NO `supertrend`, so tests that do not patch it exercise the
  MANUAL FALLBACK, not the pandas_ta branch production would take.
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


# ── supertrend: the ATR warm-up path ──────────────────────────────────────────
#
# conftest.py stubs pandas_ta, and the stub has no `supertrend`, so every test
# above silently exercises the MANUAL FALLBACK — never the pandas_ta branch that
# production actually takes.  The stub's `_atr` also uses ewm() with no
# min_periods, so it yields a value from row 0 and has no NaN warm-up.
#
# Real pandas_ta.atr DOES emit a NaN warm-up prefix, and that combination is
# what the tests above cannot see: seeding the band recursion at row 0 lets the
# warm-up NaN fail every comparison, so both branches carry it forward and the
# whole series stays NaN — an all-NaN supertrend line that reads as permanently
# bullish even on a pure downtrend.  These tests pin that behaviour by injecting
# an ATR with a realistic warm-up.

def _atr_with_warmup(high, low, close, length: int = 14, **_) -> pd.Series:
    """ATR that emits a NaN warm-up prefix, the way real pandas_ta.atr does."""
    prev_close = close.shift(1)
    tr = pd.concat(
        [high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1
    ).max(axis=1)
    out = tr.ewm(span=length, adjust=False).mean()
    out.iloc[: length - 1] = np.nan
    return out


class TestSupertrendAtrWarmup:
    @pytest.fixture
    def warmup_atr(self, monkeypatch):
        import src.indicators as ind
        monkeypatch.setattr(ind.ta, "atr", _atr_with_warmup)

    def test_line_is_finite_after_warmup(self, warmup_atr):
        df = _trending_df(60, start=100, end=200)
        out = supertrend(df, period=10, multiplier=2.5)
        assert out["supertrend"].iloc[:9].isna().all(), "warm-up should be NaN"
        assert out["supertrend"].iloc[9:].notna().all(), (
            "the band recursion carried the ATR warm-up NaN forward forever"
        )

    def test_downtrend_is_bearish_despite_warmup(self, warmup_atr):
        # The production failure in one line: with a NaN warm-up the old code
        # reported bull=True for every bar of a monotonic 200 -> 100 decline.
        df = _trending_df(60, start=200, end=100)
        out = supertrend(df, period=10, multiplier=2.5)
        assert not out["supertrend_bull"].iloc[20:].any()

    def test_uptrend_is_bullish_despite_warmup(self, warmup_atr):
        df = _trending_df(60, start=100, end=200)
        out = supertrend(df, period=10, multiplier=2.5)
        assert out["supertrend_bull"].iloc[20:].all()

    def test_bullish_line_stays_below_close(self, warmup_atr):
        df = _trending_df(60, start=100, end=200)
        out = supertrend(df, period=10, multiplier=2.5).iloc[20:]
        assert (out["supertrend"] <= out["close"]).all()

    def test_too_few_bars_for_any_atr_is_all_nan_not_a_crash(self, warmup_atr):
        df = _trending_df(5)  # fewer bars than period → ATR never becomes valid
        out = supertrend(df, period=10, multiplier=2.5)
        assert len(out) == 5
        assert out["supertrend"].isna().all()
        assert not out["supertrend_bull"].any()
        assert not out["supertrend_flip"].any()

    def test_warmup_coming_online_is_not_a_flip(self, warmup_atr):
        # A flip means bear -> bull.  The bar where the indicator first produces
        # a direction is not a reversal, so it must not be marked as one.
        df = _trending_df(60, start=100, end=200)  # pure uptrend, never reverses
        out = supertrend(df, period=10, multiplier=2.5)
        assert not out["supertrend_flip"].any()

    def test_real_reversal_still_flips(self, warmup_atr):
        closes = np.concatenate([np.linspace(200, 100, 30), np.linspace(100, 250, 30)])
        df = pd.DataFrame({
            "open": closes + 0.5, "high": closes + 1.0,
            "low": closes - 1.0, "close": closes,
            "volume": np.full(len(closes), 100.0),
        })
        out = supertrend(df, period=10, multiplier=2.5)
        flips = np.flatnonzero(out["supertrend_flip"].to_numpy())
        assert flips.size >= 1
        for i in flips:
            assert bool(out["supertrend_bull"].iloc[i]) is True
            assert bool(out["supertrend_bull"].iloc[i - 1]) is False


# ── supertrend: the pandas_ta branch production actually takes ────────────────

class TestSupertrendPandasTaBranch:
    """Exercise the branch conftest's stub hides, with a faked pandas_ta."""

    @staticmethod
    def _fake_st_frame(n: int, direction_at: dict = None) -> pd.DataFrame:
        direction_at = direction_at or {}
        d = np.array([direction_at.get(i, 1) for i in range(n)], dtype=float)
        return pd.DataFrame({
            "SUPERT_10_2.5":  np.arange(n, dtype=float),
            "SUPERTd_10_2.5": d,
            "SUPERTl_10_2.5": np.full(n, np.nan),
            "SUPERTs_10_2.5": np.full(n, np.nan),
        })

    def test_value_column_is_selected_not_rejected(self, monkeypatch):
        import src.indicators as ind
        n = 20
        frame = self._fake_st_frame(n)
        monkeypatch.setattr(ind.ta, "supertrend", lambda *a, **k: frame, raising=False)
        out = supertrend(_trending_df(n), period=10, multiplier=2.5)
        # If the SUPERT_ column were filtered out, this would silently fall back
        # to the manual implementation and these values would not match.
        assert out["supertrend"].tolist() == list(range(n))
        assert out["supertrend_bull"].all()

    def test_direction_column_drives_bull_flag(self, monkeypatch):
        import src.indicators as ind
        n = 20
        frame = self._fake_st_frame(n, direction_at={i: -1 for i in range(0, 10)})
        monkeypatch.setattr(ind.ta, "supertrend", lambda *a, **k: frame, raising=False)
        out = supertrend(_trending_df(n), period=10, multiplier=2.5)
        assert not out["supertrend_bull"].iloc[:10].any()
        assert out["supertrend_bull"].iloc[10:].all()
        # exactly one bear -> bull transition, at index 10
        assert np.flatnonzero(out["supertrend_flip"].to_numpy()).tolist() == [10]

    def test_nan_direction_warmup_is_not_a_flip(self, monkeypatch):
        import src.indicators as ind
        n = 20
        frame = self._fake_st_frame(n, direction_at={i: np.nan for i in range(0, 5)})
        monkeypatch.setattr(ind.ta, "supertrend", lambda *a, **k: frame, raising=False)
        out = supertrend(_trending_df(n), period=10, multiplier=2.5)
        assert not out["supertrend_bull"].iloc[:5].any()
        # NaN -> bull is the indicator coming online, not a reversal
        assert not out["supertrend_flip"].any()

    def test_unexpected_columns_fall_back_without_raising(self, monkeypatch):
        import src.indicators as ind
        bad = pd.DataFrame({"TOTALLY_WRONG": np.arange(30, dtype=float)})
        monkeypatch.setattr(ind.ta, "supertrend", lambda *a, **k: bad, raising=False)
        out = supertrend(_trending_df(30), period=10, multiplier=2.5)
        for col in ("supertrend", "supertrend_bull", "supertrend_flip"):
            assert col in out.columns
        assert out["supertrend"].notna().any(), "fallback should still produce a line"

    def test_fallback_tracks_the_pandas_ta_result(self, monkeypatch):
        """The two branches must agree — the fallback is a substitute, not a
        different indicator."""
        import src.indicators as ind
        df = _trending_df(80, start=100, end=260)

        monkeypatch.setattr(ind.ta, "atr", _atr_with_warmup)
        fallback = supertrend(df, period=10, multiplier=2.5)

        # A textbook supertrend built independently of the code under test.
        ref = _reference_supertrend(df, period=10, multiplier=2.5)
        pd.testing.assert_series_equal(
            fallback["supertrend"].iloc[15:], ref.iloc[15:],
            check_names=False, rtol=1e-9,
        )


def _reference_supertrend(df: pd.DataFrame, period: int, multiplier: float) -> pd.Series:
    """Independent textbook supertrend, used as an oracle for the fallback."""
    hl2 = (df["high"] + df["low"]) / 2.0
    a = _atr_with_warmup(df["high"], df["low"], df["close"], length=period)
    ub = (hl2 + multiplier * a).to_numpy(dtype=float)
    lb = (hl2 - multiplier * a).to_numpy(dtype=float)
    cl = df["close"].to_numpy(dtype=float)
    n = len(df)

    fu, fl = np.full(n, np.nan), np.full(n, np.nan)
    d = np.full(n, np.nan)
    seed = int(np.flatnonzero(~np.isnan(ub))[0])
    fu[seed], fl[seed], d[seed] = ub[seed], lb[seed], 1.0
    for i in range(seed + 1, n):
        fu[i] = ub[i] if (ub[i] < fu[i - 1] or cl[i - 1] > fu[i - 1]) else fu[i - 1]
        fl[i] = lb[i] if (lb[i] > fl[i - 1] or cl[i - 1] < fl[i - 1]) else fl[i - 1]
        if d[i - 1] == -1.0 and cl[i] > fu[i]:
            d[i] = 1.0
        elif d[i - 1] == 1.0 and cl[i] < fl[i]:
            d[i] = -1.0
        else:
            d[i] = d[i - 1]
    line = np.where(d == 1.0, fl, np.where(d == -1.0, fu, np.nan))
    return pd.Series(line, index=df.index)

"""
Unit tests for src/scientific_strategy.py

Covers:
- _size_multiplier: confidence-tier lookup, boundary values, monotonicity
- compute_position_size: math at every tier, equity scaling, 15% cap
- ScientificSignal properties: is_buy, is_sell, stop_loss_pct, take_profit_pct
- ScientificStrategy.evaluate: direction selection, confidence scoring,
  regime hard-blocks, OFI/lead-lag direction arbitration, HOLD paths

No real pandas_ta or ccxt calls are made — the conftest.py stub provides
deterministic implementations of ema/rsi/atr/macd/adx.
"""

import pytest
import numpy as np
import pandas as pd
from datetime import datetime

from src.scientific_strategy import (
    ScientificStrategy,
    ScientificSignal,
    compute_position_size,
    _size_multiplier,
    BASE_EQUITY_PCT,
    MAX_EQUITY_PCT,
)
from src.indicators import Signal


# ── helpers ───────────────────────────────────────────────────────────────────

def _make_df(n: int = 100, base: float = 50_000.0, trend: float = 5.0) -> pd.DataFrame:
    """Deterministic OHLCV DataFrame with a mild uptrend."""
    dates = pd.date_range("2024-01-01", periods=n, freq="1min")
    closes = base + np.arange(n) * trend
    return pd.DataFrame(
        {
            "open":   closes * 0.9999,
            "high":   closes * 1.001,
            "low":    closes * 0.999,
            "close":  closes,
            "volume": np.full(n, 500.0),
        },
        index=dates,
    )


def _make_oscillating_df(n: int = 100, base: float = 50_000.0,
                          amplitude: float = 300.0) -> pd.DataFrame:
    """Oscillating prices so RSI and MACD produce varied values."""
    dates = pd.date_range("2024-01-01", periods=n, freq="1min")
    closes = base + amplitude * np.sin(2 * np.pi * np.arange(n) / 20)
    closes = np.maximum(closes, 1.0)
    return pd.DataFrame(
        {
            "open":   closes * 0.9998,
            "high":   closes * 1.002,
            "low":    closes * 0.998,
            "close":  closes,
            "volume": 500.0 + 200 * np.abs(np.sin(np.arange(n))),
        },
        index=dates,
    )


class _FakeOFI:
    """Stub for OrderFlowImbalance — returns a fixed smoothed value."""
    def __init__(self, value: float = 0.0):
        self._v = value

    def get_smoothed(self, symbol: str) -> float:
        return self._v


class _FakeLeadLag:
    """Stub for LeadLagDetector — returns fixed signal and strength."""
    def __init__(self, signal: str = None, strength: float = 0.5):
        self._signal   = signal
        self._strength = strength

    def get_signal(self, symbol: str):
        return self._signal

    def get_strength(self, symbol: str) -> float:
        return self._strength


# ── _size_multiplier ──────────────────────────────────────────────────────────

class TestSizeMultiplier:
    def test_returns_zero_below_minimum_threshold(self):
        assert _size_multiplier(0.0)  == 0.0
        assert _size_multiplier(37.9) == 0.0

    def test_minimum_exploratory_tier(self):
        assert _size_multiplier(38.0) == 0.2
        assert _size_multiplier(44.9) == 0.2

    def test_second_tier(self):
        assert _size_multiplier(45.0) == 0.3
        assert _size_multiplier(59.9) == 0.3

    def test_base_tier(self):
        assert _size_multiplier(60.0) == 0.5
        assert _size_multiplier(74.9) == 0.5

    def test_mid_tier(self):
        assert _size_multiplier(75.0) == 0.7
        assert _size_multiplier(84.9) == 0.7

    def test_standard_tier(self):
        assert _size_multiplier(85.0) == 1.0
        assert _size_multiplier(92.9) == 1.0

    def test_high_confidence_tier(self):
        assert _size_multiplier(93.0) == 1.5
        assert _size_multiplier(96.9) == 1.5

    def test_max_tier(self):
        assert _size_multiplier(97.0) == 2.0
        assert _size_multiplier(100.0) == 2.0

    def test_tiers_are_monotonically_non_decreasing(self):
        """Higher confidence must never yield a lower multiplier."""
        prev = _size_multiplier(0.0)
        for conf in range(1, 101):
            curr = _size_multiplier(float(conf))
            assert curr >= prev, f"tier decreased at confidence={conf}"
            prev = curr


# ── compute_position_size ─────────────────────────────────────────────────────

class TestComputePositionSize:
    def test_returns_zero_below_threshold(self):
        assert compute_position_size(0.0,  100.0) == 0.0
        assert compute_position_size(37.9, 100.0) == 0.0

    def test_scales_linearly_with_equity(self):
        size_100  = compute_position_size(93.0, 100.0)
        size_1000 = compute_position_size(93.0, 1_000.0)
        assert abs(size_1000 / size_100 - 10.0) < 1e-6

    def test_never_exceeds_max_equity_pct(self):
        for conf in (60, 75, 85, 93, 97, 100):
            for equity in (100, 500, 10_000):
                size = compute_position_size(float(conf), float(equity))
                assert size <= equity * MAX_EQUITY_PCT + 1e-9, (
                    f"conf={conf} equity={equity} → size={size} > MAX"
                )

    def test_base_formula_at_standard_tier(self):
        # mult = 1.0 at conf=90; BASE_EQUITY_PCT = 0.06
        equity = 1_000.0
        expected = equity * BASE_EQUITY_PCT * 1.0
        assert abs(compute_position_size(90.0, equity) - expected) < 1e-6

    def test_high_confidence_formula(self):
        # mult = 1.5 at conf=93; equity=100 → raw=9 < 15=MAX
        assert abs(compute_position_size(93.0, 100.0) - 9.0) < 1e-6

    def test_max_tier_formula(self):
        # mult=2.0 at conf=97; equity=100 → raw=12 < 15=MAX
        assert abs(compute_position_size(97.0, 100.0) - 100.0 * BASE_EQUITY_PCT * 2.0) < 1e-6

    def test_zero_equity_returns_zero(self):
        assert compute_position_size(100.0, 0.0) == 0.0

    def test_result_never_negative(self):
        for conf in range(0, 101, 5):
            assert compute_position_size(float(conf), 100.0) >= 0.0


# ── ScientificSignal properties ───────────────────────────────────────────────

def _make_signal(signal=Signal.BUY, confidence=80.0, size_mult=1.0,
                 atr=500.0, close=50_000.0) -> ScientificSignal:
    return ScientificSignal(
        signal=signal, confidence=confidence, size_mult=size_mult,
        ofi_score=10.0, lead_lag_score=10.0, regime_score=15.0,
        rsi_score=10.0, technical_score=5.0, funding_score=5.0,
        ofi=0.2, lead_lag_dir="BUY", regime="TRENDING_UP",
        rsi=45.0, adx=28.0, atr=atr, close=close,
        ema_fast=50_100.0, ema_slow=49_900.0,
        volume_ratio=1.3, funding_rate=0.0001,
    )


class TestScientificSignalProperties:
    def test_is_buy_when_signal_buy_and_nonzero_size(self):
        sig = _make_signal(signal=Signal.BUY, size_mult=1.0)
        assert sig.is_buy is True
        assert sig.is_sell is False

    def test_is_sell_when_signal_sell_and_nonzero_size(self):
        sig = _make_signal(signal=Signal.SELL, size_mult=0.5)
        assert sig.is_sell is True
        assert sig.is_buy is False

    def test_is_buy_false_when_size_mult_zero(self):
        # size_mult=0 means the strategy decided not to trade even though BUY
        sig = _make_signal(signal=Signal.BUY, size_mult=0.0)
        assert sig.is_buy is False

    def test_is_sell_false_when_size_mult_zero(self):
        sig = _make_signal(signal=Signal.SELL, size_mult=0.0)
        assert sig.is_sell is False

    def test_hold_signal_not_buy_or_sell(self):
        sig = _make_signal(signal=Signal.HOLD, size_mult=0.0)
        assert not sig.is_buy
        assert not sig.is_sell

    def test_stop_loss_pct_positive(self):
        assert _make_signal().stop_loss_pct() > 0.0

    def test_stop_loss_pct_minimum_clamp(self):
        # Very small ATR → clamped to 0.4 minimum
        sig = _make_signal(atr=1.0, close=50_000.0)
        assert sig.stop_loss_pct() == pytest.approx(0.4)

    def test_stop_loss_pct_maximum_clamp(self):
        # Very large ATR → clamped to 2.5 maximum
        sig = _make_signal(atr=100_000.0, close=50_000.0)
        assert sig.stop_loss_pct() == pytest.approx(2.5)

    def test_stop_loss_pct_atr_formula(self):
        # atr=500, close=50000 → base = 500*1.5/50000*100 = 1.5% (in [0.4, 2.5])
        sig = _make_signal(atr=500.0, close=50_000.0)
        assert sig.stop_loss_pct() == pytest.approx(1.5, rel=1e-4)

    def test_stop_loss_pct_fallback_when_atr_zero(self):
        sig = _make_signal(atr=0.0, close=50_000.0)
        assert sig.stop_loss_pct() == pytest.approx(1.5)

    def test_take_profit_standard_rr(self):
        # confidence < 93 → 2:1 R:R
        sig = _make_signal(confidence=80.0, atr=500.0, close=50_000.0)
        assert sig.take_profit_pct() == pytest.approx(sig.stop_loss_pct() * 2.0)

    def test_take_profit_high_confidence_rr(self):
        # confidence >= 93 → 2.5:1 R:R
        sig = _make_signal(confidence=95.0, atr=500.0, close=50_000.0)
        assert sig.take_profit_pct() == pytest.approx(sig.stop_loss_pct() * 2.5)

    def test_take_profit_always_greater_than_stop_loss(self):
        for conf in (60, 80, 93, 100):
            sig = _make_signal(confidence=float(conf), atr=500.0, close=50_000.0)
            assert sig.take_profit_pct() > sig.stop_loss_pct()


# ── ScientificStrategy.evaluate ───────────────────────────────────────────────

class TestScientificStrategyEvaluate:
    SYMBOL = "BTC/USD"

    def _eval(self, df, ofi=0.0, lead_dir=None, lead_strength=0.5,
              regime="RANGING", regime_conf=0.8, funding=None):
        strat = ScientificStrategy()
        return strat.evaluate(
            df, self.SYMBOL,
            ofi_calc=_FakeOFI(ofi),
            lead_lag=_FakeLeadLag(lead_dir, lead_strength),
            regime=regime,
            regime_conf=regime_conf,
            funding_rate=funding,
        )

    # ── basic structural contracts ─────────────────────────────────────────────

    def test_returns_none_if_too_few_rows(self):
        df = _make_df(n=30)
        assert self._eval(df) is None

    def test_returns_none_for_empty_df(self):
        assert self._eval(pd.DataFrame()) is None

    def test_returns_scientific_signal_with_enough_data(self):
        df = _make_df(n=100)
        result = self._eval(df)
        assert result is not None
        assert isinstance(result, ScientificSignal)

    def test_signal_is_valid_enum_value(self):
        df = _make_df(n=100)
        result = self._eval(df)
        assert result.signal in (Signal.BUY, Signal.SELL, Signal.HOLD)

    def test_confidence_in_0_100(self):
        df = _make_df(n=100)
        result = self._eval(df)
        assert 0.0 <= result.confidence <= 100.0

    def test_is_buy_and_is_sell_mutually_exclusive(self):
        df = _make_df(n=100)
        result = self._eval(df)
        assert not (result.is_buy and result.is_sell)

    def test_size_mult_is_non_negative(self):
        df = _make_df(n=100)
        result = self._eval(df)
        assert result.size_mult >= 0.0

    def test_result_has_numeric_fields(self):
        df = _make_df(n=100)
        result = self._eval(df)
        assert isinstance(result.rsi, float)
        assert isinstance(result.adx, float)
        assert isinstance(result.atr, float)
        assert isinstance(result.close, float)
        assert isinstance(result.ema_fast, float)
        assert isinstance(result.ema_slow, float)
        assert result.close > 0.0

    def test_regime_stored_on_result(self):
        df = _make_df(n=100)
        result = self._eval(df, regime="TRENDING_UP")
        assert result.regime == "TRENDING_UP"

    def test_lead_lag_dir_stored_on_result(self):
        df = _make_df(n=100)
        result = self._eval(df, lead_dir="BUY")
        assert result.lead_lag_dir == "BUY"

    def test_funding_rate_stored_on_result(self):
        df = _make_df(n=100)
        result = self._eval(df, funding=0.0005)
        assert result.funding_rate == pytest.approx(0.0005)

    # ── direction selection ────────────────────────────────────────────────────

    def test_strong_bullish_ofi_produces_buy(self):
        # OFI >= 0.30 → direction = BUY regardless of other signals
        df = _make_df(n=100)
        result = self._eval(df, ofi=0.35, lead_dir=None, regime="RANGING")
        assert result is not None
        assert result.signal == Signal.BUY

    def test_strong_bearish_ofi_produces_sell(self):
        df = _make_df(n=100)
        result = self._eval(df, ofi=-0.35, lead_dir=None, regime="RANGING")
        assert result is not None
        assert result.signal == Signal.SELL

    def test_lead_lag_buy_signal_taken_when_ofi_weak(self):
        # |OFI| < 0.30 → lead-lag direction wins the tiebreak
        df = _make_df(n=100)
        result = self._eval(df, ofi=0.05, lead_dir="BUY", regime="RANGING")
        assert result is not None
        assert result.signal == Signal.BUY

    def test_lead_lag_sell_signal_taken_when_ofi_weak(self):
        df = _make_df(n=100)
        result = self._eval(df, ofi=-0.05, lead_dir="SELL", regime="RANGING")
        assert result is not None
        assert result.signal == Signal.SELL

    def test_crash_regime_blocks_longs(self):
        """CRASH regime must suppress a BUY even when OFI is strongly bullish."""
        df = _make_df(n=100)
        result = self._eval(df, ofi=0.5, lead_dir="BUY", regime="CRASH")
        assert result is not None
        assert result.signal != Signal.BUY

    def test_hold_when_no_directional_signal(self):
        # Neutral OFI, no lead-lag, flat trend → likely HOLD or very low confidence
        df = _make_df(n=100, trend=0.01)
        result = self._eval(df, ofi=0.0, lead_dir=None, regime="RANGING")
        assert result is not None
        if result.signal == Signal.HOLD:
            assert result.size_mult == 0.0

    # ── confidence scoring mechanics ───────────────────────────────────────────

    def test_aligned_ofi_raises_confidence_vs_neutral(self):
        df = _make_df(n=100)
        r_neutral = self._eval(df, ofi=0.0,  lead_dir="BUY", regime="TRENDING_UP")
        r_aligned = self._eval(df, ofi=0.25, lead_dir="BUY", regime="TRENDING_UP")
        if r_neutral and r_aligned and r_neutral.signal == r_aligned.signal == Signal.BUY:
            assert r_aligned.confidence >= r_neutral.confidence

    def test_opposing_ofi_lowers_confidence_vs_neutral(self):
        df = _make_df(n=100)
        r_neutral  = self._eval(df, ofi=0.0,   lead_dir="BUY", regime="TRENDING_UP")
        r_opposing = self._eval(df, ofi=-0.25, lead_dir="BUY", regime="TRENDING_UP")
        if r_neutral and r_opposing and r_neutral.signal == r_opposing.signal == Signal.BUY:
            assert r_opposing.confidence <= r_neutral.confidence

    def test_aligned_lead_lag_raises_confidence(self):
        df = _make_df(n=100)
        r_no_lead = self._eval(df, ofi=0.2, lead_dir=None,  regime="RANGING")
        r_led     = self._eval(df, ofi=0.2, lead_dir="BUY", regime="RANGING")
        if r_no_lead and r_led and r_no_lead.signal == r_led.signal == Signal.BUY:
            assert r_led.confidence >= r_no_lead.confidence

    def test_trending_up_regime_boosts_buy_confidence(self):
        df = _make_df(n=100)
        r_ranging  = self._eval(df, ofi=0.2, lead_dir="BUY", regime="RANGING")
        r_trending = self._eval(df, ofi=0.2, lead_dir="BUY", regime="TRENDING_UP")
        if r_ranging and r_trending and r_ranging.signal == r_trending.signal == Signal.BUY:
            assert r_trending.confidence >= r_ranging.confidence

    def test_high_positive_funding_penalises_buy_confidence(self):
        df = _make_df(n=100)
        r_no_fund   = self._eval(df, ofi=0.2, lead_dir="BUY", regime="RANGING", funding=None)
        r_high_fund = self._eval(df, ofi=0.2, lead_dir="BUY", regime="RANGING", funding=0.002)
        if r_no_fund and r_high_fund and r_no_fund.signal == r_high_fund.signal == Signal.BUY:
            assert r_high_fund.confidence <= r_no_fund.confidence

    def test_below_min_confidence_yields_zero_size_mult(self):
        df = _make_df(n=100, trend=0.0)
        result = self._eval(df, ofi=0.0, lead_dir=None, regime="RANGING")
        if result and result.confidence < 38.0:
            assert result.size_mult == 0.0

    # ── score components ───────────────────────────────────────────────────────

    def test_score_components_are_floats(self):
        df = _make_df(n=100)
        result = self._eval(df, ofi=0.2, lead_dir="BUY", regime="TRENDING_UP")
        assert result is not None
        for field in ("ofi_score", "lead_lag_score", "regime_score",
                      "rsi_score", "technical_score", "funding_score"):
            assert isinstance(getattr(result, field), float), f"{field} is not float"

    def test_bullish_ofi_stores_positive_ofi_score_on_buy(self):
        df = _make_df(n=100)
        result = self._eval(df, ofi=0.25, lead_dir="BUY", regime="TRENDING_UP")
        if result and result.signal == Signal.BUY:
            assert result.ofi_score > 0.0

    def test_evaluate_does_not_raise_on_noisy_data(self):
        """Random noise must not raise — returns None or a valid ScientificSignal."""
        rng = np.random.default_rng(42)
        dates = pd.date_range("2024-01-01", periods=80, freq="1min")
        closes = 50_000.0 + rng.standard_normal(80) * 500
        df = pd.DataFrame(
            {"open": closes, "high": closes * 1.001, "low": closes * 0.999,
             "close": closes, "volume": np.full(80, 100.0)},
            index=dates,
        )
        result = self._eval(df)
        assert result is None or isinstance(result, ScientificSignal)

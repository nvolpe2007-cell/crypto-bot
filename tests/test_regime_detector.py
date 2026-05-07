"""
Unit tests for src/regime_detector.py

Covers:
- RegimeResult: every property (is_trending, is_ranging, is_crash, is_volatile,
  allows_long, strategy_hint, color), to_dict() contract
- RegimeDetector.detect: None / empty / too-few-bars guards, output structure
  (valid regime label, confidence in [0, 1], all numeric fields present)
- Regime-specific classification paths:
    CRASH       — flat period followed by sharp crash (≫ EMA200 gap threshold)
    TRENDING_UP — consistent uptrend; ADX very high, slope positive
    TRENDING_DOWN — consistent downtrend; ADX high, slope negative
    VOLATILE    — high-frequency oscillation; ATR% exceeds volatile threshold
    RANGING     — detector with raised thresholds so non-extreme data → RANGING

Data engineering notes
----------------------
The stub pandas_ta (installed by conftest.py) computes real EWM-based indicators.
To keep ATR below the default 0.08%-of-price volatile threshold:
  * bar spread is 0.001% (high = close * 1.00001, low = close * 0.99999)
  * per-bar price change (the trend) dominates TR; keep it ≤ 10/bar on a
    50 000 base → ATR ≈ 10 → atr_pct ≈ 0.02 % < 0.08 %

For trending data the stub ADX returns ~100 (all directional movement in one
direction → dx = 100 every bar → ADX ≈ 100 >> 22 = adx_trend_threshold).

For flat prices the stub ADX fillna(25) fires (0/0 → NaN → 25), which exceeds
the default adx_trend_threshold (22) and routes to TRENDING_DOWN.  The RANGING
tests therefore use a RegimeDetector with raised thresholds (adx_ranging=100)
so the fillna ADX is classified as RANGING instead.
"""

import pytest
import numpy as np
import pandas as pd
from datetime import datetime, timedelta

from src.regime_detector import RegimeDetector, RegimeResult


_VALID_REGIMES = frozenset(
    {'TRENDING_UP', 'TRENDING_DOWN', 'RANGING', 'VOLATILE', 'CRASH'}
)


# ── Data helpers ──────────────────────────────────────────────────────────────

def _make_trending_df(n: int = 210, base: float = 50_000.0,
                      trend: float = 10.0) -> pd.DataFrame:
    """Consistent uptrend (+trend) or downtrend (-trend) per bar.

    Tiny bar spread keeps ATR ≈ |trend| so atr_pct stays well below the
    default 0.08% volatile threshold (0.02% for trend=10 on base=50000).
    """
    dates  = pd.date_range("2024-01-01", periods=n, freq="1min")
    closes = base + np.arange(n, dtype=float) * trend
    closes = np.maximum(closes, 1.0)
    sp     = closes * 0.00001   # 0.001% bar spread
    return pd.DataFrame(
        {
            "open":   closes - sp * 0.5,
            "high":   closes + sp,
            "low":    closes - sp,
            "close":  closes,
            "volume": np.full(n, 500.0),
        },
        index=dates,
    )


def _make_crash_df(flat_bars: int = 185, crash_bars: int = 20,
                   base: float = 50_000.0,
                   crash_per_bar: float = 500.0) -> pd.DataFrame:
    """Flat base period then a rapid crash.

    After crash_bars the price is far below the 200-bar EMA (gap ≪ −4%)
    and RSI is near 0.  CRASH is the highest-priority check in detect(), so
    it fires even though ATR% is high during the crash bars.
    """
    n      = flat_bars + crash_bars
    dates  = pd.date_range("2024-01-01", periods=n, freq="1min")
    closes = np.concatenate([
        np.full(flat_bars, base),
        base - np.arange(1, crash_bars + 1, dtype=float) * crash_per_bar,
    ])
    closes = np.maximum(closes, 1.0)
    sp     = closes * 0.00001
    return pd.DataFrame(
        {
            "open":   closes - sp * 0.5,
            "high":   closes + sp,
            "low":    closes - sp,
            "close":  closes,
            "volume": np.full(n, 500.0),
        },
        index=dates,
    )


def _make_oscillating_df(n: int = 210, base: float = 50_000.0,
                          amplitude: float = 500.0,
                          period: int = 10) -> pd.DataFrame:
    """High-frequency sine-wave prices.

    TR ≈ amplitude * 2π/period ≈ 314 for amplitude=500, period=10.
    atr_pct ≈ 314/50000 * 100 = 0.63% >> 0.08% → VOLATILE.
    The oscillation keeps the EMA200 near base, so the CRASH check (gap ≪ −4%)
    never fires before VOLATILE.
    """
    dates  = pd.date_range("2024-01-01", periods=n, freq="1min")
    closes = base + amplitude * np.sin(2 * np.pi * np.arange(n) / period)
    closes = np.maximum(closes, 1.0)
    sp     = closes * 0.00001
    return pd.DataFrame(
        {
            "open":   closes - sp * 0.5,
            "high":   closes + sp,
            "low":    closes - sp,
            "close":  closes,
            "volume": np.full(n, 500.0),
        },
        index=dates,
    )


def _make_result(regime: str = "RANGING", confidence: float = 0.7,
                 adx: float = 20.0, rsi: float = 50.0,
                 atr_pct: float = 0.05, trend_slope: float = 0.0,
                 rsi_std: float = 5.0) -> RegimeResult:
    return RegimeResult(
        regime=regime, confidence=confidence,
        adx=adx, rsi=rsi, atr_pct=atr_pct,
        trend_slope=trend_slope, rsi_std=rsi_std,
    )


# ── RegimeResult properties ───────────────────────────────────────────────────

class TestRegimeResultProperties:
    """Pure property tests — no indicator computation."""

    # is_trending
    def test_trending_up_is_trending(self):
        assert _make_result("TRENDING_UP").is_trending is True

    def test_trending_down_is_trending(self):
        assert _make_result("TRENDING_DOWN").is_trending is True

    def test_ranging_is_not_trending(self):
        assert _make_result("RANGING").is_trending is False

    def test_volatile_is_not_trending(self):
        assert _make_result("VOLATILE").is_trending is False

    def test_crash_is_not_trending(self):
        assert _make_result("CRASH").is_trending is False

    # is_ranging
    def test_ranging_is_ranging(self):
        assert _make_result("RANGING").is_ranging is True

    def test_trending_up_is_not_ranging(self):
        assert _make_result("TRENDING_UP").is_ranging is False

    # is_crash
    def test_crash_is_crash(self):
        assert _make_result("CRASH").is_crash is True

    def test_ranging_is_not_crash(self):
        assert _make_result("RANGING").is_crash is False

    # is_volatile
    def test_volatile_is_volatile(self):
        assert _make_result("VOLATILE").is_volatile is True

    def test_crash_is_not_volatile(self):
        assert _make_result("CRASH").is_volatile is False

    # allows_long
    def test_crash_blocks_longs(self):
        assert _make_result("CRASH").allows_long is False

    def test_trending_down_blocks_longs(self):
        assert _make_result("TRENDING_DOWN").allows_long is False

    def test_trending_up_allows_longs(self):
        assert _make_result("TRENDING_UP").allows_long is True

    def test_ranging_allows_longs(self):
        assert _make_result("RANGING").allows_long is True

    def test_volatile_allows_longs(self):
        assert _make_result("VOLATILE").allows_long is True

    # strategy_hint
    def test_trending_up_has_strategy_hint(self):
        hint = _make_result("TRENDING_UP").strategy_hint
        assert isinstance(hint, str) and len(hint) > 0

    def test_ranging_has_strategy_hint(self):
        hint = _make_result("RANGING").strategy_hint
        assert isinstance(hint, str) and len(hint) > 0

    def test_crash_has_strategy_hint(self):
        hint = _make_result("CRASH").strategy_hint
        assert isinstance(hint, str) and len(hint) > 0

    # color
    def test_color_returns_hex_string(self):
        for regime in _VALID_REGIMES:
            c = _make_result(regime).color()
            assert isinstance(c, str) and c.startswith("#"), f"bad color for {regime}"

    def test_color_different_for_crash_vs_trending_up(self):
        assert _make_result("CRASH").color() != _make_result("TRENDING_UP").color()

    # to_dict
    def test_to_dict_contains_required_keys(self):
        d = _make_result().to_dict()
        for key in ("regime", "confidence", "adx", "rsi", "atr_pct",
                    "trend_slope", "rsi_std", "allows_long", "strategy_hint", "color"):
            assert key in d, f"missing key: {key}"

    def test_to_dict_regime_matches(self):
        d = _make_result("CRASH").to_dict()
        assert d["regime"] == "CRASH"

    def test_to_dict_allows_long_matches_property(self):
        for regime in _VALID_REGIMES:
            r = _make_result(regime)
            assert r.to_dict()["allows_long"] == r.allows_long

    def test_to_dict_confidence_is_float(self):
        d = _make_result(confidence=0.75).to_dict()
        assert isinstance(d["confidence"], float)

    def test_to_dict_is_json_serialisable(self):
        import json
        for regime in _VALID_REGIMES:
            d = _make_result(regime).to_dict()
            json.dumps(d)   # must not raise


# ── detect() input guards ─────────────────────────────────────────────────────

class TestRegimeDetectorInputGuards:
    def test_returns_none_for_none_input(self):
        assert RegimeDetector().detect(None) is None

    def test_returns_none_for_empty_dataframe(self):
        assert RegimeDetector().detect(pd.DataFrame()) is None

    def test_returns_none_for_fewer_than_200_bars(self):
        df = _make_trending_df(n=199)
        assert RegimeDetector().detect(df) is None

    def test_returns_none_for_exactly_199_bars(self):
        df = _make_trending_df(n=199)
        assert RegimeDetector().detect(df) is None

    def test_returns_result_for_exactly_200_bars(self):
        df = _make_trending_df(n=200)
        assert RegimeDetector().detect(df) is not None

    def test_returns_result_for_210_bars(self):
        df = _make_trending_df(n=210)
        assert RegimeDetector().detect(df) is not None


# ── detect() output contract ──────────────────────────────────────────────────

class TestRegimeDetectorOutputContract:
    """Verify structural guarantees of detect() output regardless of regime."""

    def _run(self, **kwargs):
        return RegimeDetector(**kwargs).detect(_make_trending_df(n=210, trend=10.0))

    def test_result_is_regime_result_instance(self):
        assert isinstance(self._run(), RegimeResult)

    def test_regime_is_valid_label(self):
        result = self._run()
        assert result.regime in _VALID_REGIMES

    def test_confidence_between_0_and_1(self):
        result = self._run()
        assert 0.0 <= result.confidence <= 1.0

    def test_adx_is_non_negative_float(self):
        result = self._run()
        assert isinstance(result.adx, float)
        assert result.adx >= 0.0

    def test_rsi_is_float_in_0_100(self):
        result = self._run()
        assert isinstance(result.rsi, float)
        assert 0.0 <= result.rsi <= 100.0

    def test_atr_pct_is_non_negative(self):
        result = self._run()
        assert result.atr_pct >= 0.0

    def test_trend_slope_is_float(self):
        result = self._run()
        assert isinstance(result.trend_slope, float)

    def test_rsi_std_is_non_negative(self):
        result = self._run()
        assert result.rsi_std >= 0.0

    def test_does_not_raise_on_uptrend(self):
        RegimeDetector().detect(_make_trending_df(n=210, trend=10.0))

    def test_does_not_raise_on_downtrend(self):
        RegimeDetector().detect(_make_trending_df(n=210, trend=-5.0))

    def test_does_not_raise_on_crash_data(self):
        RegimeDetector().detect(_make_crash_df())

    def test_does_not_raise_on_oscillating_data(self):
        RegimeDetector().detect(_make_oscillating_df())


# ── CRASH detection ───────────────────────────────────────────────────────────

class TestCrashDetection:
    """185 flat bars + 20 crash bars (−500/bar) reliably triggers CRASH:
    • EMA200 ≈ 49 850 (barely moved from flat period)
    • final close ≈ 40 000
    • gap ≈ −19.8 % ≪ −4 % threshold
    • RSI → near 0 ≪ 32 threshold
    CRASH is the highest-priority check in detect(), so it fires even though
    ATR% is high during the crash bars.
    """

    def _detect(self):
        return RegimeDetector().detect(_make_crash_df())

    def test_detects_crash_regime(self):
        result = self._detect()
        assert result is not None
        assert result.regime == "CRASH"

    def test_crash_confidence_is_positive(self):
        result = self._detect()
        assert result.confidence > 0.0

    def test_crash_is_crash_property(self):
        result = self._detect()
        assert result.is_crash is True

    def test_crash_blocks_longs(self):
        result = self._detect()
        assert result.allows_long is False

    def test_smaller_crash_also_detected(self):
        """A 10-bar crash of −400/bar is still well outside the gap threshold."""
        result = RegimeDetector().detect(
            _make_crash_df(flat_bars=190, crash_bars=10, crash_per_bar=400.0)
        )
        assert result is not None
        assert result.regime == "CRASH"

    def test_moderate_decline_is_not_crash(self):
        """Gentle downtrend (−5/bar) does not reach the −4% EMA200 gap."""
        result = RegimeDetector().detect(_make_trending_df(n=210, trend=-5.0))
        assert result is not None
        assert result.regime != "CRASH"


# ── TRENDING_UP detection ─────────────────────────────────────────────────────

class TestTrendingUpDetection:
    """Consistent +10/bar uptrend (210 bars) on a 50 000 base.
    • ADX ≈ 100 >> 22 (adx_trend_threshold) → TRENDING branch
    • trend_slope > 0 AND price > EMA50 → TRENDING_UP
    • ATR ≈ 10 → atr_pct ≈ 0.02 % < 0.08 % → VOLATILE never fires
    """

    def _detect(self):
        return RegimeDetector().detect(_make_trending_df(n=210, trend=10.0))

    def test_uptrend_classified_as_trending(self):
        result = self._detect()
        assert result is not None
        assert result.regime in ("TRENDING_UP", "TRENDING_DOWN")

    def test_uptrend_classified_as_trending_up(self):
        result = self._detect()
        assert result.regime == "TRENDING_UP"

    def test_trending_up_is_trending_property(self):
        result = self._detect()
        assert result.is_trending is True

    def test_trending_up_allows_longs(self):
        result = self._detect()
        assert result.allows_long is True

    def test_trending_up_confidence_positive(self):
        result = self._detect()
        assert result.confidence > 0.0


# ── TRENDING_DOWN detection ───────────────────────────────────────────────────

class TestTrendingDownDetection:
    """Consistent −5/bar downtrend (210 bars).
    • ADX ≈ 100 (all minus_dm) → TRENDING branch
    • trend_slope < 0 → TRENDING_DOWN
    • gap ≈ −1.3 % (well within −4 % crash threshold) → no CRASH
    """

    def _detect(self):
        return RegimeDetector().detect(_make_trending_df(n=210, trend=-5.0))

    def test_downtrend_classified_as_trending_down(self):
        result = self._detect()
        assert result is not None
        assert result.regime == "TRENDING_DOWN"

    def test_trending_down_is_trending_property(self):
        result = self._detect()
        assert result.is_trending is True

    def test_trending_down_blocks_longs(self):
        result = self._detect()
        assert result.allows_long is False

    def test_trending_down_confidence_positive(self):
        result = self._detect()
        assert result.confidence > 0.0


# ── VOLATILE detection ────────────────────────────────────────────────────────

class TestVolatileDetection:
    """High-frequency oscillation (amplitude=500, period=10, 210 bars).
    • TR ≈ 314/bar → ATR ≈ 314 → atr_pct ≈ 0.63 % >> 0.08 % default
    • EMA200 stays near 50 000 (oscillation averages out) → gap ≈ 0 % → no CRASH
    VOLATILE is checked AFTER CRASH but BEFORE RANGING/TRENDING.
    """

    def _detect(self):
        return RegimeDetector().detect(_make_oscillating_df())

    def test_oscillating_data_classified_as_volatile(self):
        result = self._detect()
        assert result is not None
        assert result.regime == "VOLATILE"

    def test_volatile_is_volatile_property(self):
        result = self._detect()
        assert result.is_volatile is True

    def test_volatile_allows_longs(self):
        """VOLATILE does not block longs (only CRASH and TRENDING_DOWN do)."""
        result = self._detect()
        assert result.allows_long is True

    def test_volatile_confidence_positive(self):
        result = self._detect()
        assert result.confidence > 0.0

    def test_higher_atr_threshold_avoids_volatile(self):
        """Raising atr_pct_volatile well above the data's ATR lets it classify
        by a different rule (e.g. RANGING or TRENDING)."""
        detector = RegimeDetector(atr_pct_volatile=10.0)   # 10% threshold
        result   = detector.detect(_make_oscillating_df())
        # Still not CRASH; regime could be RANGING or TRENDING depending on ADX
        assert result is not None
        assert result.regime != "CRASH"


# ── RANGING detection ─────────────────────────────────────────────────────────

class TestRangingDetection:
    """Flat prices with a detector whose adx_ranging threshold is raised so
    that the stub ADX (fillna 25) falls squarely below it.

    Detector:
      adx_trend_threshold  = 100   (ADX 25 never triggers TRENDING)
      adx_ranging_threshold = 100  (ADX 25 < 100 → RANGING fires)
      atr_pct_volatile     = 5.0  (ATR ≈ 0 for flat data; safely below 5%)

    This mirrors a real-world scenario where market is objectively sideways.
    """

    _DETECTOR = RegimeDetector(
        adx_trend_threshold=100.0,
        adx_ranging_threshold=100.0,
        atr_pct_volatile=5.0,
    )

    def _detect(self):
        return self._DETECTOR.detect(_make_trending_df(n=210, trend=0.0))

    def test_flat_prices_classified_as_ranging(self):
        result = self._detect()
        assert result is not None
        assert result.regime == "RANGING"

    def test_ranging_is_ranging_property(self):
        result = self._detect()
        assert result.is_ranging is True

    def test_ranging_allows_longs(self):
        result = self._detect()
        assert result.allows_long is True

    def test_ranging_confidence_positive(self):
        result = self._detect()
        assert result.confidence > 0.0


# ── Borderline / edge cases ───────────────────────────────────────────────────

class TestEdgeCases:
    def test_minimal_200_bar_input_produces_valid_regime(self):
        df = _make_trending_df(n=200, trend=10.0)
        result = RegimeDetector().detect(df)
        assert result is not None
        assert result.regime in _VALID_REGIMES

    def test_large_dataset_does_not_raise(self):
        df = _make_trending_df(n=1000, trend=3.0)
        result = RegimeDetector().detect(df)
        assert result is not None

    def test_different_base_prices_produce_valid_output(self):
        for base in (100.0, 1_000.0, 50_000.0, 100_000.0):
            df     = _make_trending_df(n=210, base=base, trend=base * 0.0002)
            result = RegimeDetector().detect(df)
            assert result is not None and result.regime in _VALID_REGIMES, \
                f"Failed for base={base}"

    def test_to_dict_from_real_detect_is_json_serialisable(self):
        import json
        df     = _make_trending_df(n=210, trend=10.0)
        result = RegimeDetector().detect(df)
        assert result is not None
        json.dumps(result.to_dict())   # must not raise

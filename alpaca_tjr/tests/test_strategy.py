"""Unit tests for TJR strategy components."""
import pytest
import pandas as pd
import numpy as np
from datetime import datetime, timezone

# ── Helpers ────────────────────────────────────────────────────────────────

def make_bars(data: list[dict]) -> pd.DataFrame:
    """Build a OHLCV DataFrame from a list of dicts."""
    df = pd.DataFrame(data)
    df.index = pd.date_range("2024-01-02 09:30", periods=len(df), freq="5min", tz="UTC")
    return df[["open", "high", "low", "close", "volume"]]


def flat_bars(n: int, price: float = 100.0, volume: float = 1000) -> pd.DataFrame:
    return make_bars([
        {"open": price, "high": price + 0.1, "low": price - 0.1,
         "close": price, "volume": volume}
        for _ in range(n)
    ])


# ── Swing points ───────────────────────────────────────────────────────────

class TestSwingPoints:
    def test_detects_clear_swing_high(self):
        from alpaca_tjr.strategy.swing_points import find_swings
        # Swing high at index 3 (middle): needs n bars on each side to confirm.
        # With n=3, loop runs range(3, 7-3=4) → checks only i=3.
        bars = make_bars([
            {"open": 100, "high": 101, "low": 99, "close": 100, "volume": 1000},  # 0
            {"open": 100, "high": 101, "low": 99, "close": 100, "volume": 1000},  # 1
            {"open": 100, "high": 101, "low": 99, "close": 100, "volume": 1000},  # 2
            {"open": 102, "high": 105, "low": 101, "close": 103, "volume": 1000},  # 3 — swing high
            {"open": 103, "high": 104, "low": 101, "close": 102, "volume": 1000},  # 4
            {"open": 102, "high": 103, "low": 100, "close": 101, "volume": 1000},  # 5
            {"open": 101, "high": 102, "low": 99, "close": 100, "volume": 1000},  # 6
        ])
        swings = find_swings(bars, n=3)
        highs = [s for s in swings if s.kind == "high"]
        assert any(abs(h.price - 105) < 0.01 for h in highs), "Should detect swing high at 105"

    def test_detects_clear_swing_low(self):
        from alpaca_tjr.strategy.swing_points import find_swings
        # Swing low at index 3 (middle).
        bars = make_bars([
            {"open": 100, "high": 101, "low": 99, "close": 100, "volume": 1000},  # 0
            {"open": 100, "high": 101, "low": 99, "close": 100, "volume": 1000},  # 1
            {"open": 100, "high": 101, "low": 99, "close": 100, "volume": 1000},  # 2
            {"open": 98, "high": 99, "low": 95, "close": 96, "volume": 1000},     # 3 — swing low
            {"open": 96, "high": 98, "low": 96, "close": 97, "volume": 1000},     # 4
            {"open": 97, "high": 100, "low": 97, "close": 99, "volume": 1000},    # 5
            {"open": 99, "high": 101, "low": 98, "close": 100, "volume": 1000},   # 6
        ])
        swings = find_swings(bars, n=3)
        lows = [s for s in swings if s.kind == "low"]
        assert any(abs(l.price - 95) < 0.01 for l in lows), "Should detect swing low at 95"

    def test_insufficient_bars_returns_empty(self):
        from alpaca_tjr.strategy.swing_points import find_swings
        bars = flat_bars(3)
        assert find_swings(bars, n=3) == []


# ── Sweep detection ────────────────────────────────────────────────────────

class TestSweep:
    def test_bullish_sweep_detected(self):
        from alpaca_tjr.strategy.sweep import detect_sweep

        # Wick below 99, close back above
        bars = make_bars([
            {"open": 100, "high": 101, "low": 99.5, "close": 100.2, "volume": 1000},
            {"open": 100, "high": 100.5, "low": 99.8, "close": 100.1, "volume": 1000},
            {"open": 100, "high": 100.3, "low": 98.5, "close": 100.4, "volume": 1500},  # sweep
        ])
        result = detect_sweep(bars, level=99.0, level_name="pm_low", lookback=5, tolerance=0.0)
        assert result is not None
        assert result.direction == "bullish"
        assert result.level_name == "pm_low"

    def test_bearish_sweep_detected(self):
        from alpaca_tjr.strategy.sweep import detect_sweep

        bars = make_bars([
            {"open": 100, "high": 100.5, "low": 99.5, "close": 100.1, "volume": 1000},
            {"open": 100, "high": 100.3, "low": 99.7, "close": 100.0, "volume": 1000},
            {"open": 100, "high": 101.6, "low": 99.5, "close": 99.8, "volume": 1500},  # sweep
        ])
        result = detect_sweep(bars, level=101.0, level_name="pm_high", lookback=5, tolerance=0.0)
        assert result is not None
        assert result.direction == "bearish"

    def test_no_bullish_sweep_when_close_stays_below(self):
        from alpaca_tjr.strategy.sweep import detect_sweep

        # Wick below level, close also stays below → NOT a bullish sweep (no recovery)
        # High kept at exactly the level so no bearish sweep triggers either.
        bars = make_bars([
            {"open": 100, "high": 100.5, "low": 99.5, "close": 100.0, "volume": 1000},
            {"open": 99.0, "high": 99.0, "low": 97.5, "close": 97.8, "volume": 1500},  # stays below
        ])
        result = detect_sweep(bars, level=99.0, level_name="pm_low", lookback=5, tolerance=0.0)
        # No bullish sweep: close(97.8) < level(99). Bearish sweep also absent (high=99.0, not > 99.0).
        assert result is None


# ── BOS detection ──────────────────────────────────────────────────────────

class TestBOS:
    def _make_sweep(self, bar_index=0):
        from alpaca_tjr.strategy.sweep import Sweep
        return Sweep(
            direction="bullish",
            level=99.0,
            level_name="pm_low",
            sweep_price=98.5,
            close_price=99.5,
            bar_index=bar_index,
            timestamp=datetime(2024, 1, 2, 9, 30, tzinfo=timezone.utc),
        )

    def _make_swing(self, kind, price, idx=0):
        from alpaca_tjr.strategy.swing_points import SwingPoint
        return SwingPoint(
            kind=kind, price=price, bar_index=idx,
            timestamp=datetime(2024, 1, 2, 9, 30, tzinfo=timezone.utc),
        )

    def test_bullish_bos_detected(self):
        from alpaca_tjr.strategy.structure import detect_bos

        # Bar 0: sweep; bars 1+: BOS impulse expected
        bars = make_bars([
            {"open": 99.5, "high": 100, "low": 98.5, "close": 99.5, "volume": 1000},  # sweep bar
            {"open": 99.5, "high": 102.0, "low": 99.3, "close": 101.8, "volume": 2000},  # impulse BOS
        ])
        sweep = self._make_sweep(bar_index=0)
        swings = [self._make_swing("high", 101.0, idx=0)]  # existing swing high at 101
        result = detect_bos(bars, sweep, swings, lookback=5, body_ratio_min=0.5)
        assert result is not None
        assert result.direction == "bullish"
        assert result.broken_level == pytest.approx(101.0)

    def test_no_bos_if_not_impulse(self):
        from alpaca_tjr.strategy.structure import detect_bos

        # Small-body doji — not an impulse
        bars = make_bars([
            {"open": 99.5, "high": 102, "low": 98.5, "close": 99.5, "volume": 1000},  # sweep
            {"open": 100, "high": 102, "low": 99, "close": 100.1, "volume": 500},     # doji body=0.1/range=3
        ])
        sweep = self._make_sweep(bar_index=0)
        swings = [self._make_swing("high", 101.0)]
        result = detect_bos(bars, sweep, swings, lookback=5, body_ratio_min=0.5)
        assert result is None


# ── FVG detection ──────────────────────────────────────────────────────────

class TestFVG:
    def test_bullish_fvg_detected(self):
        from alpaca_tjr.strategy.fvg import scan_fvgs

        bars = make_bars([
            {"open": 100, "high": 101, "low": 99, "close": 100, "volume": 1000},
            {"open": 101, "high": 103, "low": 100, "close": 102, "volume": 2000},  # impulse
            {"open": 102, "high": 104, "low": 102.5, "close": 103, "volume": 1500},  # C2.low > C0.high
        ])
        # C0.high=101, C2.low=102.5 → bullish FVG zone [101, 102.5]
        fvgs = scan_fvgs(bars)
        bull_fvgs = [f for f in fvgs if f.kind == "bullish"]
        assert len(bull_fvgs) >= 1
        assert bull_fvgs[0].bottom == pytest.approx(101.0)
        assert bull_fvgs[0].top == pytest.approx(102.5)

    def test_bearish_fvg_detected(self):
        from alpaca_tjr.strategy.fvg import scan_fvgs

        bars = make_bars([
            {"open": 102, "high": 103, "low": 101, "close": 102, "volume": 1000},
            {"open": 101, "high": 102, "low": 99, "close": 100, "volume": 2000},   # impulse down
            {"open": 100, "high": 100.3, "low": 98, "close": 98.5, "volume": 1500},  # C2.high < C0.low
        ])
        # C0.low=101, C2.high=100.3 → bearish FVG zone [100.3, 101]
        fvgs = scan_fvgs(bars)
        bear_fvgs = [f for f in fvgs if f.kind == "bearish"]
        assert len(bear_fvgs) >= 1

    def test_fvg_marked_filled(self):
        from alpaca_tjr.strategy.fvg import scan_fvgs, FVG

        bars = make_bars([
            {"open": 100, "high": 101, "low": 99, "close": 100, "volume": 1000},
            {"open": 101, "high": 103, "low": 100, "close": 102, "volume": 2000},
            {"open": 102, "high": 104, "low": 98, "close": 103, "volume": 1500},  # C2.low=98 fills the gap
        ])
        fvgs = scan_fvgs(bars)
        # All should be filled since C2.low=98 goes through any bullish FVG
        bull_fvgs = [f for f in fvgs if f.kind == "bullish"]
        assert all(f.filled for f in bull_fvgs)


# ── Order block ────────────────────────────────────────────────────────────

class TestOrderBlock:
    def test_bullish_ob_found(self):
        from alpaca_tjr.strategy.order_block import find_order_block
        from alpaca_tjr.strategy.structure import BOS

        bars = make_bars([
            {"open": 101, "high": 102, "low": 100, "close": 101.5, "volume": 1000},
            {"open": 101.5, "high": 102, "low": 100, "close": 100.2, "volume": 1000},  # bearish → OB
            {"open": 100.2, "high": 103, "low": 100, "close": 102.5, "volume": 2000},  # BOS impulse
        ])
        bos = BOS(
            direction="bullish",
            broken_level=101.5,
            impulse_open=100.2,
            impulse_close=102.5,
            impulse_high=103,
            impulse_low=100,
            bar_index=2,
            timestamp=datetime(2024, 1, 2, 9, 40, tzinfo=timezone.utc),
        )
        ob = find_order_block(bars, bos, lookback=5)
        assert ob is not None
        assert ob.kind == "bullish"
        assert ob.bar_index == 1  # the bearish candle at index 1


# ── HTF bias ───────────────────────────────────────────────────────────────

class TestHTFBias:
    def test_bull_bias_above_sma(self):
        from alpaca_tjr.strategy.htf_bias import compute_bias
        closes = list(range(80, 100)) + [105]  # trending up, last close well above SMA
        daily = pd.DataFrame({"close": closes, "open": closes, "high": closes, "low": closes, "volume": [1e6] * len(closes)})
        bias = compute_bias(daily, sma_period=20, neutral_band=0.001)
        assert bias == "bull"

    def test_bear_bias_below_sma(self):
        from alpaca_tjr.strategy.htf_bias import compute_bias
        closes = list(range(120, 100, -1)) + [95]
        daily = pd.DataFrame({"close": closes, "open": closes, "high": closes, "low": closes, "volume": [1e6] * len(closes)})
        bias = compute_bias(daily, sma_period=20, neutral_band=0.001)
        assert bias == "bear"

    def test_neutral_when_at_sma(self):
        from alpaca_tjr.strategy.htf_bias import compute_bias
        closes = [100.0] * 21  # perfectly flat — last close == SMA
        daily = pd.DataFrame({"close": closes, "open": closes, "high": closes, "low": closes, "volume": [1e6] * len(closes)})
        bias = compute_bias(daily, sma_period=20, neutral_band=0.001)
        assert bias == "neutral"


# ── Session windows ────────────────────────────────────────────────────────

class TestSessions:
    def test_primary_session_is_tradeable(self):
        from alpaca_tjr.strategy.sessions import current_session, is_tradeable
        import pytz
        et = pytz.timezone("US/Eastern")
        now = et.localize(datetime(2024, 1, 2, 10, 0))  # 10am ET
        assert current_session(now) == "primary"
        assert is_tradeable(now) is True

    def test_dead_zone_not_tradeable(self):
        from alpaca_tjr.strategy.sessions import is_tradeable
        import pytz
        et = pytz.timezone("US/Eastern")
        now = et.localize(datetime(2024, 1, 2, 12, 0))  # noon ET — dead zone
        assert is_tradeable(now) is False

    def test_premarket_is_not_tradeable(self):
        from alpaca_tjr.strategy.sessions import is_tradeable
        import pytz
        et = pytz.timezone("US/Eastern")
        now = et.localize(datetime(2024, 1, 2, 7, 0))  # 7am ET — pre-market
        assert is_tradeable(now) is False

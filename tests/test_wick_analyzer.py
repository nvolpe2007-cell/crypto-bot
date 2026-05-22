"""
Unit tests for src/wick_analyzer.py

detect_rejection is called by paper_trading._kill_filter_skip() on every
potential entry to block longs into supply walls or shorts into demand walls.
detect_stop_hunt is called to give a +6 confidence boost when a forced-
liquidation flush aligns with the proposed trade direction.

Neither function has been tested before — this file fills that gap.

Coverage:
  _safe_body / _upper_wick / _lower_wick helpers (indirectly via detect_rejection)

  detect_rejection():
    buy side
    - None when df has fewer rows than lookback
    - None when df is None
    - None when no candle has upper-wick / body ratio >= 2
    - None when only 1 rejection wick in the window (< MIN_COUNT=2)
    - Reason string returned when >=2 clustered upper wicks (within 0.20% band)
    - Reason string contains 'ceiling'
    - Reason string mentions the wick count
    - None when 2 wicks exist but highs differ by >0.20% (not a cluster)
    - 'long' alias behaves identically to 'buy'
    short side
    - None when no strong lower wicks
    - None when only 1 lower wick
    - Reason string when >=2 clustered lower wicks
    - Reason string contains 'floor'
    - None when 2 lower wicks are too spread (>0.20%)
    - 'sell' alias behaves identically to 'short'
    - buy and short results are independent (upper vs lower wick check)

  detect_stop_hunt():
    buy side
    - None when df has fewer than swing_bars+3 rows
    - None when df is None
    - None when no hunt bar pierces swing_low by >=0.15%
    - Dict returned on pierce + same-bar reclaim; reclaim_lag == 0
    - Dict contains required keys: side, pierce_price, swing_level, reclaim_lag
    - Pierce price and swing level values are correct
    - reclaim_lag == 1 when piercing bar doesn't reclaim but next bar does
    - None when pierce detected but no bar in window reclaims
    - 'long' alias behaves identically to 'buy'; dict side == 'buy'
    sell side
    - None when no hunt bar pierces swing_high by >=0.15%
    - Dict returned on pierce + same-bar reclaim; side == 'sell'
    - Pierce price and swing level values are correct
    - None when pierce detected but no bar reclaims
    - 'short' alias gives same result as 'sell'
"""

import pytest
import pandas as pd
from datetime import datetime, timedelta

from src.wick_analyzer import detect_rejection, detect_stop_hunt


# ── DataFrame builder ─────────────────────────────────────────────────────────

def _make_df(candles: list) -> pd.DataFrame:
    """Build a DataFrame from a list of (open, high, low, close) tuples."""
    idx = pd.date_range("2024-01-01", periods=len(candles), freq="1min")
    rows = [(float(o), float(h), float(l), float(c), 100.0) for o, h, l, c in candles]
    return pd.DataFrame(rows, columns=["open", "high", "low", "close", "volume"], index=idx)


# Pre-built candle types
# Normal: body=1.0, upper_wick=0.2 → ratio 0.2 < 2.0  (NOT a rejection)
def _normal(price: float = 100.0):
    return (price, price + 1.2, price - 0.5, price + 1.0)


# Strong upper wick: body=0.1, upper_wick = high-(price+0.1); ratio=(high-price-0.1)/0.1
# Requires high > price + 0.3 for ratio >= 2.
def _upper_wick(high: float, price: float = 100.0):
    """Return (open, HIGH, low, close) with a strong upper wick at `high`."""
    return (price, high, price - 0.1, price + 0.1)


# Strong lower wick: body=0.1, lower_wick = (price-0.1)-low; ratio=(price-0.1-low)/0.1
# Requires low < price - 0.3 for ratio >= 2.
def _lower_wick(low: float, price: float = 100.0):
    """Return (open, high, LOW, close) with a strong lower wick at `low`."""
    return (price, price + 0.1, low, price - 0.1)


# ── detect_rejection — buy side ───────────────────────────────────────────────

class TestDetectRejectionBuySide:
    # Cluster pair (highs differ by 0.001%): both qualify as upper-wick rejections
    _clustered = [
        _normal(),
        _normal(),
        _normal(),
        _upper_wick(high=100.500),  # body=0.1, upper_wick=0.4, ratio=4.0 ✓
        _upper_wick(high=100.501),  # body=0.1, upper_wick≈0.4, ratio≈4.0 ✓
    ]

    def test_returns_none_for_none_df(self):
        assert detect_rejection(None, side="buy") is None

    def test_returns_none_when_df_shorter_than_lookback(self):
        df = _make_df([_normal() for _ in range(4)])  # default lookback=5
        assert detect_rejection(df, side="buy") is None

    def test_returns_none_when_no_upper_wicks(self):
        """All candles have body >> upper wick → no rejection wicks."""
        df = _make_df([_normal() for _ in range(10)])
        assert detect_rejection(df, side="buy") is None

    def test_returns_none_when_only_one_rejection_wick(self):
        """One strong upper wick is below the minimum count of 2."""
        candles = [_normal()] * 4 + [_upper_wick(high=100.500)]
        df = _make_df(candles)
        assert detect_rejection(df, side="buy") is None

    def test_returns_string_for_two_clustered_upper_wicks(self):
        """Two upper wicks with highs within 0.20% → ceiling rejection detected."""
        df = _make_df(self._clustered)
        result = detect_rejection(df, side="buy")
        assert result is not None
        assert isinstance(result, str)

    def test_reason_string_contains_ceiling(self):
        df = _make_df(self._clustered)
        result = detect_rejection(df, side="buy")
        assert "ceiling" in result.lower()

    def test_reason_string_mentions_wick_count(self):
        """The reason string should report the number of rejection wicks."""
        df = _make_df(self._clustered)
        result = detect_rejection(df, side="buy")
        # 2 wicks in the default window of 5 bars
        assert "2" in result

    def test_returns_none_when_wicks_not_clustered(self):
        """Two upper wicks with highs differing by ~1.98% → no cluster → None."""
        candles = [
            _normal(),
            _normal(),
            _normal(),
            # low=99.4 so high=100.0 > close=99.6 is valid
            (99.5, 100.0, 99.4, 99.6),   # body=0.1, upper_wick=0.4, high=100.0
            # high=102.0 >> close=100.1: ratio=(102-100.1)/0.1=19 ✓
            _upper_wick(high=102.0),      # high=102.0
        ]
        # Band: |102.0 - 100.0| / 101.0 * 100 ≈ 1.98% > 0.20% → no cluster
        df = _make_df(candles)
        assert detect_rejection(df, side="buy") is None

    def test_long_alias_identical_to_buy(self):
        """'long' is treated the same as 'buy'."""
        df = _make_df(self._clustered)
        assert detect_rejection(df, side="buy") == detect_rejection(df, side="long")

    def test_custom_lookback_respects_shorter_window(self):
        """With lookback=3 only the last 3 bars matter."""
        # Only the first 2 bars (not in the last 3) have wicks → no signal
        candles = [
            _upper_wick(high=100.5),  # bar 0 — outside lookback=3
            _upper_wick(high=100.5),  # bar 1 — outside lookback=3
            _normal(),                # bar 2 — in window
            _normal(),                # bar 3 — in window
            _normal(),                # bar 4 — in window
        ]
        df = _make_df(candles)
        assert detect_rejection(df, side="buy", lookback=3) is None

    def test_all_five_bars_are_rejection_wicks(self):
        """All candles in the lookback window are rejection wicks → detected."""
        candles = [_upper_wick(high=100.500) for _ in range(5)]
        df = _make_df(candles)
        result = detect_rejection(df, side="buy")
        assert result is not None


# ── detect_rejection — short side ────────────────────────────────────────────

class TestDetectRejectionShortSide:
    # Cluster pair of lower wicks (lows differ by 0.001%)
    _clustered = [
        _normal(),
        _normal(),
        _normal(),
        _lower_wick(low=99.500),   # body=0.1, lower_wick=0.4, ratio=4.0 ✓
        _lower_wick(low=99.501),   # body=0.1, lower_wick≈0.399, ratio≈3.99 ✓
    ]

    def test_returns_none_no_lower_wicks(self):
        df = _make_df([_normal() for _ in range(10)])
        assert detect_rejection(df, side="short") is None

    def test_returns_none_one_lower_wick(self):
        candles = [_normal()] * 4 + [_lower_wick(low=99.5)]
        df = _make_df(candles)
        assert detect_rejection(df, side="short") is None

    def test_returns_reason_for_two_clustered_lower_wicks(self):
        df = _make_df(self._clustered)
        result = detect_rejection(df, side="short")
        assert result is not None
        assert isinstance(result, str)

    def test_reason_string_contains_floor(self):
        df = _make_df(self._clustered)
        result = detect_rejection(df, side="short")
        assert "floor" in result.lower()

    def test_returns_none_when_lower_wicks_not_clustered(self):
        """Two lower wicks with lows differing by ~2% → no cluster → None."""
        candles = [
            _normal(),
            _normal(),
            _normal(),
            _lower_wick(low=99.0),    # low=99.0
            _lower_wick(low=97.0),    # low=97.0
        ]
        # Band: |99.0 - 97.0| / 98.0 * 100 ≈ 2.04% > 0.20% → None
        df = _make_df(candles)
        assert detect_rejection(df, side="short") is None

    def test_sell_alias_identical_to_short(self):
        df = _make_df(self._clustered)
        assert detect_rejection(df, side="short") == detect_rejection(df, side="sell")

    def test_buy_and_short_check_different_wicks(self):
        """Lower wicks trigger for 'short' but NOT for 'buy', and vice-versa."""
        candles = [
            _normal(),
            _normal(),
            _normal(),
            _lower_wick(low=99.500),
            _lower_wick(low=99.501),
        ]
        df = _make_df(candles)
        # Only 'short' side should fire
        assert detect_rejection(df, side="short") is not None
        assert detect_rejection(df, side="buy") is None


# ── detect_stop_hunt — buy side ───────────────────────────────────────────────
#
# We use swing_bars=5 to keep DataFrames small (needed = 5+2+1 = 8 bars):
#   swing_window = first 5 bars  (establishes swing_low)
#   hunt_window  = last  3 bars  (checked for pierce + reclaim)

_SB = 5   # test-specific swing_bars parameter


def _buy_df(swing_low: float, swing_normal_low: float, hunt_bars: list) -> pd.DataFrame:
    """
    Build an 8-bar DataFrame for buy-side stop-hunt tests.

    swing_low is placed in the first bar's low. The remaining 4 swing bars
    have their lows set to swing_normal_low (must be > swing_low) so the
    swing minimum is unambiguously swing_low.

    hunt_bars: list of 3 (open, high, low, close) tuples.
    """
    first = (swing_low + 1.0, swing_low + 2.0, swing_low, swing_low + 1.0)
    normal = (
        swing_normal_low + 1.0,
        swing_normal_low + 2.0,
        swing_normal_low,
        swing_normal_low + 1.0,
    )
    return _make_df([first] + [normal] * (_SB - 1) + hunt_bars)


def _sell_df(swing_high: float, swing_normal_high: float, hunt_bars: list) -> pd.DataFrame:
    """
    Build an 8-bar DataFrame for sell-side stop-hunt tests.

    swing_high is placed in the first bar's high. The remaining 4 swing bars
    have their highs set to swing_normal_high (must be < swing_high) so the
    swing maximum is unambiguously swing_high.
    """
    first = (swing_high - 1.0, swing_high, swing_high - 2.0, swing_high - 1.0)
    normal = (
        swing_normal_high - 1.0,
        swing_normal_high,
        swing_normal_high - 2.0,
        swing_normal_high - 1.0,
    )
    return _make_df([first] + [normal] * (_SB - 1) + hunt_bars)


class TestDetectStopHuntBuySide:
    # swing_low=100.0; pierce_thresh = 100.0 * (1 - 0.15/100) = 99.85
    _SWING_LOW = 100.0
    _PIERCE_THRESH = 99.85

    def test_returns_none_for_none_df(self):
        assert detect_stop_hunt(None, side="buy", swing_bars=_SB) is None

    def test_returns_none_when_df_too_short(self):
        df = _make_df([_normal() for _ in range(7)])  # < 8 needed
        assert detect_stop_hunt(df, side="buy", swing_bars=_SB) is None

    def test_returns_none_no_pierce(self):
        """Hunt lows stay above pierce threshold → no stop hunt detected."""
        # pierce_thresh = 99.85; hunt lows = 99.90 > 99.85
        hunt = [
            (100.0, 101.0, 99.90, 100.5),
            (100.0, 101.0, 99.90, 100.5),
            (100.0, 101.0, 99.90, 100.5),
        ]
        df = _buy_df(self._SWING_LOW, 105.0, hunt)
        assert detect_stop_hunt(df, side="buy", swing_bars=_SB) is None

    def test_returns_dict_on_pierce_and_same_bar_reclaim(self):
        """Hunt bar pierces (low ≤ 99.85) and reclaims (close > 100.0) in one bar."""
        hunt = [
            (100.0, 101.0, 99.80, 100.5),  # pierce (99.80≤99.85), reclaim (100.5>100)
            (100.0, 101.0, 100.0, 100.5),
            (100.0, 101.0, 100.0, 100.5),
        ]
        df = _buy_df(self._SWING_LOW, 105.0, hunt)
        result = detect_stop_hunt(df, side="buy", swing_bars=_SB)
        assert result is not None
        assert isinstance(result, dict)

    def test_return_dict_has_required_keys(self):
        hunt = [
            (100.0, 101.0, 99.80, 100.5),
            (100.0, 101.0, 100.0, 100.5),
            (100.0, 101.0, 100.0, 100.5),
        ]
        df = _buy_df(self._SWING_LOW, 105.0, hunt)
        result = detect_stop_hunt(df, side="buy", swing_bars=_SB)
        for key in ("side", "pierce_price", "swing_level", "reclaim_lag"):
            assert key in result, f"missing key: {key}"

    def test_return_values_are_correct(self):
        pierce_price = 99.80
        hunt = [
            (100.0, 101.0, pierce_price, 100.5),
            (100.0, 101.0, 100.0, 100.5),
            (100.0, 101.0, 100.0, 100.5),
        ]
        df = _buy_df(self._SWING_LOW, 105.0, hunt)
        result = detect_stop_hunt(df, side="buy", swing_bars=_SB)
        assert result["side"] == "buy"
        assert abs(result["pierce_price"] - pierce_price) < 1e-9
        assert abs(result["swing_level"] - self._SWING_LOW) < 1e-9
        assert result["reclaim_lag"] == 0

    def test_reclaim_lag_one_when_next_bar_reclaims(self):
        """Piercing bar doesn't close above swing_low; the next bar does."""
        hunt = [
            # Bar 0: pierces (low=99.80), close=99.9 < 100.0 → no reclaim yet
            (99.9, 100.5, 99.80, 99.9),
            # Bar 1: close=100.5 > 100.0 → reclaims
            (99.9, 100.5, 99.9, 100.5),
            (100.0, 101.0, 100.0, 100.5),
        ]
        df = _buy_df(self._SWING_LOW, 105.0, hunt)
        result = detect_stop_hunt(df, side="buy", swing_bars=_SB)
        assert result is not None
        assert result["reclaim_lag"] == 1

    def test_returns_none_when_no_reclaim_in_window(self):
        """Pierce detected but no bar in the hunt window closes above swing_low."""
        hunt = [
            (99.9, 100.0, 99.80, 99.9),   # pierces, close=99.9 < 100 → no reclaim
            (99.8, 100.0, 99.70, 99.9),   # also no reclaim
            (99.8, 100.0, 99.70, 99.9),   # also no reclaim
        ]
        df = _buy_df(self._SWING_LOW, 105.0, hunt)
        assert detect_stop_hunt(df, side="buy", swing_bars=_SB) is None

    def test_long_alias_gives_side_buy(self):
        """'long' is treated identically to 'buy'; result dict has side=='buy'."""
        hunt = [
            (100.0, 101.0, 99.80, 100.5),
            (100.0, 101.0, 100.0, 100.5),
            (100.0, 101.0, 100.0, 100.5),
        ]
        df = _buy_df(self._SWING_LOW, 105.0, hunt)
        result = detect_stop_hunt(df, side="long", swing_bars=_SB)
        assert result is not None
        assert result["side"] == "buy"

    def test_boundary_pierce_exactly_at_threshold(self):
        """A low exactly at pierce_thresh (99.85) must qualify as a pierce."""
        hunt = [
            (100.0, 101.0, 99.85, 100.5),   # low = 99.85 = pierce_thresh
            (100.0, 101.0, 100.0, 100.5),
            (100.0, 101.0, 100.0, 100.5),
        ]
        df = _buy_df(self._SWING_LOW, 105.0, hunt)
        result = detect_stop_hunt(df, side="buy", swing_bars=_SB)
        assert result is not None


# ── detect_stop_hunt — sell side ─────────────────────────────────────────────

class TestDetectStopHuntSellSide:
    # swing_high=100.0; pierce_thresh = 100.0 * (1 + 0.15/100) = 100.15
    _SWING_HIGH = 100.0
    _PIERCE_THRESH = 100.15

    def test_returns_none_no_pierce(self):
        """Hunt highs stay below pierce threshold → no stop hunt."""
        # pierce_thresh = 100.15; hunt highs = 100.10 < 100.15
        hunt = [
            (99.0, 100.10, 98.5, 99.5),
            (99.0, 100.10, 98.5, 99.5),
            (99.0, 100.10, 98.5, 99.5),
        ]
        df = _sell_df(self._SWING_HIGH, 95.0, hunt)
        assert detect_stop_hunt(df, side="sell", swing_bars=_SB) is None

    def test_returns_dict_on_pierce_and_reclaim(self):
        """Hunt bar pierces (high >= 100.15) and closes back below swing_high."""
        pierce_price = 100.20
        hunt = [
            # Pierces (high=100.20≥100.15), close=99.5<100.0 → reclaim ✓
            (99.0, pierce_price, 98.5, 99.5),
            (99.0, 100.0, 98.5, 99.5),
            (99.0, 100.0, 98.5, 99.5),
        ]
        df = _sell_df(self._SWING_HIGH, 95.0, hunt)
        result = detect_stop_hunt(df, side="sell", swing_bars=_SB)
        assert result is not None
        assert result["side"] == "sell"

    def test_sell_dict_values_correct(self):
        pierce_price = 100.20
        hunt = [
            (99.0, pierce_price, 98.5, 99.5),
            (99.0, 100.0, 98.5, 99.5),
            (99.0, 100.0, 98.5, 99.5),
        ]
        df = _sell_df(self._SWING_HIGH, 95.0, hunt)
        result = detect_stop_hunt(df, side="sell", swing_bars=_SB)
        assert abs(result["pierce_price"] - pierce_price) < 1e-9
        assert abs(result["swing_level"] - self._SWING_HIGH) < 1e-9
        assert result["reclaim_lag"] == 0

    def test_sell_reclaim_lag_one(self):
        """Piercing bar doesn't close below swing_high; next bar does."""
        hunt = [
            # Bar 0: pierces, close=100.5 > 100.0 → no reclaim yet
            (99.0, 100.20, 98.5, 100.5),
            # Bar 1: close=99.5 < 100.0 → reclaims
            (99.0, 100.0, 98.5, 99.5),
            (99.0, 100.0, 98.5, 99.5),
        ]
        df = _sell_df(self._SWING_HIGH, 95.0, hunt)
        result = detect_stop_hunt(df, side="sell", swing_bars=_SB)
        assert result is not None
        assert result["reclaim_lag"] == 1

    def test_returns_none_when_no_reclaim(self):
        """Pierce detected but all closes stay above swing_high → None."""
        hunt = [
            # Pierces (high=100.20), close=100.5 > swing_high → no reclaim
            (99.0, 100.20, 98.5, 100.5),
            (99.0, 100.20, 98.5, 100.5),
            (99.0, 100.20, 98.5, 100.5),
        ]
        df = _sell_df(self._SWING_HIGH, 95.0, hunt)
        assert detect_stop_hunt(df, side="sell", swing_bars=_SB) is None

    def test_short_alias_same_as_sell(self):
        """'short' is treated identically to 'sell'."""
        hunt = [
            (99.0, 100.20, 98.5, 99.5),
            (99.0, 100.0, 98.5, 99.5),
            (99.0, 100.0, 98.5, 99.5),
        ]
        df = _sell_df(self._SWING_HIGH, 95.0, hunt)
        result_sell = detect_stop_hunt(df, side="sell", swing_bars=_SB)
        result_short = detect_stop_hunt(df, side="short", swing_bars=_SB)
        assert (result_sell is None) == (result_short is None)

    def test_boundary_pierce_exactly_at_threshold_sell(self):
        """A high exactly at pierce_thresh (100.15) must qualify."""
        hunt = [
            (99.0, 100.15, 98.5, 99.5),   # high = 100.15 = pierce_thresh
            (99.0, 100.0, 98.5, 99.5),
            (99.0, 100.0, 98.5, 99.5),
        ]
        df = _sell_df(self._SWING_HIGH, 95.0, hunt)
        result = detect_stop_hunt(df, side="sell", swing_bars=_SB)
        assert result is not None

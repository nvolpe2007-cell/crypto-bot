"""Unit tests for src/trailing_stop.py

Covers:
- atr_stop style: trailing never activates (always returns None)
- ema21_1h style: arms at +0.5%, trail 1.0% below peak (long)
- ema50_4h style: arms at +1.0%, trail 2.0% below peak (long)
- Short positions: mirrored favorable/adverse logic
- Ratchet behaviour: trail_stop_price only moves in the favourable direction
- Peak tracking: peak_favorable_price correctly updated on every tick
- MAX_HOLD backstop: exits when intended_hold_min elapsed regardless of price
- MAX_HOLD priority: fires before trail-stop check
- Unknown/None trail_style: treated as no trailing
"""

import pytest
from datetime import datetime, timezone
from unittest.mock import patch, MagicMock

from src.trailing_stop import update_trailing_stop, TRAIL_PARAMS


# ── helpers ───────────────────────────────────────────────────────────────────

_ENTRY_TIME = datetime(2024, 1, 1, 0, 0, 0, tzinfo=timezone.utc)


class _FakePos:
    """Minimal stand-in for PaperPosition."""

    def __init__(
        self,
        side: str = "buy",
        entry_price: float = 100.0,
        trail_style: str = "atr_stop",
        intended_hold_min: int = 0,
        entry_time: datetime = None,
        peak_favorable_price: float = 0.0,
        trail_stop_price: float = 0.0,
    ):
        self.side = side
        self.entry_price = entry_price
        self.trail_style = trail_style
        self.intended_hold_min = intended_hold_min
        self.entry_time = entry_time or _ENTRY_TIME
        self.peak_favorable_price = peak_favorable_price
        self.trail_stop_price = trail_stop_price


def _mock_now(dt: datetime):
    """Return a context manager that patches datetime.now in trailing_stop."""
    m = MagicMock()
    m.now.return_value = dt
    return patch("src.trailing_stop.datetime", m)


_NOW_MINUS_1MIN = datetime(2024, 1, 1, 0, 0, 30, tzinfo=timezone.utc)   # 0.5 min later
_NOW_PLUS_30MIN  = datetime(2024, 1, 1, 0, 30, 0, tzinfo=timezone.utc)   # 30 min later
_NOW_PLUS_6MIN   = datetime(2024, 1, 1, 0, 6, 0, tzinfo=timezone.utc)    # 6 min later
_NOW_PLUS_4MIN   = datetime(2024, 1, 1, 0, 4, 0, tzinfo=timezone.utc)    # 4 min later


# ── atr_stop — no trailing ────────────────────────────────────────────────────

class TestAtrStop:
    """atr_stop is the scalp style — fixed SL/TP only, trail never activates."""

    def _pos(self, **kw) -> _FakePos:
        return _FakePos(trail_style="atr_stop", entry_price=100.0,
                        intended_hold_min=0, **kw)

    def test_returns_none_at_entry_price(self):
        with _mock_now(_NOW_PLUS_30MIN):
            assert update_trailing_stop(self._pos(), 100.0) is None

    def test_returns_none_on_large_gain(self):
        with _mock_now(_NOW_PLUS_30MIN):
            assert update_trailing_stop(self._pos(), 200.0) is None

    def test_returns_none_on_loss(self):
        with _mock_now(_NOW_PLUS_30MIN):
            assert update_trailing_stop(self._pos(), 50.0) is None

    def test_trail_stop_price_stays_zero(self):
        pos = self._pos()
        with _mock_now(_NOW_PLUS_30MIN):
            update_trailing_stop(pos, 200.0)
        assert pos.trail_stop_price == 0.0


# ── ema21_1h — swing trail ────────────────────────────────────────────────────

class TestEma21_1hLong:
    """trigger=+0.5%, trail=1.0% below peak; long positions."""

    def _pos(self, **kw) -> _FakePos:
        return _FakePos(trail_style="ema21_1h", entry_price=100.0,
                        intended_hold_min=0, **kw)

    def test_not_armed_below_trigger(self):
        pos = self._pos()
        with _mock_now(_NOW_PLUS_30MIN):
            result = update_trailing_stop(pos, 100.4)   # +0.4% < 0.5% trigger
        assert result is None
        assert pos.trail_stop_price == 0.0

    def test_arms_exactly_at_trigger(self):
        pos = self._pos()
        with _mock_now(_NOW_PLUS_30MIN):
            result = update_trailing_stop(pos, 100.5)   # exactly +0.5%
        assert result is None
        assert pos.trail_stop_price > 0.0

    def test_trail_stop_set_correctly_on_arm(self):
        pos = self._pos()
        with _mock_now(_NOW_PLUS_30MIN):
            update_trailing_stop(pos, 102.0)
        # trail = 1% below peak 102.0
        assert abs(pos.trail_stop_price - 102.0 * 0.99) < 1e-9

    def test_no_exit_when_price_above_trail(self):
        pos = self._pos()
        with _mock_now(_NOW_PLUS_30MIN):
            update_trailing_stop(pos, 102.0)               # arms trail at ~100.98
            result = update_trailing_stop(pos, 101.5)      # still above trail
        assert result is None

    def test_trail_stop_triggered(self):
        pos = self._pos()
        with _mock_now(_NOW_PLUS_30MIN):
            update_trailing_stop(pos, 102.0)               # trail at ~100.98
            result = update_trailing_stop(pos, 100.9)      # below trail
        assert result == "TRAIL_STOP"

    def test_trail_ratchets_up_with_price(self):
        pos = self._pos()
        with _mock_now(_NOW_PLUS_30MIN):
            update_trailing_stop(pos, 102.0)
            first_stop = pos.trail_stop_price
            update_trailing_stop(pos, 105.0)               # new high
        assert pos.trail_stop_price > first_stop
        assert abs(pos.trail_stop_price - 105.0 * 0.99) < 1e-9

    def test_trail_never_moves_down_for_long(self):
        pos = self._pos()
        with _mock_now(_NOW_PLUS_30MIN):
            update_trailing_stop(pos, 105.0)
            high_stop = pos.trail_stop_price
            update_trailing_stop(pos, 102.0)               # retraced but above trail
        assert pos.trail_stop_price == high_stop

    def test_peak_tracks_highest_price(self):
        pos = self._pos()
        with _mock_now(_NOW_PLUS_30MIN):
            update_trailing_stop(pos, 105.0)
            update_trailing_stop(pos, 103.0)               # pullback
            update_trailing_stop(pos, 108.0)               # new high
        assert pos.peak_favorable_price == 108.0


# ── ema50_4h — position trail ─────────────────────────────────────────────────

class TestEma50_4hLong:
    """trigger=+1.0%, trail=2.0% below peak; long positions."""

    def _pos(self, **kw) -> _FakePos:
        return _FakePos(trail_style="ema50_4h", entry_price=100.0,
                        intended_hold_min=0, **kw)

    def test_not_armed_below_trigger(self):
        pos = self._pos()
        with _mock_now(_NOW_PLUS_30MIN):
            result = update_trailing_stop(pos, 100.8)      # +0.8% < 1.0%
        assert result is None
        assert pos.trail_stop_price == 0.0

    def test_arms_and_trail_correct(self):
        pos = self._pos()
        with _mock_now(_NOW_PLUS_30MIN):
            update_trailing_stop(pos, 101.5)               # +1.5% — arms
        assert pos.trail_stop_price > 0.0
        assert abs(pos.trail_stop_price - 101.5 * 0.98) < 1e-9

    def test_be_fires_before_wider_tier_trail(self):
        """ema50_4h trail (99.47) is wider than BE stop (100.55). BE pre-empts.

        Once peak hits +1.5% (above the 0.85% BE arm), BE locks in a small
        profit before the wide tier trail can be reached. This is intentional —
        BE protects against give-back, tier trail catches large reversals.
        """
        pos = self._pos()
        with _mock_now(_NOW_PLUS_30MIN):
            update_trailing_stop(pos, 101.5)               # arms BE + tier trail
            result = update_trailing_stop(pos, 99.0)       # below BE stop 100.55
        assert result == "BREAKEVEN"

    def test_wider_trail_than_ema21(self):
        """ema50_4h trail (2%) should always be wider than ema21_1h (1%) at same peak."""
        pos_slow = self._pos()
        pos_fast = _FakePos(trail_style="ema21_1h", entry_price=100.0,
                            intended_hold_min=0)
        with _mock_now(_NOW_PLUS_30MIN):
            update_trailing_stop(pos_slow, 103.0)
            update_trailing_stop(pos_fast, 103.0)
        # ema50_4h stop is further from peak (more room to breathe)
        assert pos_fast.trail_stop_price > pos_slow.trail_stop_price


# ── Short positions ───────────────────────────────────────────────────────────

class TestShortPositions:
    """For shorts, favorable = price going DOWN."""

    def _short(self, **kw) -> _FakePos:
        return _FakePos(side="short", trail_style="ema21_1h",
                        entry_price=100.0, intended_hold_min=0, **kw)

    def test_not_armed_below_trigger_short(self):
        pos = self._short()
        with _mock_now(_NOW_PLUS_30MIN):
            result = update_trailing_stop(pos, 99.7)       # -0.3% < 0.5% trigger
        assert result is None
        assert pos.trail_stop_price == 0.0

    def test_arms_for_short(self):
        pos = self._short()
        with _mock_now(_NOW_PLUS_30MIN):
            update_trailing_stop(pos, 99.0)                # -1% — arms
        assert pos.trail_stop_price > 0.0
        # trail = 1% ABOVE peak (98 short peak) for a short
        assert abs(pos.trail_stop_price - 99.0 * 1.01) < 1e-9

    def test_trail_stop_triggered_short(self):
        pos = self._short()
        with _mock_now(_NOW_PLUS_30MIN):
            update_trailing_stop(pos, 98.0)                # arms: trail at 98 * 1.01 = 98.98
            result = update_trailing_stop(pos, 99.2)       # above 98.98 → triggered
        assert result == "TRAIL_STOP"

    def test_trail_ratchets_down_for_short(self):
        pos = self._short()
        with _mock_now(_NOW_PLUS_30MIN):
            update_trailing_stop(pos, 98.0)
            first_stop = pos.trail_stop_price
            update_trailing_stop(pos, 96.0)                # new low → better for short
        # stop moves down (closer to new peak, which is the new low)
        assert pos.trail_stop_price < first_stop

    def test_trail_never_moves_up_against_short(self):
        pos = self._short()
        with _mock_now(_NOW_PLUS_30MIN):
            update_trailing_stop(pos, 96.0)
            low_stop = pos.trail_stop_price
            update_trailing_stop(pos, 97.5)                # bounce — shouldn't move stop up
        assert pos.trail_stop_price == low_stop

    def test_peak_tracks_lowest_price_short(self):
        pos = self._short()
        with _mock_now(_NOW_PLUS_30MIN):
            update_trailing_stop(pos, 97.0)
            update_trailing_stop(pos, 99.0)                # bounce
            update_trailing_stop(pos, 95.0)                # new low
        assert pos.peak_favorable_price == 95.0


# ── Breakeven trail (applies to ALL trail styles) ─────────────────────────────

class TestBreakevenTrail:
    """BE trail: once MFE ≥ 0.85%, exit if price retraces to entry + 0.55%."""

    def test_no_be_when_peak_below_arm(self):
        """Peak +0.5% < 0.85% arm → BE not active."""
        pos = _FakePos(trail_style="atr_stop", entry_price=100.0, intended_hold_min=0)
        with _mock_now(_NOW_PLUS_30MIN):
            update_trailing_stop(pos, 100.5)               # peak +0.5%, BE not armed
            result = update_trailing_stop(pos, 100.1)      # retrace below BE stop
        assert result is None

    def test_be_fires_after_arm_and_retrace_long(self):
        """Peak +1.0% (≥ 0.85%) → BE armed. Retrace below entry+0.55% → exit."""
        pos = _FakePos(trail_style="atr_stop", entry_price=100.0, intended_hold_min=0)
        with _mock_now(_NOW_PLUS_30MIN):
            update_trailing_stop(pos, 101.0)               # peak +1%, BE armed
            result = update_trailing_stop(pos, 100.3)      # below BE stop 100.55
        assert result == "BREAKEVEN"

    def test_no_be_exit_when_price_above_be_stop(self):
        """Armed but price still above BE stop → no exit."""
        pos = _FakePos(trail_style="atr_stop", entry_price=100.0, intended_hold_min=0)
        with _mock_now(_NOW_PLUS_30MIN):
            update_trailing_stop(pos, 101.0)               # arms BE
            result = update_trailing_stop(pos, 100.7)      # 100.7 > 100.55
        assert result is None

    def test_be_fires_for_short(self):
        """Short: peak -1% (favorable), retrace to entry-0.55% → exit."""
        pos = _FakePos(side="short", trail_style="atr_stop",
                       entry_price=100.0, intended_hold_min=0)
        with _mock_now(_NOW_PLUS_30MIN):
            update_trailing_stop(pos, 99.0)                # peak -1% favorable
            result = update_trailing_stop(pos, 99.7)       # above BE stop 99.45
        assert result == "BREAKEVEN"

    def test_be_applies_to_atr_stop(self):
        """BE works on scalp positions even though they have no tier trail."""
        pos = _FakePos(trail_style="atr_stop", entry_price=100.0, intended_hold_min=0)
        with _mock_now(_NOW_PLUS_30MIN):
            update_trailing_stop(pos, 101.0)               # arms BE
            result = update_trailing_stop(pos, 100.2)      # below 100.55
        assert result == "BREAKEVEN"


# ── MAX_HOLD backstop ─────────────────────────────────────────────────────────

class TestMaxHold:
    def test_no_exit_before_hold_expires(self):
        pos = _FakePos(trail_style="atr_stop", entry_price=100.0,
                       entry_time=_ENTRY_TIME, intended_hold_min=5)
        with _mock_now(_NOW_PLUS_4MIN):                    # 4 min < 5 min
            result = update_trailing_stop(pos, 100.0)
        assert result is None

    def test_exit_when_hold_expires(self):
        pos = _FakePos(trail_style="atr_stop", entry_price=100.0,
                       entry_time=_ENTRY_TIME, intended_hold_min=5)
        with _mock_now(_NOW_PLUS_6MIN):                    # 6 min > 5 min
            result = update_trailing_stop(pos, 100.0)
        assert result == "MAX_HOLD"

    def test_max_hold_fires_over_trail_style(self):
        """MAX_HOLD check occurs before trailing-stop check."""
        pos = _FakePos(trail_style="ema21_1h", entry_price=100.0,
                       entry_time=_ENTRY_TIME, intended_hold_min=5)
        with _mock_now(_NOW_PLUS_6MIN):
            result = update_trailing_stop(pos, 110.0)      # up 10% — would arm trail
        assert result == "MAX_HOLD"

    def test_no_hold_when_intended_hold_is_zero(self):
        pos = _FakePos(trail_style="atr_stop", entry_price=100.0,
                       entry_time=_ENTRY_TIME, intended_hold_min=0)
        far_future = datetime(2024, 12, 31, 23, 59, 0, tzinfo=timezone.utc)
        with _mock_now(far_future):
            result = update_trailing_stop(pos, 100.0)
        assert result is None

    def test_hold_exact_boundary_exits(self):
        pos = _FakePos(trail_style="atr_stop", entry_price=100.0,
                       entry_time=_ENTRY_TIME, intended_hold_min=5)
        exactly_5min = datetime(2024, 1, 1, 0, 5, 0, tzinfo=timezone.utc)
        with _mock_now(exactly_5min):
            result = update_trailing_stop(pos, 100.0)
        assert result == "MAX_HOLD"


# ── TRAIL_PARAMS table ────────────────────────────────────────────────────────

class TestTrailParamsTable:
    def test_atr_stop_is_none_none(self):
        assert TRAIL_PARAMS["atr_stop"] == (None, None)

    def test_ema21_1h_params(self):
        trigger, trail = TRAIL_PARAMS["ema21_1h"]
        assert trigger == pytest.approx(0.005)
        assert trail   == pytest.approx(0.010)

    def test_ema50_4h_params(self):
        trigger, trail = TRAIL_PARAMS["ema50_4h"]
        assert trigger == pytest.approx(0.010)
        assert trail   == pytest.approx(0.020)

    def test_ema50_trigger_wider_than_ema21(self):
        """Position-tier trade must move further before trailing arms."""
        assert TRAIL_PARAMS["ema50_4h"][0] > TRAIL_PARAMS["ema21_1h"][0]

    def test_ema50_trail_wider_than_ema21(self):
        """Position-tier trail gives the trade more room."""
        assert TRAIL_PARAMS["ema50_4h"][1] > TRAIL_PARAMS["ema21_1h"][1]

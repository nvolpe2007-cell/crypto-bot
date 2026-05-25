"""
Unit tests for src/microstructure_strategy.py

MicrostructureStrategy is the PRIMARY production strategy (paper_trading.py
uses it as the main entry/exit engine). It had zero test coverage before this
file was added.

Covers:
- MicrostructureStrategy.check_exit: all 7 exit conditions (SIGNAL_STOP,
  PRICE_STOP, T1_PARTIAL, breakeven stop after T1, T2, SIGNAL_FADE, TIME_STOP),
  no-exit case, legacy-signal skip
- MicrostructureStrategy.compute_size: formula verification, edge cases, caps
- MicrostructureStrategy._check_15m_structure: higher-lows / lower-highs logic,
  insufficient-data fail-open, flat data
- MicrostructureStrategy._score_ofi: direction match/mismatch, magnitude tiers,
  None state fallback
- MicrostructureStrategy._score_cvd: direction, price_responding, None state
- MicrostructureStrategy._score_lead_lag: aligned, opposing, no-signal
- MicrostructureStrategy._score_regime: each regime for buy and sell, confidence
  scaling
- MicrostructureStrategy.update_price: price and timestamp stored
- MicrostructureStrategy.update_candle: CVD tracker created and updated
- MicrostructureStrategy.update_book: OFI calculator created, prices stored
- _hold_micro: returns a HOLD MicrostructureSignal
"""

import time
import pytest
import numpy as np
import pandas as pd
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from src.microstructure_strategy import (
    MicrostructureStrategy,
    MicrostructureSignal,
    _hold_micro,
    _SIG_STOP_THRESH,
    _SIG_FADE_THRESH,
    _STOP_SPREAD_MULT,
    _T1_SPREAD_MULT,
    _T2_SPREAD_MULT,
    _TIME_STOP_SECS,
    _RISK_PER_TRADE,
    _REGIME_MULT,
    _OFI_STRONG_MULT,
    _STRUCTURE_SAMPLE,
    _STRUCTURE_LOWS_N,
    _ROUND_TRIP_COST_FRAC,
    _STOP_COST_MULT,
    _T2_COST_MULT,
)
from src.ofi_v2 import OFIState
from src.cvd_tracker import CVDState
from src.indicators import Signal


# ── Helpers ───────────────────────────────────────────────────────────────────

PRICE = 50_000.0
SPREAD = 10.0       # bid-ask spread in price units
SYMBOL = 'BTC/USD'


def _make_signal(
    side: str = 'buy',
    spread: float = SPREAD,
    t1_taken: bool = False,
    stop_at_breakeven: bool = False,
    **kwargs,
) -> MicrostructureSignal:
    """Return a minimal MicrostructureSignal for use as position.entry_signal."""
    is_buy = (side == 'buy')
    sig_enum = Signal.BUY if is_buy else Signal.SELL
    defaults = dict(
        signal=sig_enum, confidence=70.0, size_mult=1.0,
        ofi_score=20.0, lead_lag_score=10.0, regime_score=8.0,
        rsi_score=0.0, technical_score=10.0, funding_score=0.0,
        ofi=0.40, lead_lag_dir='BUY' if is_buy else 'SELL',
        regime='TRENDING_UP' if is_buy else 'TRENDING_DOWN',
        rsi=55.0, adx=28.0, atr=500.0, close=PRICE,
        ema_fast=PRICE - 100, ema_slow=PRICE - 200, volume_ratio=1.2,
        funding_rate=None,
        spread_at_entry=spread,
        ofi_norm_at_entry=0.40 if is_buy else -0.40,
        entry_time=time.time(),
        t1_taken=t1_taken,
        stop_at_breakeven=stop_at_breakeven,
        kill_reason='',
    )
    defaults.update(kwargs)
    return MicrostructureSignal(**defaults)


def _make_position(side: str = 'buy', entry_price: float = PRICE,
                   signal: MicrostructureSignal = None) -> MagicMock:
    """Return a PaperPosition-like mock with a MicrostructureSignal."""
    pos = MagicMock()
    pos.side = side
    pos.entry_price = entry_price
    pos.entry_signal = signal or _make_signal(side=side)
    return pos


def _make_ofi_state(
    ofi_norm: float = 0.40,
    ofi_accel: float = 0.05,
    ticks_above_threshold: int = 3,
    depth: float = 5.0,
    direction: int = 1,
) -> OFIState:
    """Build a minimal OFIState for testing."""
    return OFIState(
        ofi_norm=ofi_norm,
        ofi_accel=ofi_accel,
        ofi_raw=ofi_norm,
        depth=depth,
        ticks_above_threshold=ticks_above_threshold,
        state='signal',
        is_signal=True,
        direction=direction,
        timestamp=time.time(),
    )


def _make_cvd_state(
    direction: int = 1,
    price_responding: bool = True,
    cvd_slope: float = 100.0,
    seconds_since_aligned: float = 30.0,
) -> CVDState:
    """Build a minimal CVDState for testing."""
    return CVDState(
        cvd_now=500.0,
        cvd_slope=cvd_slope,
        cvd_direction=direction,
        price_responding=price_responding,
        seconds_since_aligned=seconds_since_aligned,
        last_candle_delta=50.0,
        candle_count=20,
    )


def _make_df(n: int = 100, base: float = PRICE, trend: float = 5.0) -> pd.DataFrame:
    """Return a minimal OHLCV DataFrame with a mild uptrend."""
    dates = pd.date_range('2024-01-01', periods=n, freq='1min')
    closes = base + np.arange(n) * trend
    return pd.DataFrame({
        'open':   closes * 0.9999,
        'high':   closes * 1.001,
        'low':    closes * 0.999,
        'close':  closes,
        'volume': np.full(n, 500.0),
    }, index=dates)


# ── _hold_micro ───────────────────────────────────────────────────────────────

class TestHoldMicro:
    def test_returns_microstructure_signal(self):
        sig = _hold_micro(PRICE, None, 'RANGING', None)
        assert isinstance(sig, MicrostructureSignal)

    def test_signal_is_hold(self):
        sig = _hold_micro(PRICE, None, 'RANGING', None)
        assert sig.signal == Signal.HOLD

    def test_confidence_is_zero(self):
        sig = _hold_micro(PRICE, None, 'UNKNOWN', None)
        assert sig.confidence == 0.0

    def test_size_mult_is_zero(self):
        sig = _hold_micro(PRICE, None, 'UNKNOWN', None)
        assert sig.size_mult == 0.0

    def test_kill_reason_stored(self):
        sig = _hold_micro(PRICE, None, 'RANGING', None, kill_reason='WS_STALE')
        assert sig.kill_reason == 'WS_STALE'

    def test_regime_stored(self):
        sig = _hold_micro(PRICE, None, 'VOLATILE', None)
        assert sig.regime == 'VOLATILE'


# ── MicrostructureStrategy.check_exit ─────────────────────────────────────────

class TestCheckExitNoPosition:
    def test_none_position_returns_none_tuple(self):
        strat = MicrostructureStrategy()
        reason, etype = strat.check_exit(SYMBOL, None, PRICE, 0.0, 0.0)
        assert reason is None and etype is None


class TestCheckExitLegacySignal:
    """Positions with non-MicrostructureSignal entry_signal must be skipped."""

    def test_non_micro_signal_returns_none(self):
        pos = MagicMock()
        pos.side = 'buy'
        pos.entry_price = PRICE
        pos.entry_signal = MagicMock(spec=[])   # not a MicrostructureSignal
        strat = MicrostructureStrategy()
        reason, etype = strat.check_exit(SYMBOL, pos, PRICE, 0.0, 10.0)
        assert reason is None and etype is None


class TestCheckExitSignalStop:
    """OFI flips hard in the opposite direction → SIGNAL_STOP (highest priority)."""

    def test_signal_stop_buy_position(self):
        # Long position; OFI goes strongly negative (opposing_ofi = negative)
        # opposing_ofi = current_ofi_norm * direction = -0.20 * 1 = -0.20 < -0.15
        pos = _make_position('buy')
        strat = MicrostructureStrategy()
        reason, etype = strat.check_exit(SYMBOL, pos, PRICE, -0.20, 10.0)
        assert reason == 'SIGNAL_STOP'
        assert etype == 'FULL'

    def test_signal_stop_short_position(self):
        # Short position; OFI goes strongly positive (opposing_ofi = ofi * -1)
        # opposing_ofi = current_ofi_norm * direction = 0.20 * -1 = -0.20 < -0.15
        pos = _make_position('short')
        strat = MicrostructureStrategy()
        reason, etype = strat.check_exit(SYMBOL, pos, PRICE, 0.20, 10.0)
        assert reason == 'SIGNAL_STOP'
        assert etype == 'FULL'

    def test_signal_stop_at_threshold_boundary(self):
        # opposing_ofi exactly at threshold should NOT trigger (strict <)
        pos = _make_position('buy')
        strat = MicrostructureStrategy()
        reason, etype = strat.check_exit(SYMBOL, pos, PRICE, _SIG_STOP_THRESH, 10.0)
        # _SIG_STOP_THRESH = -0.15; buy: opposing_ofi = -0.15 * 1 = -0.15
        # condition: opposing_ofi < _SIG_STOP_THRESH → -0.15 < -0.15 is False
        assert reason != 'SIGNAL_STOP'

    def test_signal_stop_just_below_threshold(self):
        # opposing_ofi = -0.16 < -0.15 → triggers
        pos = _make_position('buy')
        strat = MicrostructureStrategy()
        reason, etype = strat.check_exit(SYMBOL, pos, PRICE, -0.16, 10.0)
        assert reason == 'SIGNAL_STOP'


class TestCheckExitPriceStop:
    """Price moves 2.5× spread against entry → PRICE_STOP."""

    def test_price_stop_long_falls_too_far(self):
        spread = SPREAD
        # Code uses max(spread×mult, price×cost_frac×mult) — cost floor dominates at $50k
        stop_dist = max(spread * _STOP_SPREAD_MULT,
                        PRICE * _ROUND_TRIP_COST_FRAC * _STOP_COST_MULT)
        pos = _make_position('buy', entry_price=PRICE, signal=_make_signal('buy', spread=spread))
        bad_price = PRICE - stop_dist - 1.0
        strat = MicrostructureStrategy()
        reason, etype = strat.check_exit(SYMBOL, pos, bad_price, 0.05, 10.0)
        assert reason == 'PRICE_STOP'
        assert etype == 'FULL'

    def test_price_stop_short_rises_too_far(self):
        spread = SPREAD
        stop_dist = max(spread * _STOP_SPREAD_MULT,
                        PRICE * _ROUND_TRIP_COST_FRAC * _STOP_COST_MULT)
        pos = _make_position('short', entry_price=PRICE, signal=_make_signal('short', spread=spread))
        bad_price = PRICE + stop_dist + 1.0
        strat = MicrostructureStrategy()
        reason, etype = strat.check_exit(SYMBOL, pos, bad_price, -0.05, 10.0)
        assert reason == 'PRICE_STOP'
        assert etype == 'FULL'

    def test_no_price_stop_within_threshold(self):
        spread = SPREAD
        stop_dist = spread * _STOP_SPREAD_MULT   # = 25
        pos = _make_position('buy', entry_price=PRICE, signal=_make_signal('buy', spread=spread))
        # Price only dropped 20 — within 25 stop
        strat = MicrostructureStrategy()
        reason, etype = strat.check_exit(SYMBOL, pos, PRICE - 20.0, 0.05, 10.0)
        assert reason != 'PRICE_STOP'

    def test_price_stop_uses_fallback_spread_when_zero(self):
        """If spread_at_entry is 0, code uses 1bp fallback — should not crash."""
        sig = _make_signal('buy', spread=0.0)
        pos = _make_position('buy', entry_price=PRICE, signal=sig)
        strat = MicrostructureStrategy()
        # spread=0 → fallback = PRICE * 0.0001; cost floor dominates at $50k
        fallback_spread = PRICE * 0.0001
        stop_dist = max(fallback_spread * _STOP_SPREAD_MULT,
                        PRICE * _ROUND_TRIP_COST_FRAC * _STOP_COST_MULT)
        reason, etype = strat.check_exit(SYMBOL, pos, PRICE - stop_dist - 1.0, 0.05, 10.0)
        assert reason == 'PRICE_STOP'


class TestCheckExitT1Partial:
    """Price reaches T1 target (2× spread) → T1_PARTIAL (50% close)."""

    def test_t1_partial_long(self):
        spread = SPREAD
        # Code: max(spread×T1_mult, price×cost_frac) — cost floor dominates at $50k
        t1_dist = max(spread * _T1_SPREAD_MULT, PRICE * _ROUND_TRIP_COST_FRAC)
        sig = _make_signal('buy', spread=spread)
        pos = _make_position('buy', entry_price=PRICE, signal=sig)
        strat = MicrostructureStrategy()
        reason, etype = strat.check_exit(SYMBOL, pos, PRICE + t1_dist + 1.0, 0.40, 10.0)
        assert reason == 'T1_PARTIAL'
        assert etype == 'PARTIAL'

    def test_t1_partial_sets_flags(self):
        spread = SPREAD
        t1_dist = max(spread * _T1_SPREAD_MULT, PRICE * _ROUND_TRIP_COST_FRAC)
        sig = _make_signal('buy', spread=spread)
        pos = _make_position('buy', entry_price=PRICE, signal=sig)
        strat = MicrostructureStrategy()
        strat.check_exit(SYMBOL, pos, PRICE + t1_dist + 1.0, 0.40, 10.0)
        assert sig.t1_taken is True
        assert sig.stop_at_breakeven is True

    def test_t1_only_triggers_once(self):
        spread = SPREAD
        t1_dist = spread * _T1_SPREAD_MULT
        sig = _make_signal('buy', spread=spread, t1_taken=True)
        pos = _make_position('buy', entry_price=PRICE, signal=sig)
        strat = MicrostructureStrategy()
        reason, etype = strat.check_exit(SYMBOL, pos, PRICE + t1_dist + 1.0, 0.40, 10.0)
        assert reason != 'T1_PARTIAL'

    def test_t1_short_position(self):
        spread = SPREAD
        t1_dist = max(spread * _T1_SPREAD_MULT, PRICE * _ROUND_TRIP_COST_FRAC)
        sig = _make_signal('short', spread=spread)
        pos = _make_position('short', entry_price=PRICE, signal=sig)
        strat = MicrostructureStrategy()
        reason, etype = strat.check_exit(SYMBOL, pos, PRICE - t1_dist - 1.0, -0.40, 10.0)
        assert reason == 'T1_PARTIAL'
        assert etype == 'PARTIAL'


class TestCheckExitBreakevenStop:
    """After T1, if price returns below entry → PRICE_STOP (breakeven stop)."""

    def test_breakeven_stop_after_t1(self):
        sig = _make_signal('buy', spread=SPREAD, t1_taken=True, stop_at_breakeven=True)
        pos = _make_position('buy', entry_price=PRICE, signal=sig)
        strat = MicrostructureStrategy()
        # Price dropped below entry → price_delta < 0 → BE stop
        reason, etype = strat.check_exit(SYMBOL, pos, PRICE - 1.0, 0.40, 10.0)
        assert reason == 'PRICE_STOP'
        assert etype == 'FULL'

    def test_no_breakeven_stop_before_t1(self):
        sig = _make_signal('buy', spread=SPREAD, t1_taken=False, stop_at_breakeven=False)
        pos = _make_position('buy', entry_price=PRICE, signal=sig)
        strat = MicrostructureStrategy()
        # Price slightly below entry but T1 not yet taken — no BE stop
        reason, etype = strat.check_exit(SYMBOL, pos, PRICE - 1.0, 0.40, 10.0)
        # Only other possible exits here: price stop (price_delta = -1 vs stop = -25 → no)
        assert reason != 'PRICE_STOP'


class TestCheckExitT2:
    """Price reaches T2 target (4.5× spread) → T2 FULL exit."""

    def test_t2_long(self):
        spread = SPREAD
        # Code: max(spread×T2_mult, price×cost_frac×T2_cost_mult) — cost floor dominates
        t2_dist = max(spread * _T2_SPREAD_MULT,
                      PRICE * _ROUND_TRIP_COST_FRAC * _T2_COST_MULT)
        sig = _make_signal('buy', spread=spread, t1_taken=True)
        pos = _make_position('buy', entry_price=PRICE, signal=sig)
        strat = MicrostructureStrategy()
        reason, etype = strat.check_exit(SYMBOL, pos, PRICE + t2_dist + 1.0, 0.40, 10.0)
        assert reason == 'T2'
        assert etype == 'FULL'

    def test_t2_short(self):
        spread = SPREAD
        t2_dist = max(spread * _T2_SPREAD_MULT,
                      PRICE * _ROUND_TRIP_COST_FRAC * _T2_COST_MULT)
        sig = _make_signal('short', spread=spread, t1_taken=True)
        pos = _make_position('short', entry_price=PRICE, signal=sig)
        strat = MicrostructureStrategy()
        reason, etype = strat.check_exit(SYMBOL, pos, PRICE - t2_dist - 1.0, -0.40, 10.0)
        assert reason == 'T2'
        assert etype == 'FULL'


class TestCheckExitSignalFade:
    """After T1, OFI fades below 0.10 → SIGNAL_FADE."""

    def test_signal_fade_after_t1(self):
        sig = _make_signal('buy', spread=SPREAD, t1_taken=True, stop_at_breakeven=True)
        pos = _make_position('buy', entry_price=PRICE, signal=sig)
        strat = MicrostructureStrategy()
        # Price above entry (no BE stop), OFI faded to 0.05
        reason, etype = strat.check_exit(SYMBOL, pos, PRICE + 5.0, 0.05, 10.0)
        assert reason == 'SIGNAL_FADE'
        assert etype == 'FULL'

    def test_no_signal_fade_before_t1(self):
        sig = _make_signal('buy', spread=SPREAD, t1_taken=False)
        pos = _make_position('buy', entry_price=PRICE, signal=sig)
        strat = MicrostructureStrategy()
        reason, etype = strat.check_exit(SYMBOL, pos, PRICE + 5.0, 0.05, 10.0)
        assert reason != 'SIGNAL_FADE'

    def test_no_signal_fade_when_ofi_strong(self):
        sig = _make_signal('buy', spread=SPREAD, t1_taken=True, stop_at_breakeven=True)
        pos = _make_position('buy', entry_price=PRICE, signal=sig)
        strat = MicrostructureStrategy()
        # OFI still above fade threshold
        reason, etype = strat.check_exit(SYMBOL, pos, PRICE + 5.0, _SIG_FADE_THRESH + 0.05, 10.0)
        assert reason != 'SIGNAL_FADE'


class TestCheckExitTimeStop:
    """Position open too long without T1 → TIME_STOP."""

    def test_time_stop_without_t1(self):
        sig = _make_signal('buy', spread=SPREAD, t1_taken=False)
        pos = _make_position('buy', entry_price=PRICE, signal=sig)
        strat = MicrostructureStrategy()
        # Price within normal range (no other exit), time exceeded
        reason, etype = strat.check_exit(SYMBOL, pos, PRICE + 5.0, 0.30, _TIME_STOP_SECS + 1.0)
        assert reason == 'TIME_STOP'
        assert etype == 'FULL'

    def test_no_time_stop_when_t1_already_taken(self):
        sig = _make_signal('buy', spread=SPREAD, t1_taken=True, stop_at_breakeven=False)
        pos = _make_position('buy', entry_price=PRICE, signal=sig)
        strat = MicrostructureStrategy()
        # Even with time exceeded, T1 was hit so we let it run
        reason, etype = strat.check_exit(SYMBOL, pos, PRICE + 5.0, 0.30, _TIME_STOP_SECS + 1.0)
        assert reason != 'TIME_STOP'

    def test_no_time_stop_before_threshold(self):
        sig = _make_signal('buy', spread=SPREAD, t1_taken=False)
        pos = _make_position('buy', entry_price=PRICE, signal=sig)
        strat = MicrostructureStrategy()
        reason, etype = strat.check_exit(SYMBOL, pos, PRICE + 5.0, 0.30, _TIME_STOP_SECS - 1.0)
        assert reason != 'TIME_STOP'


class TestCheckExitNoTrigger:
    """Position inside all thresholds → (None, None)."""

    def test_healthy_long_no_exit(self):
        spread = SPREAD
        sig = _make_signal('buy', spread=spread)
        pos = _make_position('buy', entry_price=PRICE, signal=sig)
        strat = MicrostructureStrategy()
        # OFI still aligned (+0.35), price slightly up, time short
        reason, etype = strat.check_exit(SYMBOL, pos, PRICE + 5.0, 0.35, 10.0)
        assert reason is None and etype is None

    def test_healthy_short_no_exit(self):
        spread = SPREAD
        sig = _make_signal('short', spread=spread)
        pos = _make_position('short', entry_price=PRICE, signal=sig)
        strat = MicrostructureStrategy()
        reason, etype = strat.check_exit(SYMBOL, pos, PRICE - 5.0, -0.35, 10.0)
        assert reason is None and etype is None


# ── MicrostructureStrategy.compute_size ───────────────────────────────────────

class TestComputeSize:
    def test_basic_formula(self):
        strat = MicrostructureStrategy()
        equity = 1_000.0
        spread = 10.0
        price  = 50_000.0
        result = strat.compute_size(equity, 0.30, 'TRENDING_UP', spread, price)
        stop_dist = spread * _STOP_SPREAD_MULT   # 25.0
        size_units = (equity * _RISK_PER_TRADE) / stop_dist  # (1000*0.005)/25 = 0.2
        size_usd   = size_units * price             # 0.2 * 50000 = 10000
        regime_mult = _REGIME_MULT['TRENDING_UP']   # 1.0
        expected = min(size_usd * regime_mult, equity * 0.03)  # min(10000, 30)
        assert result == pytest.approx(expected, rel=1e-6)

    def test_zero_spread_returns_zero(self):
        strat = MicrostructureStrategy()
        assert strat.compute_size(1_000.0, 0.30, 'TRENDING_UP', 0.0, PRICE) == 0.0

    def test_zero_equity_returns_zero(self):
        strat = MicrostructureStrategy()
        assert strat.compute_size(0.0, 0.30, 'TRENDING_UP', 10.0, PRICE) == 0.0

    def test_zero_price_returns_zero(self):
        strat = MicrostructureStrategy()
        assert strat.compute_size(1_000.0, 0.30, 'TRENDING_UP', 10.0, 0.0) == 0.0

    def test_crash_regime_returns_zero(self):
        strat = MicrostructureStrategy()
        result = strat.compute_size(1_000.0, 0.30, 'CRASH', 10.0, PRICE)
        assert result == 0.0   # _REGIME_MULT['CRASH'] = 0.0

    def test_volatile_regime_reduces_size(self):
        strat = MicrostructureStrategy()
        # Use large spread so neither result hits the 3% cap, letting regime mult be visible
        normal   = strat.compute_size(1_000.0, 0.30, 'TRENDING_UP', 10_000.0, PRICE)
        volatile = strat.compute_size(1_000.0, 0.30, 'VOLATILE',    10_000.0, PRICE)
        assert volatile < normal

    def test_strong_ofi_bonus_increases_size(self):
        strat = MicrostructureStrategy()
        normal = strat.compute_size(1_000.0, 0.30, 'RANGING', 10.0, PRICE)
        strong = strat.compute_size(1_000.0, 0.60, 'RANGING', 10.0, PRICE)
        # ofi_norm=0.60 > 0.55 → regime_mult *= 1.2
        assert strong >= normal

    def test_three_pct_cap_applied(self):
        strat = MicrostructureStrategy()
        equity = 100.0
        # Use very small spread to generate large uncapped size
        result = strat.compute_size(equity, 0.30, 'TRENDING_UP', 0.001, PRICE)
        assert result <= equity * 0.03 + 1e-9

    def test_result_non_negative(self):
        strat = MicrostructureStrategy()
        for regime in ('TRENDING_UP', 'RANGING', 'VOLATILE', 'CRASH', 'UNKNOWN'):
            result = strat.compute_size(1_000.0, 0.30, regime, 10.0, PRICE)
            assert result >= 0.0


# ── MicrostructureStrategy._check_15m_structure ───────────────────────────────

class TestCheck15mStructure:
    MIN_BARS = _STRUCTURE_SAMPLE * _STRUCTURE_LOWS_N + _STRUCTURE_SAMPLE  # = 60

    def _make_higher_lows_df(self) -> pd.DataFrame:
        """Build a DataFrame where every 15th bar's low is strictly higher."""
        n = self.MIN_BARS + _STRUCTURE_SAMPLE
        dates = pd.date_range('2024-01-01', periods=n, freq='1min')
        base = 50_000.0
        trend = 50.0
        lows   = base + np.arange(n) * trend * 0.998
        closes = base + np.arange(n) * trend
        return pd.DataFrame({
            'open':   closes * 0.9998,
            'high':   closes * 1.001,
            'low':    lows,
            'close':  closes,
            'volume': np.full(n, 500.0),
        }, index=dates)

    def _make_lower_highs_df(self) -> pd.DataFrame:
        """Build a DataFrame where every 15th bar's high is strictly lower."""
        n = self.MIN_BARS + _STRUCTURE_SAMPLE
        dates = pd.date_range('2024-01-01', periods=n, freq='1min')
        base = 50_000.0
        trend = -50.0
        highs  = base + np.arange(n) * trend * 1.001
        closes = base + np.arange(n) * trend
        return pd.DataFrame({
            'open':   closes * 0.9998,
            'high':   highs,
            'low':    closes * 0.999,
            'close':  closes,
            'volume': np.full(n, 500.0),
        }, index=dates)

    def test_higher_lows_confirms_long(self):
        strat = MicrostructureStrategy()
        df = self._make_higher_lows_df()
        assert strat._check_15m_structure(df, direction=1) is True

    def test_lower_highs_confirms_short(self):
        strat = MicrostructureStrategy()
        df = self._make_lower_highs_df()
        assert strat._check_15m_structure(df, direction=-1) is True

    def test_insufficient_bars_returns_true(self):
        """Fail-open when not enough data."""
        strat = MicrostructureStrategy()
        df = _make_df(n=30)   # too few bars
        assert strat._check_15m_structure(df, direction=1) is True

    def test_flat_data_fails_higher_lows(self):
        """Flat prices have equal lows — not strictly higher → False."""
        n = self.MIN_BARS + _STRUCTURE_SAMPLE
        dates = pd.date_range('2024-01-01', periods=n, freq='1min')
        df = pd.DataFrame({
            'open':   np.full(n, PRICE),
            'high':   np.full(n, PRICE * 1.001),
            'low':    np.full(n, PRICE * 0.999),
            'close':  np.full(n, PRICE),
            'volume': np.full(n, 500.0),
        }, index=dates)
        strat = MicrostructureStrategy()
        assert strat._check_15m_structure(df, direction=1) is False

    def test_downtrend_fails_long_structure(self):
        """Downtrending data → lows are descending → higher-lows check fails."""
        strat = MicrostructureStrategy()
        df = _make_df(n=self.MIN_BARS + _STRUCTURE_SAMPLE, base=PRICE, trend=-50.0)
        assert strat._check_15m_structure(df, direction=1) is False


# ── Scoring helpers ───────────────────────────────────────────────────────────

class TestScoreOFI:
    def test_none_state_returns_neutral(self):
        strat = MicrostructureStrategy()
        score = strat._score_ofi(None, is_buy=True)
        assert score == 8.0   # defined neutral-positive

    def test_strong_aligned_buy_returns_30(self):
        strat = MicrostructureStrategy()
        state = _make_ofi_state(ofi_norm=0.60, direction=1)
        score = strat._score_ofi(state, is_buy=True)
        assert score == 30.0

    def test_moderate_aligned_returns_25(self):
        strat = MicrostructureStrategy()
        state = _make_ofi_state(ofi_norm=0.47, direction=1)
        score = strat._score_ofi(state, is_buy=True)
        assert score == 25.0

    def test_threshold_aligned_returns_20(self):
        strat = MicrostructureStrategy()
        state = _make_ofi_state(ofi_norm=0.35, direction=1)
        score = strat._score_ofi(state, is_buy=True)
        assert score == 20.0

    def test_weak_aligned_returns_12(self):
        strat = MicrostructureStrategy()
        state = _make_ofi_state(ofi_norm=0.27, direction=1)
        score = strat._score_ofi(state, is_buy=True)
        assert score == 12.0

    def test_very_weak_aligned_returns_5(self):
        strat = MicrostructureStrategy()
        state = _make_ofi_state(ofi_norm=0.10, direction=1)
        score = strat._score_ofi(state, is_buy=True)
        assert score == 5.0

    def test_strong_opposing_returns_negative_15(self):
        strat = MicrostructureStrategy()
        state = _make_ofi_state(ofi_norm=-0.40, direction=-1)
        score = strat._score_ofi(state, is_buy=True)   # buy, but OFI negative
        assert score == -15.0

    def test_moderate_opposing_returns_negative_8(self):
        strat = MicrostructureStrategy()
        state = _make_ofi_state(ofi_norm=-0.27, direction=-1)
        score = strat._score_ofi(state, is_buy=True)
        assert score == -8.0

    def test_weak_opposing_returns_negative_3(self):
        strat = MicrostructureStrategy()
        state = _make_ofi_state(ofi_norm=-0.10, direction=-1)
        score = strat._score_ofi(state, is_buy=True)
        assert score == -3.0


class TestScoreCVD:
    def test_none_returns_neutral_10(self):
        strat = MicrostructureStrategy()
        assert strat._score_cvd(None, direction=1) == 10.0

    def test_aligned_and_responding_returns_above_10(self):
        strat = MicrostructureStrategy()
        state = _make_cvd_state(direction=1, price_responding=True, cvd_slope=50.0)
        score = strat._score_cvd(state, direction=1)
        assert score > 10.0

    def test_aligned_not_responding_returns_5(self):
        strat = MicrostructureStrategy()
        state = _make_cvd_state(direction=1, price_responding=False)
        score = strat._score_cvd(state, direction=1)
        assert score == 5.0

    def test_opposing_returns_negative_5(self):
        strat = MicrostructureStrategy()
        state = _make_cvd_state(direction=-1, price_responding=True)
        score = strat._score_cvd(state, direction=1)   # direction mismatch
        assert score == -5.0

    def test_slope_magnitude_affects_score(self):
        strat = MicrostructureStrategy()
        low_slope  = _make_cvd_state(direction=1, price_responding=True, cvd_slope=10.0)
        high_slope = _make_cvd_state(direction=1, price_responding=True, cvd_slope=200.0)
        assert strat._score_cvd(high_slope, 1) > strat._score_cvd(low_slope, 1)


class TestScoreLeadLag:
    def test_no_signal_returns_zero(self):
        strat = MicrostructureStrategy()
        assert strat._score_lead_lag(lead_dir=0, direction=1) == 0.0

    def test_aligned_returns_at_least_10(self):
        strat = MicrostructureStrategy()
        score = strat._score_lead_lag(lead_dir=1, direction=1)
        assert score >= 10.0

    def test_aligned_returns_at_most_20(self):
        strat = MicrostructureStrategy()
        # time_remaining_ms() / 2000 maxes at 1.0 → 10 + 10 = 20
        with patch.object(strat.lead_lag, 'time_remaining_ms', return_value=2000):
            score = strat._score_lead_lag(lead_dir=1, direction=1)
        assert score <= 20.0

    def test_opposing_returns_negative_10(self):
        strat = MicrostructureStrategy()
        score = strat._score_lead_lag(lead_dir=-1, direction=1)
        assert score == -10.0

    def test_short_aligned_returns_positive(self):
        strat = MicrostructureStrategy()
        score = strat._score_lead_lag(lead_dir=-1, direction=-1)
        assert score >= 10.0


class TestScoreRegime:
    def test_trending_up_with_buy_returns_high(self):
        strat = MicrostructureStrategy()
        score = strat._score_regime('TRENDING_UP', is_buy=True, regime_conf=1.0)
        assert score == pytest.approx(10.0)

    def test_trending_up_with_sell_returns_zero(self):
        strat = MicrostructureStrategy()
        score = strat._score_regime('TRENDING_UP', is_buy=False, regime_conf=1.0)
        assert score == 0.0

    def test_trending_down_with_sell_returns_high(self):
        strat = MicrostructureStrategy()
        score = strat._score_regime('TRENDING_DOWN', is_buy=False, regime_conf=1.0)
        assert score == pytest.approx(10.0)

    def test_crash_returns_zero(self):
        strat = MicrostructureStrategy()
        score = strat._score_regime('CRASH', is_buy=True, regime_conf=1.0)
        assert score == 0.0

    def test_ranging_returns_intermediate(self):
        strat = MicrostructureStrategy()
        score = strat._score_regime('RANGING', is_buy=True, regime_conf=1.0)
        assert 0 < score < 10.0

    def test_regime_confidence_scales_score(self):
        strat = MicrostructureStrategy()
        low_conf  = strat._score_regime('TRENDING_UP', is_buy=True, regime_conf=0.0)
        high_conf = strat._score_regime('TRENDING_UP', is_buy=True, regime_conf=1.0)
        assert high_conf > low_conf

    def test_unknown_regime_returns_nonzero(self):
        strat = MicrostructureStrategy()
        score = strat._score_regime('UNKNOWN', is_buy=True, regime_conf=0.5)
        assert score > 0.0


# ── Data feed methods ─────────────────────────────────────────────────────────

class TestUpdatePrice:
    def test_price_stored(self):
        strat = MicrostructureStrategy()
        strat.update_price(SYMBOL, 51_000.0)
        assert strat._symbol_prices[SYMBOL] == 51_000.0

    def test_price_overwrites_previous(self):
        strat = MicrostructureStrategy()
        strat.update_price(SYMBOL, 50_000.0)
        strat.update_price(SYMBOL, 51_000.0)
        assert strat._symbol_prices[SYMBOL] == 51_000.0

    def test_last_price_time_set(self):
        strat = MicrostructureStrategy()
        before = time.time()
        strat.update_price(SYMBOL, 50_000.0)
        after = time.time()
        ts = strat._last_price_time[SYMBOL]
        assert before <= ts <= after

    def test_multiple_symbols(self):
        strat = MicrostructureStrategy()
        strat.update_price('BTC/USD', 50_000.0)
        strat.update_price('ETH/USD', 3_000.0)
        assert strat._symbol_prices['BTC/USD'] == 50_000.0
        assert strat._symbol_prices['ETH/USD'] == 3_000.0


class TestUpdateCandle:
    def _make_candle_dict(self, o=50_000.0, c=50_100.0, h=50_200.0,
                          lo=49_900.0, vol=500.0) -> dict:
        return {'open': o, 'close': c, 'high': h, 'low': lo, 'volume': vol,
                'timestamp': time.time()}

    def test_creates_cvd_tracker_for_new_symbol(self):
        strat = MicrostructureStrategy()
        candle = self._make_candle_dict()
        strat.update_candle(SYMBOL, candle)
        assert SYMBOL in strat._cvd_trackers

    def test_cvd_state_updated(self):
        strat = MicrostructureStrategy()
        candle = self._make_candle_dict()
        strat.update_candle(SYMBOL, candle)
        assert SYMBOL in strat._cvd_states

    def test_candle_volume_tracked(self):
        strat = MicrostructureStrategy()
        candle = self._make_candle_dict(vol=750.0)
        strat.update_candle(SYMBOL, candle)
        assert strat._candle_volume[SYMBOL] == 750.0

    def test_object_candle_accepted(self):
        """update_candle should accept attribute-based objects too."""
        strat = MicrostructureStrategy()
        candle = MagicMock()
        candle.open = 50_000.0
        candle.close = 50_100.0
        candle.high = 50_200.0
        candle.low = 49_900.0
        candle.volume = 300.0
        candle.timestamp = time.time()
        strat.update_candle(SYMBOL, candle)
        assert SYMBOL in strat._cvd_trackers

    def test_multiple_updates_accumulate(self):
        """Multiple candle updates should work without error."""
        strat = MicrostructureStrategy()
        for i in range(5):
            strat.update_candle(SYMBOL, self._make_candle_dict(
                o=50_000.0 + i, c=50_100.0 + i,
                h=50_200.0 + i, lo=49_900.0 + i, vol=500.0
            ))
        assert strat._cvd_states[SYMBOL].candle_count == 5


class TestUpdateBook:
    def test_creates_ofi_calculator_for_new_symbol(self):
        strat = MicrostructureStrategy()
        bids = [[50_000.0, 1.0], [49_990.0, 2.0]]
        asks = [[50_010.0, 1.0], [50_020.0, 2.0]]
        strat.update_book(SYMBOL, bids, asks, time.time())
        assert SYMBOL in strat._ofi_calcs

    def test_ofi_state_populated_after_update(self):
        strat = MicrostructureStrategy()
        bids = [[50_000.0, 1.0]]
        asks = [[50_010.0, 1.0]]
        strat.update_book(SYMBOL, bids, asks, time.time())
        # First update: OFI may be 0 (no prior snapshot to diff against)
        # but state should exist
        assert SYMBOL in strat.ofi_states

    def test_last_price_time_updated(self):
        strat = MicrostructureStrategy()
        bids = [[50_000.0, 1.0]]
        asks = [[50_010.0, 1.0]]
        before = time.time()
        ts = time.time()
        strat.update_book(SYMBOL, bids, asks, ts)
        assert strat._last_price_time[SYMBOL] == ts

    def test_lead_update_fires_for_btc(self):
        """When symbol is BTC/USD (lead), update_book should update lead_lag."""
        strat = MicrostructureStrategy()
        strat._symbol_prices['BTC/USD'] = 50_000.0
        bids = [[50_000.0, 5.0], [49_990.0, 3.0]]
        asks = [[50_010.0, 5.0], [50_020.0, 3.0]]
        # Should not raise
        strat.update_book('BTC/USD', bids, asks, time.time())


# ── MicrostructureStrategy.update_volume_sma ─────────────────────────────────

class TestUpdateVolumeSma:
    def test_stores_sma(self):
        strat = MicrostructureStrategy()
        strat.update_volume_sma(SYMBOL, 450.0)
        assert strat._volume_sma20[SYMBOL] == 450.0

    def test_overwrites_previous(self):
        strat = MicrostructureStrategy()
        strat.update_volume_sma(SYMBOL, 400.0)
        strat.update_volume_sma(SYMBOL, 450.0)
        assert strat._volume_sma20[SYMBOL] == 450.0

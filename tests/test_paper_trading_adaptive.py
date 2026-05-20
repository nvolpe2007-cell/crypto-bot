"""
Unit tests for the adaptive-threshold and utility functions in src/paper_trading.py.

Covers:
- _update_streaks_and_adapt: win/loss streak tracking, min_confidence adjustments,
  caps and floors, counter increments
- _inject_live_price: close/high/low replacement, immutability of original DataFrame
- _sanitize: NaN/Inf replacement, nested traversal, passthrough of normal values
- _diagnose: issue/positive classification based on signal context
"""

import math
import copy
import pytest
import pandas as pd
import numpy as np
from datetime import datetime, timezone
from unittest.mock import MagicMock

from src.paper_trading import (
    _update_streaks_and_adapt,
    _adapt,
    _inject_live_price,
    _sanitize,
    _diagnose,
)
from src.indicators import Signal


# ── Fixtures ───────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def reset_adapt():
    """Restore global _adapt to defaults before every test."""
    original = copy.deepcopy(_adapt)
    yield
    _adapt.update(original)
    # Clear any extra keys added during the test
    for k in list(_adapt.keys()):
        if k not in original:
            del _adapt[k]


def _make_signal(**kwargs):
    """Return a minimal ScientificSignal-like mock with sensible defaults."""
    from src.scientific_strategy import ScientificSignal
    defaults = dict(
        signal=Signal.BUY, confidence=75.0, size_mult=1.0,
        ofi_score=10.0, lead_lag_score=5.0, regime_score=10.0,
        rsi_score=8.0, technical_score=8.0, funding_score=0.0,
        ofi=0.20, lead_lag_dir='BUY', regime='TRENDING_UP',
        rsi=55.0, adx=28.0, atr=500.0, close=50_000.0,
        ema_fast=49_900.0, ema_slow=49_500.0, volume_ratio=1.2,
        funding_rate=None,
    )
    defaults.update(kwargs)
    return ScientificSignal(**defaults)


def _make_ohlcv_df(n: int = 5, close: float = 50_000.0) -> pd.DataFrame:
    """Create a minimal OHLCV DataFrame with n rows."""
    idx = pd.date_range('2024-01-01', periods=n, freq='1min')
    return pd.DataFrame({
        'open':   [close * 0.999] * n,
        'high':   [close * 1.002] * n,
        'low':    [close * 0.998] * n,
        'close':  [close] * n,
        'volume': [100.0] * n,
    }, index=idx)


# ── _update_streaks_and_adapt ──────────────────────────────────────────────────

class TestUpdateStreaksAndAdapt:
    def test_loss_increments_loss_streak(self):
        _adapt['loss_streak'] = 0
        _update_streaks_and_adapt(won=False, notifier=None)
        assert _adapt['loss_streak'] == 1

    def test_win_increments_win_streak(self):
        _adapt['win_streak'] = 0
        _update_streaks_and_adapt(won=True, notifier=None)
        assert _adapt['win_streak'] == 1

    def test_loss_resets_win_streak(self):
        _adapt['win_streak'] = 4
        _update_streaks_and_adapt(won=False, notifier=None)
        assert _adapt['win_streak'] == 0

    def test_win_resets_loss_streak(self):
        _adapt['loss_streak'] = 2
        _update_streaks_and_adapt(won=True, notifier=None)
        assert _adapt['loss_streak'] == 0

    def test_total_trades_increments_on_win(self):
        _adapt['total_trades'] = 5
        _update_streaks_and_adapt(won=True, notifier=None)
        assert _adapt['total_trades'] == 6

    def test_total_trades_increments_on_loss(self):
        _adapt['total_trades'] = 3
        _update_streaks_and_adapt(won=False, notifier=None)
        assert _adapt['total_trades'] == 4

    def test_total_wins_increments_only_on_win(self):
        _adapt['total_wins'] = 2
        _update_streaks_and_adapt(won=True, notifier=None)
        assert _adapt['total_wins'] == 3

    def test_total_wins_unchanged_on_loss(self):
        _adapt['total_wins'] = 2
        _update_streaks_and_adapt(won=False, notifier=None)
        assert _adapt['total_wins'] == 2

    def test_three_losses_raises_min_confidence(self):
        _adapt['loss_streak'] = 2
        _adapt['min_confidence'] = 38.0
        _update_streaks_and_adapt(won=False, notifier=None)  # 3rd loss
        # ceiling=45, step=+3: min(45, 38+3) = 41
        assert _adapt['min_confidence'] == pytest.approx(41.0)

    def test_min_confidence_capped_at_45(self):
        # AGGRESSIVE mode: ceiling is 45, step is +3
        _adapt['loss_streak'] = 2
        _adapt['min_confidence'] = 43.0  # +3 would overshoot 45
        _update_streaks_and_adapt(won=False, notifier=None)
        assert _adapt['min_confidence'] == pytest.approx(45.0)

    def test_min_confidence_not_raised_when_already_at_cap(self):
        # Condition is < 45, so at or above 45 no raise occurs
        _adapt['loss_streak'] = 2
        _adapt['min_confidence'] = 45.0
        _update_streaks_and_adapt(won=False, notifier=None)
        assert _adapt['min_confidence'] == pytest.approx(45.0)

    def test_five_wins_relaxes_min_confidence_when_above_60(self):
        _adapt['win_streak'] = 4
        _adapt['min_confidence'] = 65.0
        _update_streaks_and_adapt(won=True, notifier=None)  # 5th win
        assert _adapt['min_confidence'] == pytest.approx(63.0)

    def test_min_confidence_floor_at_35_on_relaxation(self):
        # AGGRESSIVE mode: floor is 35, step is -2
        _adapt['win_streak'] = 4
        _adapt['min_confidence'] = 36.5  # -2 would undershoot 35
        _update_streaks_and_adapt(won=True, notifier=None)
        assert _adapt['min_confidence'] == pytest.approx(35.0)

    def test_min_confidence_not_relaxed_when_already_at_35_or_below(self):
        # Condition is > 35, so at exactly 35 no relaxation occurs
        _adapt['win_streak'] = 4
        _adapt['min_confidence'] = 35.0
        _update_streaks_and_adapt(won=True, notifier=None)
        assert _adapt['min_confidence'] == pytest.approx(35.0)

    def test_returns_empty_list_when_no_threshold_change(self):
        _adapt['loss_streak'] = 0   # 1st loss, no adjustment yet
        changes = _update_streaks_and_adapt(won=False, notifier=None)
        assert changes == []

    def test_returns_nonempty_list_when_confidence_raised(self):
        _adapt['loss_streak'] = 2
        _adapt['min_confidence'] = 38.0
        changes = _update_streaks_and_adapt(won=False, notifier=None)
        assert len(changes) == 1
        assert 'raised' in changes[0]

    def test_returns_nonempty_list_when_confidence_relaxed(self):
        _adapt['win_streak'] = 4
        _adapt['min_confidence'] = 65.0
        changes = _update_streaks_and_adapt(won=True, notifier=None)
        assert len(changes) == 1
        assert 'relaxed' in changes[0]

    def test_consecutive_losses_only_trigger_on_third(self):
        """First and second losses do not change min_confidence."""
        _adapt['min_confidence'] = 38.0
        _adapt['loss_streak'] = 0
        _update_streaks_and_adapt(won=False, notifier=None)  # 1st loss
        assert _adapt['min_confidence'] == pytest.approx(38.0)
        _update_streaks_and_adapt(won=False, notifier=None)  # 2nd loss
        assert _adapt['min_confidence'] == pytest.approx(38.0)
        _update_streaks_and_adapt(won=False, notifier=None)  # 3rd loss — triggers
        # ceiling=45, step=+3: min(45, 38+3) = 41
        assert _adapt['min_confidence'] == pytest.approx(41.0)


# ── _inject_live_price ─────────────────────────────────────────────────────────

class TestInjectLivePrice:
    def test_close_replaced_with_live_price(self):
        df = _make_ohlcv_df(close=50_000.0)
        result = _inject_live_price(df, 51_000.0)
        assert result['close'].iloc[-1] == 51_000.0

    def test_high_updated_when_live_price_above_high(self):
        df = _make_ohlcv_df(close=50_000.0)
        high_before = float(df['high'].iloc[-1])   # 50_000 * 1.002 = 50_100
        result = _inject_live_price(df, high_before + 500.0)
        assert result['high'].iloc[-1] == high_before + 500.0

    def test_high_unchanged_when_live_price_below_high(self):
        df = _make_ohlcv_df(close=50_000.0)
        high_before = float(df['high'].iloc[-1])
        result = _inject_live_price(df, high_before - 100.0)
        assert result['high'].iloc[-1] == high_before

    def test_low_updated_when_live_price_below_low(self):
        df = _make_ohlcv_df(close=50_000.0)
        low_before = float(df['low'].iloc[-1])   # 50_000 * 0.998 = 49_900
        result = _inject_live_price(df, low_before - 500.0)
        assert result['low'].iloc[-1] == low_before - 500.0

    def test_low_unchanged_when_live_price_above_low(self):
        df = _make_ohlcv_df(close=50_000.0)
        low_before = float(df['low'].iloc[-1])
        result = _inject_live_price(df, low_before + 100.0)
        assert result['low'].iloc[-1] == low_before

    def test_original_dataframe_not_mutated(self):
        df = _make_ohlcv_df(close=50_000.0)
        original_close = float(df['close'].iloc[-1])
        _inject_live_price(df, 99_000.0)
        assert float(df['close'].iloc[-1]) == original_close

    def test_only_last_row_is_modified(self):
        df = _make_ohlcv_df(n=5, close=50_000.0)
        result = _inject_live_price(df, 99_000.0)
        # Rows 0-3 should be unchanged
        for i in range(4):
            assert result['close'].iloc[i] == 50_000.0

    def test_returns_dataframe_with_same_index(self):
        df = _make_ohlcv_df(n=3, close=50_000.0)
        result = _inject_live_price(df, 51_000.0)
        assert list(result.index) == list(df.index)


# ── _sanitize ──────────────────────────────────────────────────────────────────

class TestSanitize:
    def test_nan_float_replaced_with_none(self):
        assert _sanitize(float('nan')) is None

    def test_inf_float_replaced_with_none(self):
        assert _sanitize(float('inf')) is None

    def test_negative_inf_replaced_with_none(self):
        assert _sanitize(float('-inf')) is None

    def test_normal_float_preserved(self):
        assert _sanitize(3.14) == pytest.approx(3.14)

    def test_zero_float_preserved(self):
        assert _sanitize(0.0) == 0.0

    def test_integer_passed_through(self):
        assert _sanitize(42) == 42

    def test_string_passed_through(self):
        assert _sanitize("hello") == "hello"

    def test_none_passed_through(self):
        assert _sanitize(None) is None

    def test_dict_values_sanitized(self):
        result = _sanitize({'a': float('nan'), 'b': 1.0})
        assert result == {'a': None, 'b': 1.0}

    def test_dict_keys_preserved(self):
        result = _sanitize({'x': float('inf'), 'y': 'ok'})
        assert set(result.keys()) == {'x', 'y'}

    def test_list_elements_sanitized(self):
        result = _sanitize([float('nan'), 2.0, float('inf')])
        assert result == [None, 2.0, None]

    def test_nested_dict_sanitized_recursively(self):
        result = _sanitize({'outer': {'inner': float('nan')}})
        assert result == {'outer': {'inner': None}}

    def test_dict_in_list_sanitized(self):
        result = _sanitize([{'v': float('nan')}, {'v': 1.0}])
        assert result == [{'v': None}, {'v': 1.0}]

    def test_numpy_nan_via_float_is_sanitized(self):
        # float(np.nan) is still float('nan')
        assert _sanitize(float(np.nan)) is None


# ── _diagnose ─────────────────────────────────────────────────────────────────

class TestDiagnose:
    def test_ofi_confirms_buy_returns_positive(self):
        sig = _make_signal(ofi=0.25, signal=Signal.BUY)
        issues, positives = _diagnose('buy', pnl=10.0, exit_reason='SIGNAL',
                                      holding_min=5.0, sig=sig)
        assert any('OFI' in p for p in positives)

    def test_ofi_against_buy_returns_issue(self):
        sig = _make_signal(ofi=-0.20, signal=Signal.BUY)
        issues, positives = _diagnose('buy', pnl=-5.0, exit_reason='SIGNAL',
                                      holding_min=5.0, sig=sig)
        assert any('OFI' in i for i in issues)

    def test_weak_ofi_returns_issue(self):
        sig = _make_signal(ofi=0.05, signal=Signal.BUY)
        issues, positives = _diagnose('buy', pnl=2.0, exit_reason='SIGNAL',
                                      holding_min=5.0, sig=sig)
        assert any('OFI' in i for i in issues)

    def test_high_confidence_returns_positive(self):
        sig = _make_signal(confidence=92.0, ofi=None, lead_lag_dir=None)
        issues, positives = _diagnose('buy', pnl=5.0, exit_reason='SIGNAL',
                                      holding_min=5.0, sig=sig)
        assert any('conviction' in p.lower() or 'confidence' in p.lower() for p in positives)

    def test_low_confidence_returns_issue(self):
        sig = _make_signal(confidence=62.0, ofi=None, lead_lag_dir=None)
        issues, positives = _diagnose('buy', pnl=-2.0, exit_reason='SIGNAL',
                                      holding_min=5.0, sig=sig)
        assert any('confidence' in i.lower() for i in issues)

    def test_stop_loss_exit_returns_issue(self):
        sig = _make_signal(ofi=None, lead_lag_dir=None, confidence=75.0)
        issues, positives = _diagnose('buy', pnl=-10.0, exit_reason='STOP_LOSS',
                                      holding_min=3.0, sig=sig)
        assert any('stop' in i.lower() or 'stopped' in i.lower() for i in issues)

    def test_take_profit_exit_returns_positive(self):
        sig = _make_signal(ofi=None, lead_lag_dir=None, confidence=75.0)
        issues, positives = _diagnose('buy', pnl=15.0, exit_reason='TAKE_PROFIT',
                                      holding_min=10.0, sig=sig)
        assert any('target' in p.lower() or 'profit' in p.lower() for p in positives)

    def test_false_breakout_detected_for_quick_loss(self):
        sig = _make_signal(ofi=None, lead_lag_dir=None, confidence=75.0)
        issues, positives = _diagnose('buy', pnl=-3.0, exit_reason='SIGNAL',
                                      holding_min=1.0, sig=sig)
        assert any('breakout' in i.lower() or 'false' in i.lower() for i in issues)

    def test_trending_up_regime_aligned_with_buy_is_positive(self):
        sig = _make_signal(regime='TRENDING_UP', ofi=None, lead_lag_dir=None, confidence=75.0)
        issues, positives = _diagnose('buy', pnl=8.0, exit_reason='SIGNAL',
                                      holding_min=10.0, sig=sig)
        assert any('regime' in p.lower() or 'TRENDING_UP' in p for p in positives)

    def test_volatile_regime_returns_issue(self):
        sig = _make_signal(regime='VOLATILE', ofi=None, lead_lag_dir=None, confidence=75.0)
        issues, positives = _diagnose('buy', pnl=-5.0, exit_reason='SIGNAL',
                                      holding_min=5.0, sig=sig)
        assert any('volatile' in i.lower() or 'VOLATILE' in i for i in issues)

    def test_overbought_rsi_on_buy_returns_issue(self):
        sig = _make_signal(rsi=70.0, ofi=None, lead_lag_dir=None, regime='UNKNOWN',
                           confidence=75.0)
        issues, positives = _diagnose('buy', pnl=-2.0, exit_reason='SIGNAL',
                                      holding_min=5.0, sig=sig)
        assert any('rsi' in i.lower() or 'overbought' in i.lower() for i in issues)

    def test_healthy_rsi_on_buy_returns_positive(self):
        sig = _make_signal(rsi=45.0, ofi=None, lead_lag_dir=None, regime='UNKNOWN',
                           confidence=75.0)
        issues, positives = _diagnose('buy', pnl=5.0, exit_reason='SIGNAL',
                                      holding_min=5.0, sig=sig)
        assert any('rsi' in p.lower() or 'room' in p.lower() for p in positives)

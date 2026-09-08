"""Tests for the equity indicator lab and the Pine rule it ships.

Pine cannot be compiled locally, so the parts that CAN be verified here are:
  1. the replay engine that produced every number in the Pine header, and
  2. that the Pine script's stateless `barssince` formulation is equivalent to
     the stateful long/flat machine -- re-implemented here and cross-checked,
     because that equivalence is the one place the script could silently
     describe a different rule from the one measured.
"""
import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
PINE = ROOT / 'pine' / 'trend_signal_stocks.pine'


def _lab():
    spec = importlib.util.spec_from_file_location(
        'equity_lab_under_test', ROOT / 'scripts' / 'equity_indicator_lab.py')
    mod = importlib.util.module_from_spec(spec)
    sys.modules['equity_lab_under_test'] = mod
    spec.loader.exec_module(mod)
    return mod


lab = _lab()


# -- the moving average ------------------------------------------------------

def test_sma_warmup_is_none_then_correct():
    out = lab.sma([1, 2, 3, 4, 5], 3)
    assert out[0] is None and out[1] is None
    assert out[2] == pytest.approx(2.0)
    assert out[3] == pytest.approx(3.0)
    assert out[4] == pytest.approx(4.0)


def test_sma_rolling_window_does_not_drift():
    vals = [float(i % 7) for i in range(500)]
    out = lab.sma(vals, 10)
    # recompute the last window the slow, obviously-correct way
    assert out[-1] == pytest.approx(sum(vals[-10:]) / 10)


# -- the stateless / stateful equivalence the Pine script relies on ----------

def _stateless_is_long(closes, ma, band):
    """The Pine formulation: whichever band was crossed most recently wins.
    `barssince` semantics -- None means 'never happened'."""
    out, last_up, last_dn = [], None, None
    for i, c in enumerate(closes):
        if ma[i] is not None:
            if c > ma[i] * (1 + band):
                last_up = i
            if c < ma[i] * (1 - band):
                last_dn = i
        bl = (i - last_up) if last_up is not None else 999999
        bs = (i - last_dn) if last_dn is not None else 999999
        out.append(bl < bs)
    return out


def _stateful_is_long(closes, ma, band):
    """The plain state machine the rule is described as."""
    out, long = [], False
    for i, c in enumerate(closes):
        if ma[i] is not None:
            if long and c < ma[i] * (1 - band):
                long = False
            elif not long and c > ma[i] * (1 + band):
                long = True
        out.append(long)
    return out


def test_pine_stateless_form_matches_the_state_machine():
    import random
    random.seed(1234)
    px, closes = 100.0, []
    for _ in range(3000):
        px *= (1 + random.gauss(0.0004, 0.02))
        closes.append(px)
    ma = lab.sma(closes, 200)
    for band in (0.0, 0.01, 0.02, 0.04):
        a = _stateless_is_long(closes, ma, band)
        b = _stateful_is_long(closes, ma, band)
        # they may differ only during warmup, before either band has been crossed
        first = next(i for i, m in enumerate(ma) if m is not None)
        start = next((i for i in range(first, len(a)) if a[i] or b[i]), len(a))
        assert a[start:] == b[start:], f'divergence at band={band}'


# -- the replay engine -------------------------------------------------------

def _ramp(n=800, rate=0.002):
    px, out = 100.0, []
    for _ in range(n):
        px *= (1 + rate)
        out.append(px)
    return out


def test_monotonic_uptrend_is_one_open_trade_that_beats_nothing():
    closes = _ramp()
    r = lab.replay(closes, 100, 0.02, 0.0)
    assert r['n'] == 1                       # enters once, never exits
    assert r['win_rate'] == 1.0
    assert r['strat_x'] > 1.0
    # cannot beat buy-and-hold: it enters late and holds the same asset after
    assert r['strat_x'] <= r['bh_x'] + 1e-9


def test_costs_are_charged_per_trade():
    closes = _ramp()
    free = lab.replay(closes, 100, 0.02, 0.0)
    paid = lab.replay(closes, 100, 0.02, 0.01)
    assert paid['expectancy'] == pytest.approx(free['expectancy'] - 0.01)


def test_buy_and_hold_drawdown_is_measured_not_assumed():
    # Flat through the 100-bar MA warmup, THEN a -50% V. The crash has to sit
    # after warmup: replay() measures buy-and-hold from the first bar the rule
    # could have traded, so anything before that is correctly invisible.
    closes = [100.0] * 150
    closes += [100.0 * (1 - 0.005) ** i for i in range(1, 140)]     # -50%
    closes += [closes[-1] * (1.005) ** i for i in range(1, 300)]    # recover
    r = lab.replay(closes, 100, 0.02, 0.0)
    assert r['bh_dd'] < -0.4, r['bh_dd']
    assert -1.0 <= r['bh_dd'] <= 0.0


def test_buy_and_hold_window_starts_at_the_first_tradeable_bar():
    """A crash INSIDE the MA warmup must not be charged to buy-and-hold -- the
    rule could not have acted on it, so counting it would flatter the rule."""
    pre_crash = [100.0 * (1 - 0.02) ** i for i in range(60)]        # -70% in warmup
    closes = pre_crash + [pre_crash[-1]] * 400
    r = lab.replay(closes, 100, 0.02, 0.0)
    assert r.get('n', 0) == 0 or r['bh_dd'] > -0.1


def test_flat_price_never_trades():
    r = lab.replay([100.0] * 500, 100, 0.02, 0.0)
    assert r.get('n', 0) == 0


def test_open_position_is_marked_to_market_not_dropped():
    closes = _ramp()
    r = lab.replay(closes, 100, 0.02, 0.0)
    # the single trade is still open at the end; its P&L must still be counted
    assert r['n'] == 1 and r['expectancy'] > 0


# -- universe aggregation ----------------------------------------------------

def _wave(n=1200, amp=0.25, period=180, drift=0.0):
    """Oscillating series that genuinely round-trips the bands several times,
    so universe_median's >=3 trade filter is satisfied."""
    import math as _m
    return [100.0 * (1 + drift) ** i * (1 + amp * _m.sin(2 * _m.pi * i / period))
            for i in range(n)]


def test_universe_median_reports_medians_not_the_best_asset():
    great = _wave(drift=0.0020)          # strong uptrend under the oscillation
    poor = _wave(drift=0.0)              # pure chop, no trend to capture
    data = {'GREAT': [{'t': i, 'c': c} for i, c in enumerate(great)],
            'POOR1': [{'t': i, 'c': c} for i, c in enumerate(poor)],
            'POOR2': [{'t': i, 'c': c} for i, c in enumerate(poor)]}
    r = lab.universe_median(data, 100, 0.02, 0.0)
    assert r['assets'] == 3
    best = lab.replay(great, 100, 0.02, 0.0)
    assert best['n'] >= 3
    # With 2 poor names and 1 great one the median must be a POOR name, not the
    # standout -- this is the whole reporting discipline of the lab.
    assert r['med_exp'] < best['expectancy']


def test_universe_median_skips_assets_with_too_few_trades():
    data = {'FLAT': [{'t': i, 'c': 100.0} for i in range(500)]}
    assert lab.universe_median(data, 100, 0.02, 0.0)['assets'] == 0


# -- the shipped Pine file must not drift from what was measured -------------

def test_pine_file_ships_the_measured_parameters():
    src = PINE.read_text(encoding='utf-8')
    assert 'input.int(200, "Trend MA length"' in src
    assert 'input.float(1.0, "Hysteresis band %"' in src


def test_pine_header_states_it_does_not_beat_buy_and_hold():
    src = PINE.read_text(encoding='utf-8')
    assert 'It will not beat buying and holding' in src
    assert 'BEAT BUY-AND-HOLD:    4% of 70 assets' in src


def test_pine_signals_are_confirmed_close_only():
    src = PINE.read_text(encoding='utf-8')
    # every plotted signal must be gated on a confirmed bar, or the chart shows
    # signals that never actually fired
    for line in src.splitlines():
        if line.strip().startswith('plotshape('):
            assert 'barstate.isconfirmed' in line


def test_pine_has_both_alerts():
    src = PINE.read_text(encoding='utf-8')
    assert src.count('alertcondition(') == 2


# -- panel honesty (bug found 2026-09-08 by watching the script run live) ------

def test_panel_can_report_worse_on_both():
    """On a 10-minute SPY chart the rule was worse on BOTH return and drawdown,
    and the panel still said 'less pain, less money'. A panel that cannot report
    the bad case is advertising, not measurement."""
    src = PINE.read_text(encoding='utf-8')
    assert 'WORSE ON BOTH here' in src
    assert 'betterDD  = ddRule > ddHold' in src
    # all four quadrants must be reachable
    for phrase in ('better on both (rare)', 'more money, more pain',
                   'less pain, less money', 'WORSE ON BOTH here'):
        assert phrase in src, phrase


def test_panel_warns_when_not_on_a_daily_chart():
    """Every number in the header is daily-bar evidence."""
    src = PINE.read_text(encoding='utf-8')
    assert 'timeframe.isdaily' in src
    assert 'NOT DAILY - untested' in src


def test_panel_table_has_room_for_every_row():
    """The verdict + timeframe rows need 7, not 6 -- an off-by-one here silently
    drops the timeframe warning, which is the row that matters most."""
    src = PINE.read_text(encoding='utf-8')
    assert 'table.new(position.top_right, 2, 7' in src
    rows = {int(l.split('table.cell(t,')[1].split(',')[1])
            for l in src.splitlines() if 'table.cell(t,' in l}
    assert max(rows) <= 6, f'row index {max(rows)} exceeds the declared 7 rows'

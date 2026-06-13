"""Tests for the intraday regime-following arm (regime_arm.py)."""

import importlib
import pytest

import regime_arm as ra


@pytest.fixture(autouse=True)
def _reset():
    """Reload module so per-test monkeypatching of globals is isolated."""
    importlib.reload(ra)
    yield
    importlib.reload(ra)


# ── indicators ────────────────────────────────────────────────────────────────

def test_ema_warmup_then_value():
    vals = [1, 2, 3, 4, 5, 6]
    e = ra.ema(vals, 3)
    assert e[0] is None and e[1] is None
    assert e[2] == pytest.approx(2.0)          # seed = SMA of first 3
    assert e[3] is not None and e[3] > 2.0     # rises with the uptrend


def test_atr_pct_positive():
    highs = [10, 11, 12, 13, 14]
    lows = [9, 10, 11, 12, 13]
    closes = [9.5, 10.5, 11.5, 12.5, 13.5]
    a = ra.atr_pct_series(highs, lows, closes, n=3)
    assert a[-1] is not None and a[-1] > 0


# ── regime classification ───────────────────────────────────────────────────

def _arrays(n, ef, es, atr):
    return [ef] * n, [es] * n, [atr] * n


def test_classify_uptrend_is_long(monkeypatch):
    monkeypatch.setattr(ra, "SLOPE_WIN", 1)
    monkeypatch.setattr(ra, "ROUND_TRIP_COST", 0.005)
    monkeypatch.setattr(ra, "COST_MULT", 1.0)
    closes = [100, 101, 102, 103]
    ema_f = [None, 100.0, 100.5, 101.0]   # rising
    ema_s = [None, 99.0, 99.0, 99.0]      # fast above slow
    atrp = [None, 0.02, 0.02, 0.02]       # ATR 2% >> cost gate
    pos, label = ra.classify(3, closes, ema_f, ema_s, atrp)
    assert pos == 1 and label == "TRENDING_UP"


def test_classify_downtrend_is_short(monkeypatch):
    monkeypatch.setattr(ra, "SLOPE_WIN", 1)
    monkeypatch.setattr(ra, "ALLOW_SHORT", True)
    monkeypatch.setattr(ra, "COST_MULT", 1.0)
    closes = [100, 99, 98, 97]
    ema_f = [None, 100.0, 99.5, 99.0]     # falling
    ema_s = [None, 101.0, 101.0, 101.0]   # fast below slow
    atrp = [None, 0.02, 0.02, 0.02]
    pos, label = ra.classify(3, closes, ema_f, ema_s, atrp)
    assert pos == -1 and label == "TRENDING_DOWN"


def test_cost_gate_blocks_low_atr(monkeypatch):
    monkeypatch.setattr(ra, "SLOPE_WIN", 1)
    monkeypatch.setattr(ra, "ROUND_TRIP_COST", 0.005)
    monkeypatch.setattr(ra, "COST_MULT", 1.5)   # need ATR >= 0.75%
    closes = [100, 101, 102, 103]
    ema_f = [None, 100.0, 100.5, 101.0]
    ema_s = [None, 99.0, 99.0, 99.0]
    atrp = [None, 0.001, 0.001, 0.001]          # ATR 0.1% < gate → flat
    pos, label = ra.classify(3, closes, ema_f, ema_s, atrp)
    assert pos == 0 and label == "move<cost"


def test_shorts_disabled_stays_flat_in_downtrend(monkeypatch):
    monkeypatch.setattr(ra, "SLOPE_WIN", 1)
    monkeypatch.setattr(ra, "ALLOW_SHORT", False)
    monkeypatch.setattr(ra, "COST_MULT", 1.0)
    closes = [100, 99, 98, 97]
    ema_f = [None, 100.0, 99.5, 99.0]
    ema_s = [None, 101.0, 101.0, 101.0]
    atrp = [None, 0.02, 0.02, 0.02]
    pos, _ = ra.classify(3, closes, ema_f, ema_s, atrp)
    assert pos == 0


# ── backtest direction + P&L sign ────────────────────────────────────────────

def _ramp_bars(start, step, n, atr_frac=0.03):
    """Monotonic series with high/low bands so ATR clears the gate."""
    bars = []
    p = start
    for i in range(n):
        c = p
        bars.append({"t": i * 3600, "h": c * (1 + atr_frac), "l": c * (1 - atr_frac), "c": c})
        p += step
    return bars


def test_backtest_longs_and_profits_in_uptrend(monkeypatch):
    monkeypatch.setattr(ra, "EMA_FAST", 3)
    monkeypatch.setattr(ra, "EMA_SLOW", 5)
    monkeypatch.setattr(ra, "SLOPE_WIN", 2)
    monkeypatch.setattr(ra, "COST_MULT", 0.5)
    bars = _ramp_bars(100, +1.0, 60)          # steady uptrend
    m = ra.backtest(bars)
    assert m["longs"] >= 1 and m["shorts"] == 0
    assert m["total_ret"] > 0                  # rode the uptrend long


def test_backtest_shorts_and_profits_in_downtrend(monkeypatch):
    monkeypatch.setattr(ra, "EMA_FAST", 3)
    monkeypatch.setattr(ra, "EMA_SLOW", 5)
    monkeypatch.setattr(ra, "SLOPE_WIN", 2)
    monkeypatch.setattr(ra, "COST_MULT", 0.5)
    monkeypatch.setattr(ra, "ALLOW_SHORT", True)
    bars = _ramp_bars(200, -1.5, 60)          # steady downtrend
    m = ra.backtest(bars)
    assert m["shorts"] >= 1
    assert m["total_ret"] > 0                  # profited from shorting the fall


# ── forward state machine ────────────────────────────────────────────────────

def test_forward_seed_then_no_double_open(tmp_path, monkeypatch):
    monkeypatch.setattr(ra, "EMA_FAST", 3)
    monkeypatch.setattr(ra, "EMA_SLOW", 5)
    monkeypatch.setattr(ra, "SLOPE_WIN", 2)
    monkeypatch.setattr(ra, "COST_MULT", 0.5)
    bars = _ramp_bars(100, +1.0, 60)
    state = {"positions": {}, "closed": [], "last_bar_t": {},
             "starting_equity": 500.0, "equity": 500.0, "started_at": "x"}
    acted = ra.process_symbol("BTC", bars, state)
    assert acted == 0                          # inception seeds, takes no trade
    assert "BTC" in state["positions"]         # seeded a position (uptrend → long)
    assert state["positions"]["BTC"]["side"] == "long"
    # re-run with same bars: nothing new closed → no change
    acted2 = ra.process_symbol("BTC", bars, state)
    assert acted2 == 0


def test_close_pnl_sign(tmp_path, monkeypatch):
    monkeypatch.setattr(ra, "ROUND_TRIP_COST", 0.0)   # isolate the move
    state = {"positions": {"BTC": {"symbol": "BTC", "side": "long", "entry": 100.0,
                                    "entry_ts": "0", "size_usd": 100.0}},
             "closed": [], "equity": 500.0}
    rec = ra._close(state, "BTC", 110.0, "1", "test")  # +10% long
    assert rec["pnl"] == pytest.approx(10.0)
    # short profits when price falls
    state["positions"]["ETH"] = {"symbol": "ETH", "side": "short", "entry": 100.0,
                                 "entry_ts": "0", "size_usd": 100.0}
    rec2 = ra._close(state, "ETH", 90.0, "1", "test")  # -10% move, short → +10
    assert rec2["pnl"] == pytest.approx(10.0)

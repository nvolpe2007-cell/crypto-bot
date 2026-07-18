"""Tests for the low-turnover trend allocation forward arm (tsmom_paper.py)."""

import importlib
import pytest

import tsmom_paper as tp


@pytest.fixture(autouse=True)
def _reset():
    importlib.reload(tp)
    yield
    importlib.reload(tp)


def _bars(closes, start_t=1_000_000, step=86_400):
    return [{"t": start_t + i * step, "c": float(c)} for i, c in enumerate(closes)]


def test_target_position_long_above_band():
    assert tp._target_position(110, 100, False) is True


def test_target_position_cash_below_band():
    assert tp._target_position(90, 100, True) is False


def test_target_position_holds_in_deadzone():
    # 2% default band: price 101 vs sma 100 is inside the band either way
    assert tp._target_position(101, 100, True) is True
    assert tp._target_position(101, 100, False) is False


def test_seed_takes_long_no_trade(monkeypatch):
    monkeypatch.setattr(tp, "SMA_N", 3)
    state = {"positions": {}, "closed": [], "last_bar_t": {},
             "starting_equity": 1000.0, "equity": 1000.0}
    bars = _bars([100, 100, 100, 200])            # well above SMA -> seed LONG
    acted = tp.process_symbol("BTC", bars, state)
    assert acted == 0
    assert "BTC" in state["positions"]


def test_kill_switch_blocks_seed_entry(monkeypatch):
    monkeypatch.setattr(tp, "SMA_N", 3)
    monkeypatch.setattr(tp, "_is_killed", lambda: True)
    state = {"positions": {}, "closed": [], "last_bar_t": {},
             "starting_equity": 1000.0, "equity": 1000.0}
    bars = _bars([100, 100, 100, 200])            # well above SMA -> would seed LONG
    acted = tp.process_symbol("BTC", bars, state)
    assert acted == 0
    assert "BTC" not in state["positions"]        # seed suppressed by kill switch


def test_kill_switch_blocks_new_open_but_not_close(monkeypatch):
    monkeypatch.setattr(tp, "SMA_N", 3)
    monkeypatch.setattr(tp, "COST_FRAC", 0.0)
    closes = [100, 100, 100, 50, 200]              # seed CASH (below SMA), then break up
    bars = _bars(closes)
    state = {"positions": {}, "closed": [], "last_bar_t": {},
             "starting_equity": 1000.0, "equity": 1000.0}
    tp.process_symbol("BTC", bars[:4], state)      # inception seed -> CASH
    assert "BTC" not in state["positions"]

    monkeypatch.setattr(tp, "_is_killed", lambda: True)
    acted = tp.process_symbol("BTC", bars, state)  # would OPEN long, but killed
    assert acted == 0
    assert "BTC" not in state["positions"]

    # exits still work while killed: open one (unkilled) on a fresh bar, then
    # close on another fresh bar while killed again.
    monkeypatch.setattr(tp, "_is_killed", lambda: False)
    bars_open = _bars(closes + [210])              # still above SMA+band -> OPEN
    tp.process_symbol("BTC", bars_open, state)
    assert "BTC" in state["positions"]

    monkeypatch.setattr(tp, "_is_killed", lambda: True)
    bars2 = _bars(closes + [210, 10])              # sharp drop -> CLOSE
    acted = tp.process_symbol("BTC", bars2, state)
    assert acted == 1                              # exit not blocked by kill switch
    assert "BTC" not in state["positions"]

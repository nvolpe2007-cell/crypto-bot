"""Tests for the leveraged perp paper arm (lev_perp_paper.py), focused on the
master kill-switch wiring — exits (TP/liquidation/flip) must never be blocked,
only new entries."""

import importlib
import pytest

import lev_perp_paper as lp


@pytest.fixture(autouse=True)
def _reset():
    importlib.reload(lp)
    yield
    importlib.reload(lp)


def _bars(closes, start_t=1_000_000, step=86_400, hl_spread=1.0):
    return [{"t": start_t + i * step, "h": c + hl_spread, "l": c - hl_spread,
             "c": float(c), "v": 100.0} for i, c in enumerate(closes)]


def test_target_side_long_above_sma():
    assert lp._target_side(110, 100) == 1


def test_target_side_short_below_sma():
    assert lp._target_side(90, 100) == -1


def test_kill_switch_blocks_seed_entry(monkeypatch):
    monkeypatch.setattr(lp, "SMA_N", 3)
    monkeypatch.setattr(lp, "_entry_filter", lambda *a, **k: True)   # bypass filters
    monkeypatch.setattr(lp, "_is_killed", lambda: True)
    state = {"positions": {}, "closed": [], "last_bar_t": {},
             "starting_equity": 1000.0, "equity": 1000.0}
    bars = _bars([100, 100, 100, 110])
    acted = lp.process_symbol("BTC", bars, state)
    assert acted == 0
    assert "BTC" not in state["positions"]


def test_kill_switch_blocks_re_entry_but_not_flip_close(monkeypatch):
    monkeypatch.setenv("LEV_PERP_NOTIFY", "0")
    monkeypatch.setattr(lp, "SMA_N", 3)
    monkeypatch.setattr(lp, "TRADE_COST_FRAC", 0.0)
    monkeypatch.setattr(lp, "FUNDING_APY", 0.0)
    monkeypatch.setattr(lp, "_entry_filter", lambda *a, **k: True)   # bypass filters
    state = {"positions": {}, "closed": [], "last_bar_t": {},
             "starting_equity": 1000.0, "equity": 1000.0}
    closes = [100, 100, 100, 110]
    bars = _bars(closes)
    lp.process_symbol("BTC", bars, state)            # seed LONG @110 (unkilled)
    assert "BTC" in state["positions"]

    # next bar's close drops below SMA -> flip-close the long, then would re-enter SHORT
    bars2 = _bars(closes + [50])
    monkeypatch.setattr(lp, "_is_killed", lambda: True)
    acted = lp.process_symbol("BTC", bars2, state)
    assert acted == 1                                # the flip-close still happened
    assert "BTC" not in state["positions"]            # but the re-entry was blocked


def test_kill_switch_does_not_block_tp_or_liquidation_exit(monkeypatch):
    monkeypatch.setenv("LEV_PERP_NOTIFY", "0")
    monkeypatch.setattr(lp, "SMA_N", 3)
    monkeypatch.setattr(lp, "TRADE_COST_FRAC", 0.0)
    monkeypatch.setattr(lp, "FUNDING_APY", 0.0)
    monkeypatch.setattr(lp, "_entry_filter", lambda *a, **k: True)
    monkeypatch.setattr(lp, "_is_killed", lambda: True)
    state = {"positions": {}, "closed": [], "last_bar_t": {},
             "starting_equity": 1000.0, "equity": 1000.0}
    lp._open(state, "BTC", 1, 100.0, "1000000")      # already-open long, entered while live
    pos = state["positions"]["BTC"]
    bars = _bars([100, 100, 100, 100])               # warm-up; last bar's h/l ignored for SMA
    bars[-1]["h"] = pos["tp"] + 1                     # last bar touches take-profit
    state["last_bar_t"]["BTC"] = bars[-2]["t"]
    acted = lp.process_symbol("BTC", bars, state)
    assert acted == 1                                 # TP exit fires even while killed
    assert "BTC" not in state["positions"]
    assert state["closed"][0]["reason"] == "take_profit"

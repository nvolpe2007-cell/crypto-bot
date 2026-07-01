"""Tests for the focused BTC-only trend allocation arm (btc_trend_paper.py)."""

import importlib
import pytest

import btc_trend_paper as bt


@pytest.fixture(autouse=True)
def _reset():
    """Reload module so per-test monkeypatching of globals is isolated."""
    importlib.reload(bt)
    yield
    importlib.reload(bt)


def _bars(closes, start_t=1_000_000, step=86_400):
    """Daily bars (ascending) from a list of closes."""
    return [{"t": start_t + i * step, "c": float(c)} for i, c in enumerate(closes)]


# ── full-book sizing: the whole point vs the 1/3-slice conf arm ──────────────

def test_trade_size_is_full_book():
    assert bt.TRADE_SIZE == bt.STARTING_EQUITY      # BTC gets 100% of the book


# ── signal: confluence of trend AND momentum ────────────────────────────────

def test_want_long_warmup_returns_none(monkeypatch):
    monkeypatch.setattr(bt, "SMA_N", 5)
    monkeypatch.setattr(bt, "MOMO_N", 3)
    monkeypatch.setattr(bt, "WARMUP", 6)
    assert bt._want_long([1, 2, 3]) is None         # not enough history yet


def test_want_long_true_when_both_legs_agree(monkeypatch):
    monkeypatch.setattr(bt, "SMA_N", 5)
    monkeypatch.setattr(bt, "MOMO_N", 3)
    monkeypatch.setattr(bt, "WARMUP", 6)
    closes = [10, 10, 10, 10, 10, 20]               # last > SMA AND > close[-1-3]
    assert bt._want_long(closes) is True


def test_want_long_false_when_below_sma(monkeypatch):
    monkeypatch.setattr(bt, "SMA_N", 5)
    monkeypatch.setattr(bt, "MOMO_N", 3)
    monkeypatch.setattr(bt, "WARMUP", 6)
    closes = [100, 100, 100, 100, 100, 50]          # below SMA -> off even if momo
    assert bt._want_long(closes) is False


def test_want_long_false_when_momentum_down(monkeypatch):
    monkeypatch.setattr(bt, "SMA_N", 3)
    monkeypatch.setattr(bt, "MOMO_N", 2)
    monkeypatch.setattr(bt, "WARMUP", 4)
    closes = [1, 50, 40, 30]                        # below SMA -> off
    sma = sum(closes[-3:]) / 3                       # = 40; close 30 < 40
    assert closes[-1] < sma
    assert bt._want_long(closes) is False


# ── forward-only seeding ────────────────────────────────────────────────────

def test_first_run_seeds_cash_no_trade(monkeypatch):
    monkeypatch.setattr(bt, "SMA_N", 3)
    monkeypatch.setattr(bt, "MOMO_N", 2)
    monkeypatch.setattr(bt, "WARMUP", 4)
    state = {"positions": {}, "closed": [], "last_bar_t": {},
             "starting_equity": 1000.0, "equity": 1000.0}
    bars = _bars([100, 100, 100, 50])               # confluence off at inception
    acted = bt.process_symbol("BTC", bars, state)
    assert acted == 0
    assert "BTC" not in state["positions"]          # seeded cash
    assert state["last_bar_t"]["BTC"] == bars[-1]["t"]


def test_first_run_seeds_long_when_confluence_on(monkeypatch):
    monkeypatch.setattr(bt, "SMA_N", 3)
    monkeypatch.setattr(bt, "MOMO_N", 2)
    monkeypatch.setattr(bt, "WARMUP", 4)
    state = {"positions": {}, "closed": [], "last_bar_t": {},
             "starting_equity": 1000.0, "equity": 1000.0}
    bars = _bars([10, 10, 10, 30])                  # confluence ON at inception
    acted = bt.process_symbol("BTC", bars, state)
    assert acted == 0                               # seeding never books a trade
    assert "BTC" in state["positions"]              # but participates from inception
    assert state["positions"]["BTC"]["entry"] == 30.0


# ── acting on newly-closed bars + P&L accounting ────────────────────────────

def test_open_then_close_books_pnl_minus_cost(monkeypatch):
    monkeypatch.setattr(bt, "SMA_N", 3)
    monkeypatch.setattr(bt, "MOMO_N", 2)
    monkeypatch.setattr(bt, "WARMUP", 4)
    monkeypatch.setattr(bt, "COST_FRAC", 0.0)       # isolate the return math
    monkeypatch.setattr(bt, "TRADE_SIZE", 100.0)
    closes = [100, 100, 100, 90,    # seed: below SMA -> cash
              200,                   # >SMA & momentum up -> OPEN long @200
              50]                    # below SMA -> CLOSE @50
    bars = _bars(closes)
    state = {"positions": {}, "closed": [], "last_bar_t": {},
             "starting_equity": 1000.0, "equity": 1000.0}
    bt.process_symbol("BTC", bars[:4], state)       # inception seed (cash)
    bt.process_symbol("BTC", bars, state)           # advance through open + close
    assert len(state["closed"]) == 1
    rec = state["closed"][0]
    assert rec["entry"] == 200.0 and rec["exit"] == 50.0
    # ret = (50-200)/200 = -0.75 on $100 = -$75, zero cost
    assert rec["pnl"] == pytest.approx(-75.0)
    assert state["equity"] == pytest.approx(925.0)


def test_cost_is_charged_on_close(monkeypatch):
    monkeypatch.setattr(bt, "SMA_N", 3)
    monkeypatch.setattr(bt, "MOMO_N", 2)
    monkeypatch.setattr(bt, "WARMUP", 4)
    monkeypatch.setattr(bt, "COST_FRAC", 0.0054)
    monkeypatch.setattr(bt, "TRADE_SIZE", 1000.0)
    closes = [100, 100, 100, 90, 200, 200]          # open @200, flat close @200
    bars = _bars(closes)
    state = {"positions": {}, "closed": [], "last_bar_t": {},
             "starting_equity": 1000.0, "equity": 1000.0}
    bt.process_symbol("BTC", bars[:4], state)
    bt.process_symbol("BTC", bars[:5], state)        # OPEN @200
    # force a close by dropping below SMA on the next bar
    bars2 = _bars([100, 100, 100, 90, 200, 50])
    bt.process_symbol("BTC", bars2, state)
    rec = state["closed"][0]
    # ret = (50-200)/200 = -0.75 on $1000 = -$750, minus 0.54% of $1000 = -$5.40
    assert rec["pnl"] == pytest.approx(-755.40, abs=0.01)


def test_idempotent_no_double_act_on_same_bars(monkeypatch):
    monkeypatch.setattr(bt, "SMA_N", 3)
    monkeypatch.setattr(bt, "MOMO_N", 2)
    monkeypatch.setattr(bt, "WARMUP", 4)
    bars = _bars([10, 10, 10, 30, 40])              # confluence stays on
    state = {"positions": {}, "closed": [], "last_bar_t": {},
             "starting_equity": 1000.0, "equity": 1000.0}
    bt.process_symbol("BTC", bars, state)           # seed long
    before = dict(state["last_bar_t"])
    acted = bt.process_symbol("BTC", bars, state)   # same bars again
    assert acted == 0                               # nothing new to act on
    assert state["last_bar_t"] == before


# ── master kill switch: halts NEW entries, never exits ──────────────────────

def test_kill_switch_blocks_seed_entry(monkeypatch):
    monkeypatch.setattr(bt, "SMA_N", 3)
    monkeypatch.setattr(bt, "MOMO_N", 2)
    monkeypatch.setattr(bt, "WARMUP", 4)
    monkeypatch.setattr(bt, "_is_killed", lambda: True)
    state = {"positions": {}, "closed": [], "last_bar_t": {},
             "starting_equity": 1000.0, "equity": 1000.0}
    bars = _bars([10, 10, 10, 30])                  # confluence ON at inception
    acted = bt.process_symbol("BTC", bars, state)
    assert acted == 0
    assert "BTC" not in state["positions"]          # entry suppressed by kill switch


def test_kill_switch_blocks_new_open_but_not_close(monkeypatch):
    monkeypatch.setattr(bt, "SMA_N", 3)
    monkeypatch.setattr(bt, "MOMO_N", 2)
    monkeypatch.setattr(bt, "WARMUP", 4)
    closes = [100, 100, 100, 90, 200]               # seed cash, then confluence on
    bars = _bars(closes)
    state = {"positions": {}, "closed": [], "last_bar_t": {},
             "starting_equity": 1000.0, "equity": 1000.0}
    bt.process_symbol("BTC", bars[:4], state)       # inception seed (cash)
    monkeypatch.setattr(bt, "_is_killed", lambda: True)
    acted = bt.process_symbol("BTC", bars, state)   # would OPEN, but killed
    assert acted == 0
    assert "BTC" not in state["positions"]

    # exits still work while killed: open one (unkilled) on a fresh bar, then
    # close on another fresh bar while killed again.
    monkeypatch.setattr(bt, "_is_killed", lambda: False)
    bars_open = _bars(closes + [210])               # still confluence-on -> OPEN
    bt.process_symbol("BTC", bars_open, state)
    assert "BTC" in state["positions"]
    monkeypatch.setattr(bt, "_is_killed", lambda: True)
    bars2 = _bars(closes + [210, 50])                # below SMA -> CLOSE
    acted = bt.process_symbol("BTC", bars2, state)
    assert acted == 1                               # exit not blocked by kill switch
    assert "BTC" not in state["positions"]

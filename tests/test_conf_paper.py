"""Tests for the trend+momentum confluence forward arm (conf_paper.py)."""

import importlib
import pytest

import conf_paper as cp


@pytest.fixture(autouse=True)
def _reset():
    """Reload module so per-test monkeypatching of globals is isolated."""
    importlib.reload(cp)
    yield
    importlib.reload(cp)


def _bars(closes, start_t=1_000_000, step=86_400):
    """Daily bars (ascending) from a list of closes."""
    return [{"t": start_t + i * step, "c": float(c)} for i, c in enumerate(closes)]


# ── signal: confluence of trend AND momentum ────────────────────────────────

def test_want_long_warmup_returns_none(monkeypatch):
    monkeypatch.setattr(cp, "SMA_N", 5)
    monkeypatch.setattr(cp, "MOMO_N", 3)
    monkeypatch.setattr(cp, "WARMUP", 6)
    assert cp._want_long([1, 2, 3]) is None        # not enough history yet


def test_want_long_true_when_both_legs_agree(monkeypatch):
    monkeypatch.setattr(cp, "SMA_N", 5)
    monkeypatch.setattr(cp, "MOMO_N", 3)
    monkeypatch.setattr(cp, "WARMUP", 6)
    closes = [10, 10, 10, 10, 10, 20]               # last > SMA AND > close[-1-3]
    assert cp._want_long(closes) is True


def test_want_long_false_when_below_sma(monkeypatch):
    monkeypatch.setattr(cp, "SMA_N", 5)
    monkeypatch.setattr(cp, "MOMO_N", 3)
    monkeypatch.setattr(cp, "WARMUP", 6)
    closes = [100, 100, 100, 100, 100, 50]          # below SMA -> off even if momo
    assert cp._want_long(closes) is False


def test_want_long_false_when_momentum_down(monkeypatch):
    monkeypatch.setattr(cp, "SMA_N", 3)
    monkeypatch.setattr(cp, "MOMO_N", 2)
    monkeypatch.setattr(cp, "WARMUP", 4)
    # above the 3-SMA but 2-day momentum negative (close < close 2 bars ago)
    closes = [1, 50, 40, 30]
    sma = sum(closes[-3:]) / 3                       # = 40; close 30 < 40 -> below SMA
    assert closes[-1] < sma
    assert cp._want_long(closes) is False


# ── forward-only seeding ────────────────────────────────────────────────────

def test_first_run_seeds_cash_no_trade(monkeypatch):
    monkeypatch.setattr(cp, "SMA_N", 3)
    monkeypatch.setattr(cp, "MOMO_N", 2)
    monkeypatch.setattr(cp, "WARMUP", 4)
    state = {"positions": {}, "closed": [], "last_bar_t": {},
             "starting_equity": 1000.0, "equity": 1000.0}
    bars = _bars([100, 100, 100, 50])               # confluence off at inception
    acted = cp.process_symbol("BTC", bars, state)
    assert acted == 0
    assert "BTC" not in state["positions"]           # seeded cash
    assert state["last_bar_t"]["BTC"] == bars[-1]["t"]


def test_first_run_seeds_long_when_confluence_on(monkeypatch):
    monkeypatch.setattr(cp, "SMA_N", 3)
    monkeypatch.setattr(cp, "MOMO_N", 2)
    monkeypatch.setattr(cp, "WARMUP", 4)
    state = {"positions": {}, "closed": [], "last_bar_t": {},
             "starting_equity": 1000.0, "equity": 1000.0}
    bars = _bars([10, 10, 10, 30])                   # confluence ON at inception
    acted = cp.process_symbol("BTC", bars, state)
    assert acted == 0                                # seeding never books a trade
    assert "BTC" in state["positions"]               # but participates from inception
    assert state["positions"]["BTC"]["entry"] == 30.0


# ── acting on newly-closed bars + P&L accounting ────────────────────────────

def test_open_then_close_books_pnl_minus_cost(monkeypatch):
    monkeypatch.setattr(cp, "SMA_N", 3)
    monkeypatch.setattr(cp, "MOMO_N", 2)
    monkeypatch.setattr(cp, "WARMUP", 4)
    monkeypatch.setattr(cp, "COST_FRAC", 0.0)        # isolate the return math
    monkeypatch.setattr(cp, "TRADE_SIZE", 100.0)
    # seed cash on a flat-then-down series, then a clean break up (open), then collapse (close)
    closes = [100, 100, 100, 90,    # seed: below SMA -> cash
              200,                   # >SMA & momentum up -> OPEN long @200
              50]                    # below SMA -> CLOSE @50
    bars = _bars(closes)
    state = {"positions": {}, "closed": [], "last_bar_t": {},
             "starting_equity": 1000.0, "equity": 1000.0}
    cp.process_symbol("BTC", bars[:4], state)        # inception seed (cash)
    cp.process_symbol("BTC", bars, state)            # advance through open + close
    assert len(state["closed"]) == 1
    rec = state["closed"][0]
    assert rec["entry"] == 200.0 and rec["exit"] == 50.0
    # ret = (50-200)/200 = -0.75 on $100 = -$75, zero cost
    assert rec["pnl"] == pytest.approx(-75.0)
    assert state["equity"] == pytest.approx(925.0)


def test_idempotent_no_double_act_on_same_bars(monkeypatch):
    monkeypatch.setattr(cp, "SMA_N", 3)
    monkeypatch.setattr(cp, "MOMO_N", 2)
    monkeypatch.setattr(cp, "WARMUP", 4)
    bars = _bars([10, 10, 10, 30, 40])               # confluence stays on
    state = {"positions": {}, "closed": [], "last_bar_t": {},
             "starting_equity": 1000.0, "equity": 1000.0}
    cp.process_symbol("BTC", bars, state)            # seed long
    before = dict(state["last_bar_t"])
    acted = cp.process_symbol("BTC", bars, state)    # same bars again
    assert acted == 0                                # nothing new to act on
    assert state["last_bar_t"] == before

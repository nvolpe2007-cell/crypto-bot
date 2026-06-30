"""Tests for the conviction-scaled fractional-Kelly compounding arm (kelly_trend_paper.py)."""

import importlib
import pytest

import kelly_trend_paper as kt


@pytest.fixture(autouse=True)
def _reset():
    importlib.reload(kt)
    yield
    importlib.reload(kt)


def _bars(closes, start_t=1_000_000, step=86_400):
    return [{"t": start_t + i * step, "c": float(c)} for i, c in enumerate(closes)]


# ── conviction + fraction band ───────────────────────────────────────────────

def test_conviction_scales_with_momentum(monkeypatch):
    monkeypatch.setattr(kt, "MOMO_N", 2)
    monkeypatch.setattr(kt, "CONV_REF", 0.20)
    assert kt._conviction([100, 100, 100]) == 0.0          # flat → no conviction
    assert kt._conviction([100, 100, 110]) == pytest.approx(0.5)   # +10% vs 20% ref
    assert kt._conviction([100, 100, 130]) == 1.0          # +30% clamps to full


def test_fraction_never_leverages_by_default(monkeypatch):
    monkeypatch.setattr(kt, "MIN_FRAC", 0.25)
    monkeypatch.setattr(kt, "MAX_FRAC", 1.0)
    assert kt._fraction(0.0) == pytest.approx(0.25)        # weak trend → min band
    assert kt._fraction(1.0) == pytest.approx(1.0)         # full conviction → fully invested
    assert kt._fraction(5.0) <= kt.MAX_FRAC                # clamp holds above 1.0 → no leverage


# ── compounding: bet grows with equity ───────────────────────────────────────

def test_notional_is_fraction_of_current_equity(monkeypatch):
    monkeypatch.setattr(kt, "SMA_N", 3)
    monkeypatch.setattr(kt, "MOMO_N", 2)
    monkeypatch.setattr(kt, "WARMUP", 4)
    monkeypatch.setattr(kt, "CONV_REF", 0.20)
    monkeypatch.setattr(kt, "MIN_FRAC", 0.5)
    monkeypatch.setattr(kt, "MAX_FRAC", 1.0)
    state = {"positions": {}, "closed": [], "last_bar_t": {},
             "starting_equity": 1000.0, "equity": 2000.0}      # book has grown
    closes = [10, 10, 10, 12]                                   # +20% momo → full conviction
    kt._open(state, "BTC", 12.0, "1", closes)
    pos = state["positions"]["BTC"]
    # conviction = clamp(0.20/0.20)=1.0 → frac=1.0 → notional = 1.0 * 2000 (current equity)
    assert pos["fraction"] == pytest.approx(1.0)
    assert pos["size_usd"] == pytest.approx(2000.0)


def test_bigger_book_books_bigger_pnl(monkeypatch):
    """Same % move compounds: a larger equity → larger notional → larger $ P&L."""
    monkeypatch.setattr(kt, "MOMO_N", 2)
    monkeypatch.setattr(kt, "CONV_REF", 0.20)
    monkeypatch.setattr(kt, "MIN_FRAC", 1.0)            # force full deployment for a clean check
    monkeypatch.setattr(kt, "MAX_FRAC", 1.0)
    monkeypatch.setattr(kt, "COST_FRAC", 0.0)
    small = {"positions": {}, "closed": [], "equity": 1000.0, "starting_equity": 1000.0}
    big = {"positions": {}, "closed": [], "equity": 4000.0, "starting_equity": 1000.0}
    for st in (small, big):
        kt._open(st, "BTC", 100.0, "1", [100, 100, 100])
        kt._close(st, "BTC", 110.0, "2", "x")           # +10%
    assert small["closed"][0]["pnl"] == pytest.approx(100.0)    # 10% of $1000
    assert big["closed"][0]["pnl"] == pytest.approx(400.0)      # 10% of $4000 — compounded


# ── signal parity with the flat arm + forward seeding ────────────────────────

def test_seeds_cash_when_confluence_off(monkeypatch):
    monkeypatch.setattr(kt, "SMA_N", 3)
    monkeypatch.setattr(kt, "MOMO_N", 2)
    monkeypatch.setattr(kt, "WARMUP", 4)
    state = {"positions": {}, "closed": [], "last_bar_t": {},
             "starting_equity": 1000.0, "equity": 1000.0}
    acted = kt.process_symbol("BTC", _bars([100, 100, 100, 50]), state)
    assert acted == 0 and "BTC" not in state["positions"]


def test_open_then_close_compounds_equity(monkeypatch):
    monkeypatch.setattr(kt, "SMA_N", 3)
    monkeypatch.setattr(kt, "MOMO_N", 2)
    monkeypatch.setattr(kt, "WARMUP", 4)
    monkeypatch.setattr(kt, "COST_FRAC", 0.0)
    monkeypatch.setattr(kt, "MIN_FRAC", 1.0)
    monkeypatch.setattr(kt, "MAX_FRAC", 1.0)
    closes = [100, 100, 100, 90,    # seed cash
              200,                   # OPEN long (fully invested)
              250]                   # +25% then breaks down? no — stays; force close below SMA
    state = {"positions": {}, "closed": [], "last_bar_t": {},
             "starting_equity": 1000.0, "equity": 1000.0}
    kt.process_symbol("BTC", _bars(closes)[:4], state)         # seed
    kt.process_symbol("BTC", _bars(closes)[:5], state)         # OPEN @200
    kt.process_symbol("BTC", _bars([100, 100, 100, 90, 200, 50]), state)  # CLOSE @50
    assert len(state["closed"]) == 1
    # fully invested $1000 at 200 → exit 50 = -75% = -$750
    assert state["equity"] == pytest.approx(250.0)


def test_idempotent_no_double_act(monkeypatch):
    monkeypatch.setattr(kt, "SMA_N", 3)
    monkeypatch.setattr(kt, "MOMO_N", 2)
    monkeypatch.setattr(kt, "WARMUP", 4)
    bars = _bars([10, 10, 10, 30, 40])
    state = {"positions": {}, "closed": [], "last_bar_t": {},
             "starting_equity": 1000.0, "equity": 1000.0}
    kt.process_symbol("BTC", bars, state)
    before = dict(state["last_bar_t"])
    assert kt.process_symbol("BTC", bars, state) == 0
    assert state["last_bar_t"] == before


# ── master kill switch: halts NEW entries, never exits ──────────────────────

def test_kill_switch_blocks_seed_entry(monkeypatch):
    monkeypatch.setattr(kt, "SMA_N", 3)
    monkeypatch.setattr(kt, "MOMO_N", 2)
    monkeypatch.setattr(kt, "WARMUP", 4)
    monkeypatch.setattr(kt, "_is_killed", lambda: True)
    state = {"positions": {}, "closed": [], "last_bar_t": {},
             "starting_equity": 1000.0, "equity": 1000.0}
    acted = kt.process_symbol("BTC", _bars([10, 10, 10, 30]), state)
    assert acted == 0
    assert "BTC" not in state["positions"]


def test_kill_switch_blocks_new_open_but_not_close(monkeypatch):
    monkeypatch.setattr(kt, "SMA_N", 3)
    monkeypatch.setattr(kt, "MOMO_N", 2)
    monkeypatch.setattr(kt, "WARMUP", 4)
    closes = [100, 100, 100, 90, 200]               # seed cash, then confluence on
    bars = _bars(closes)
    state = {"positions": {}, "closed": [], "last_bar_t": {},
             "starting_equity": 1000.0, "equity": 1000.0}
    kt.process_symbol("BTC", bars[:4], state)       # inception seed (cash)
    monkeypatch.setattr(kt, "_is_killed", lambda: True)
    acted = kt.process_symbol("BTC", bars, state)   # would OPEN, but killed
    assert acted == 0
    assert "BTC" not in state["positions"]

    # exits still work while killed: open one (unkilled) on a fresh bar, then
    # close on another fresh bar while killed again.
    monkeypatch.setattr(kt, "_is_killed", lambda: False)
    bars_open = _bars(closes + [210])               # still confluence-on -> OPEN
    kt.process_symbol("BTC", bars_open, state)
    assert "BTC" in state["positions"]
    monkeypatch.setattr(kt, "_is_killed", lambda: True)
    bars2 = _bars(closes + [210, 50])                # below SMA -> CLOSE
    acted = kt.process_symbol("BTC", bars2, state)
    assert acted == 1
    assert "BTC" not in state["positions"]

"""Tests for the trend long/short perp forward arm (tsmom_ls_paper.py)."""

import importlib
import pytest

import tsmom_ls_paper as ls


@pytest.fixture(autouse=True)
def _reset():
    importlib.reload(ls)
    yield
    importlib.reload(ls)


def _bars(closes, start_t=1_000_000, step=86_400):
    return [{"t": start_t + i * step, "c": float(c)} for i, c in enumerate(closes)]


# ── signal: +1 above SMA, -1 below (pure sign with BAND=0) ──────────────────

def test_target_side_long_above_sma(monkeypatch):
    monkeypatch.setattr(ls, "BAND", 0.0)
    assert ls._target_side(110, 100, 0) == 1


def test_target_side_short_below_sma(monkeypatch):
    monkeypatch.setattr(ls, "BAND", 0.0)
    assert ls._target_side(90, 100, 0) == -1


def test_band_holds_current_side_in_deadzone(monkeypatch):
    monkeypatch.setattr(ls, "BAND", 0.05)        # 5% dead-zone around the SMA
    # price 102 is inside 100*(1±5%) -> hold whatever we had
    assert ls._target_side(102, 100, -1) == -1
    assert ls._target_side(102, 100, 1) == 1


# ── forward-only seeding always takes a side, never books a trade ────────────

def test_seed_takes_a_side_no_trade(monkeypatch):
    monkeypatch.setattr(ls, "SMA_N", 3)
    state = {"positions": {}, "closed": [], "last_bar_t": {},
             "starting_equity": 1000.0, "equity": 1000.0}
    bars = _bars([100, 100, 100, 80])            # below SMA -> seed SHORT
    acted = ls.process_symbol("BTC", bars, state)
    assert acted == 0 and len(state["closed"]) == 0
    assert state["positions"]["BTC"]["side"] == -1


# ── short P&L: profits when price falls ─────────────────────────────────────

def test_short_profits_when_price_falls(monkeypatch):
    monkeypatch.setattr(ls, "SMA_N", 3)
    monkeypatch.setattr(ls, "TRADE_COST_FRAC", 0.0)
    monkeypatch.setattr(ls, "FUNDING_APY", 0.0)
    monkeypatch.setattr(ls, "TRADE_SIZE", 100.0)
    # seed SHORT below SMA, then a clean break up flips to long and books the short
    closes = [100, 100, 100, 80,    # seed SHORT @80
              200]                   # >SMA -> flip: CLOSE short @200 (loss), open long
    bars = _bars(closes)
    state = {"positions": {}, "closed": [], "last_bar_t": {},
             "starting_equity": 1000.0, "equity": 1000.0}
    ls.process_symbol("BTC", bars[:4], state)    # seed short @80
    ls.process_symbol("BTC", bars, state)        # flip on the 200 bar
    rec = state["closed"][0]
    assert rec["side"] == -1 and rec["entry"] == 80.0 and rec["exit"] == 200.0
    # short ret = -1*(200-80)/80 = -1.5 on $100 = -$150
    assert rec["pnl"] == pytest.approx(-150.0)


def test_short_gain_on_decline(monkeypatch):
    monkeypatch.setattr(ls, "SMA_N", 3)
    monkeypatch.setattr(ls, "TRADE_COST_FRAC", 0.0)
    monkeypatch.setattr(ls, "FUNDING_APY", 0.0)
    monkeypatch.setattr(ls, "TRADE_SIZE", 100.0)
    closes = [50, 50, 50, 100,   # above SMA -> seed LONG @100
              40]                 # below SMA -> flip: CLOSE long @40 (loss)
    bars = _bars(closes)
    state = {"positions": {}, "closed": [], "last_bar_t": {},
             "starting_equity": 1000.0, "equity": 1000.0}
    ls.process_symbol("BTC", bars[:4], state)
    ls.process_symbol("BTC", bars, state)
    rec = state["closed"][0]
    assert rec["side"] == 1 and rec["entry"] == 100.0 and rec["exit"] == 40.0
    # long ret = (40-100)/100 = -0.6 on $100 = -$60
    assert rec["pnl"] == pytest.approx(-60.0)


# ── funding is charged as a cost over the holding period ─────────────────────

def test_funding_cost_charged_over_time(monkeypatch):
    size = 1000.0
    monkeypatch.setattr(ls, "FUNDING_APY", 0.10)     # 10% APY
    entry_t = 1_000_000
    exit_t = entry_t + 365 * 86_400                   # one full year held
    cost = ls._funding_cost(size, str(entry_t), str(exit_t))
    assert cost == pytest.approx(100.0, rel=1e-3)     # 10% of $1000 over a year


def test_notify_disabled_is_noop(monkeypatch):
    monkeypatch.setenv("TSMOM_LS_NOTIFY", "0")
    state = {"positions": {}, "closed": [], "equity": 1000.0,
             "starting_equity": 1000.0}
    ls._notify(state, {}, acted=5)                    # would otherwise fire
    assert "last_notify_ts" not in state             # never touched the notifier


def test_notify_sends_account_snapshot_on_trade(monkeypatch):
    monkeypatch.setenv("TSMOM_LS_NOTIFY", "1")
    sent = {}

    class _Fake:
        def send_message(self, msg, parse_mode="HTML"):
            sent["msg"] = msg
            return True

    import src.notifications as notif
    monkeypatch.setattr(notif, "create_notifier_from_env", lambda: _Fake())
    state = {"positions": {"BTC": {"symbol": "BTC", "side": -1, "entry": 100.0,
                                    "entry_ts": "1", "size_usd": 333.0}},
             "closed": [], "equity": 1000.0, "starting_equity": 1000.0}
    ls._notify(state, {"BTC": 80.0}, acted=1)         # a flip happened
    assert "Paper Account" in sent["msg"]
    assert "BTC" in sent["msg"] and "SHORT" in sent["msg"]
    assert state.get("last_notify_ts")                # records that it sent


def test_funding_reduces_pnl(monkeypatch):
    monkeypatch.setattr(ls, "SMA_N", 3)
    monkeypatch.setattr(ls, "TRADE_COST_FRAC", 0.0)
    monkeypatch.setattr(ls, "FUNDING_APY", 0.10)
    monkeypatch.setattr(ls, "TRADE_SIZE", 100.0)
    # long that goes nowhere in price but is held ~10 days -> only funding bleeds it
    step = 86_400
    closes = [50, 50, 50, 100] + [100] * 10 + [40]   # flat above SMA, then drop -> flip
    bars = _bars(closes, step=step)
    state = {"positions": {}, "closed": [], "last_bar_t": {},
             "starting_equity": 1000.0, "equity": 1000.0}
    ls.process_symbol("BTC", bars[:4], state)
    ls.process_symbol("BTC", bars, state)
    rec = state["closed"][0]
    assert rec["funding_cost"] > 0                    # funding was charged
    # net should be price P&L minus the funding cost
    price_ret = (rec["exit"] - rec["entry"]) / rec["entry"] * rec["size_usd"]
    assert rec["pnl"] == pytest.approx(price_ret - rec["funding_cost"], abs=1e-6)

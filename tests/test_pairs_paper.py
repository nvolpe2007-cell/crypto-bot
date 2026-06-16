"""Tests for pairs_paper.py — the market-neutral dollar-neutral pairs forward arm.

No network: feed synthetic close series and prices directly.
"""
import json
from datetime import datetime, timezone

import pytest

import pairs_paper as pp


def _state():
    return {"positions": {}, "closed": [], "last_bar_t": {}, "equity_curve": [],
            "starting_equity": 1000.0, "equity": 1000.0, "equity_mtm": 1000.0,
            "halted": False}


def test_zscore_basic():
    # flat series then a spike → large positive z
    s = [0.0] * 50 + [0.0, 0.0, 1.0]
    z = pp._zscore(s, 20)
    assert z is not None and z > 3


def test_spread_series_aligns_on_common_ts():
    a = {1: 100.0, 2: 110.0, 3: 121.0}
    b = {2: 50.0, 3: 50.0, 4: 50.0}        # only 2,3 common
    s = pp._spread_series(a, b)
    assert [t for t, _ in s] == [2, 3]


def test_open_is_dollar_neutral_and_directioned(monkeypatch):
    monkeypatch.setattr(pp, "LEG_NOTIONAL", 150.0)
    st = _state()
    prices = {"BTC": 60000.0, "ETH": 2000.0}
    # z>0 means a(BTC) rich vs b(ETH) → SHORT BTC, LONG ETH
    pp._open(st, ("BTC", "ETH"), z=2.5, prices=prices, ts="1000")
    p = st["positions"]["BTC-ETH"]
    assert p["short_sym"] == "BTC" and p["long_sym"] == "ETH"
    assert p["leg_notional"] == 150.0


def test_convergence_is_market_neutral_pnl(monkeypatch):
    # Both legs rise the same % (pure market beta) → neutral pnl ~ 0 before costs.
    monkeypatch.setattr(pp, "LEG_NOTIONAL", 100.0)
    st = _state()
    pp._open(st, ("BTC", "ETH"), z=2.5, prices={"BTC": 100.0, "ETH": 100.0}, ts="1000")
    # market +10% on both: long +10%, short -10% → nets to 0 gross
    gross = pp._leg_pnl(st["positions"]["BTC-ETH"], {"BTC": 110.0, "ETH": 110.0})
    assert gross == pytest.approx(0.0, abs=1e-6)


def test_convergence_profits_when_spread_closes(monkeypatch):
    monkeypatch.setattr(pp, "LEG_NOTIONAL", 100.0)
    monkeypatch.setattr(pp, "COST_FRAC", 0.0)
    monkeypatch.setattr(pp, "FUNDING_APY", 0.0)
    st = _state()
    # BTC rich → short BTC, long ETH. Then BTC falls, ETH rises = convergence → profit.
    pp._open(st, ("BTC", "ETH"), z=2.5, prices={"BTC": 100.0, "ETH": 100.0}, ts="0")
    rec = pp._close(st, "BTC-ETH", {"BTC": 95.0, "ETH": 105.0}, "3600", "converged")
    # long ETH +5% + short BTC +5% = +10% on $100/leg = +$10
    assert rec["pnl"] == pytest.approx(10.0)
    assert st["equity"] == pytest.approx(1010.0)


def test_costs_charge_four_legs(monkeypatch):
    monkeypatch.setattr(pp, "LEG_NOTIONAL", 100.0)
    monkeypatch.setattr(pp, "COST_FRAC", 0.0015)
    monkeypatch.setattr(pp, "FUNDING_APY", 0.0)
    st = _state()
    pp._open(st, ("BTC", "ETH"), z=2.5, prices={"BTC": 100.0, "ETH": 100.0}, ts="0")
    rec = pp._close(st, "BTC-ETH", {"BTC": 100.0, "ETH": 100.0}, "0", "converged")
    # no move, no funding → cost = 2 legs * 100 * 0.0015 = $0.30 loss
    assert rec["pnl"] == pytest.approx(-0.30)


def test_drawdown_stop_flattens_and_halts(monkeypatch):
    monkeypatch.setattr(pp, "LEG_NOTIONAL", 100.0)
    monkeypatch.setattr(pp, "COST_FRAC", 0.0)
    monkeypatch.setattr(pp, "FUNDING_APY", 0.0)
    monkeypatch.setattr(pp, "MAX_DRAWDOWN", 50.0)
    monkeypatch.setenv("PAIRS_NOTIFY", "0")
    st = _state()
    pp._open(st, ("BTC", "ETH"), z=2.5, prices={"BTC": 100.0, "ETH": 100.0}, ts="0")
    # spread blows out against us: short BTC but BTC +60%, long ETH but ETH -0% → -$60
    now = datetime.now(timezone.utc)
    engaged = pp.maybe_drawdown_stop(st, {"BTC": 160.0, "ETH": 100.0}, "1", now)
    assert engaged is True and st["halted"] is True and not st["positions"]


def test_process_opens_on_entry_z(monkeypatch):
    monkeypatch.setattr(pp, "LOOKBACK", 20)
    monkeypatch.setattr(pp, "ENTRY_Z", 2.0)
    monkeypatch.setattr(pp, "LEG_NOTIONAL", 100.0)
    st = _state()
    # Build BTC/ETH closes where the ratio spikes on the last bar → high |z|.
    ts = list(range(1, 25))
    btc = {t: 100.0 for t in ts}
    eth = {t: 100.0 for t in ts}
    eth[24] = 80.0          # ETH drops on last bar → ln(BTC/ETH) spikes up → z>0 → short BTC long ETH
    now = datetime.now(timezone.utc)
    acted = pp.process(st, {"BTC": btc, "ETH": eth, "SOL": {}},
                       {"BTC": 100.0, "ETH": 80.0, "SOL": 50.0}, now)
    assert acted >= 1
    assert "BTC-ETH" in st["positions"]
    p = st["positions"]["BTC-ETH"]
    assert p["short_sym"] == "BTC" and p["long_sym"] == "ETH"


def test_process_idempotent_same_bar(monkeypatch):
    monkeypatch.setattr(pp, "LOOKBACK", 20)
    monkeypatch.setattr(pp, "ENTRY_Z", 2.0)
    monkeypatch.setattr(pp, "LEG_NOTIONAL", 100.0)
    st = _state()
    ts = list(range(1, 25))
    btc = {t: 100.0 for t in ts}; eth = {t: 100.0 for t in ts}; eth[24] = 80.0
    now = datetime.now(timezone.utc)
    closes = {"BTC": btc, "ETH": eth, "SOL": {}}
    prices = {"BTC": 100.0, "ETH": 80.0, "SOL": 50.0}
    pp.process(st, closes, prices, now)
    acted2 = pp.process(st, closes, prices, now)     # same bar again
    assert acted2 == 0                                # no double-open

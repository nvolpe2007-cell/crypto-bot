"""Tests for the composable desk-context blocks (src/desk_blocks.py)."""

import importlib
import pytest

import src.desk_blocks as db


@pytest.fixture(autouse=True)
def _reset():
    importlib.reload(db)
    yield
    importlib.reload(db)


# ── cross-asset risk label (pure; no network) ────────────────────────────────

def test_risk_label_risk_on_off_mixed():
    assert db._risk_label(1.5, -0.3, -1.0) == "risk_on"     # stocks up, dollar down
    assert db._risk_label(-2.0, 0.8, 2.0) == "risk_off"     # stocks down, dollar up
    assert db._risk_label(1.0, 0.5, 0.0) == "mixed"         # both up
    assert db._risk_label(None, 0.5, 0.0) == "unknown"


# ── slow volume flow (poor-man's CVD) ────────────────────────────────────────

def test_buy_pressure_all_up_volume_is_positive():
    closes = [10, 11, 12, 13, 14]       # every day up
    vols = [100, 100, 100, 100, 100]
    assert db._buy_pressure(closes, vols, 4) == 1.0


def test_buy_pressure_all_down_is_negative():
    closes = [14, 13, 12, 11, 10]       # every day down
    vols = [100, 100, 100, 100, 100]
    assert db._buy_pressure(closes, vols, 4) == -1.0


def test_buy_pressure_net_of_up_and_down_volume():
    # up days carry 300 vol, down days 100 → (300-100)/400 = 0.5
    closes = [10, 11, 9, 12, 11]        # up, down, up, down
    vols = [0, 200, 50, 100, 50]        # up vols: 200+100=300; down vols: 50+50=100
    assert db._buy_pressure(closes, vols, 4) == pytest.approx(0.5)


def test_buy_pressure_none_when_too_short():
    assert db._buy_pressure([1, 2], [1, 1], 20) is None


def test_volume_flow_flags_bearish_divergence():
    # price ends well above 20 bars ago, but volume is all on DOWN days → weak rally
    closes = [100] + [90 + (i % 2) for i in range(20)] + [130]   # net up over window
    closes = [100, 130, 100, 131, 100, 132, 100, 133, 100, 134, 100, 135,
              100, 136, 100, 137, 100, 138, 100, 139, 100, 145]
    vols = [10 if closes[i] > closes[i - 1] else 1000 for i in range(len(closes))]
    out = db.volume_flow({"BTC": [{"c": c, "v": v} for c, v in zip(closes, vols)]})
    assert out["BTC"]["buy_pressure_20d"] < 0
    assert out["BTC"]["vs_price"] == "bearish_divergence"


def test_volume_flow_failsafe_on_garbage():
    assert db.volume_flow({"BTC": [{"x": 1}]}) == {}        # no usable bars → empty


# ── risk budget / concentration ──────────────────────────────────────────────

def test_risk_budget_flat_when_no_positions():
    out = db.risk_budget({"positions": {}}, {})
    assert out["directional_concentration"] == "flat"


def test_risk_budget_all_long_concentration_and_net():
    state = {"starting_equity": 1000.0, "positions": {
        "BTC": {"side": 1, "size_usd": 300}, "ETH": {"side": 1, "size_usd": 200}}}
    out = db.risk_budget(state, {})
    assert out["directional_concentration"] == "all_long"
    assert out["net_exposure_usd"] == 500.0 and out["gross_exposure_usd"] == 500.0
    assert out["net_exposure_pct_equity"] == 50.0


def test_risk_budget_mixed_nets_out():
    state = {"starting_equity": 1000.0, "positions": {
        "BTC": {"side": 1, "size_usd": 300}, "ETH": {"side": -1, "size_usd": 100}}}
    out = db.risk_budget(state, {})
    assert out["directional_concentration"] == "mixed"
    assert out["net_exposure_usd"] == 200.0 and out["gross_exposure_usd"] == 400.0


# ── composition + env toggles ─────────────────────────────────────────────────

def test_build_desk_blocks_respects_toggles(monkeypatch):
    monkeypatch.setattr(db, "BLOCK_CROSS_ASSET", False)     # skip the network block
    monkeypatch.setattr(db, "BLOCK_FLOW", True)
    monkeypatch.setattr(db, "BLOCK_RISK", True)
    ohlc = {"BTC": [{"c": 10 + i, "v": 100} for i in range(25)]}
    state = {"starting_equity": 1000.0, "positions": {"BTC": {"side": 1, "size_usd": 100}}}
    blocks = db.build_desk_blocks(ohlc, state, {})
    assert "cross_asset" not in blocks                      # toggled off
    assert "flow" in blocks and "risk_budget" in blocks      # the others compose in
    assert blocks["risk_budget"]["directional_concentration"] == "all_long"


def test_build_desk_blocks_drops_empty_blocks(monkeypatch):
    monkeypatch.setattr(db, "BLOCK_CROSS_ASSET", False)
    monkeypatch.setattr(db, "BLOCK_FLOW", True)
    monkeypatch.setattr(db, "BLOCK_RISK", False)
    # flow with no usable data → empty → dropped entirely
    assert db.build_desk_blocks({"BTC": [{"x": 1}]}, {}, {}) == {}

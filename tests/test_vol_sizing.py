"""Tests for realized-volatility (inverse-vol) sizing (src/vol_sizing.py)."""

import importlib

import src.vol_sizing as vs


def test_multiplier_neutral_at_target():
    # realized == target → 1.0
    assert vs.realized_vol_multiplier(0.004, target=0.004, floor=0.5, cap=1.5) == 1.0


def test_higher_vol_sizes_down_lower_vol_sizes_up():
    hi = vs.realized_vol_multiplier(0.008, target=0.004, floor=0.5, cap=1.5)  # 2x vol
    lo = vs.realized_vol_multiplier(0.002, target=0.004, floor=0.5, cap=1.5)  # half vol
    assert hi < 1.0 < lo


def test_bounds_are_enforced():
    # extreme vol clamps at floor; dead-calm clamps at cap
    assert vs.realized_vol_multiplier(1.0, target=0.004, floor=0.5, cap=1.5) == 0.5
    assert vs.realized_vol_multiplier(1e-6, target=0.004, floor=0.5, cap=1.5) == 1.5


def test_neutral_on_bad_data():
    assert vs.realized_vol_multiplier(None) == 1.0
    assert vs.realized_vol_multiplier(0.0) == 1.0
    assert vs.realized_vol_multiplier(-0.01) == 1.0


def test_apply_vol_target_scales_size(monkeypatch):
    monkeypatch.setattr(vs, "ENABLED", True)
    monkeypatch.setattr(vs, "TARGET_ATR_PCT", 0.004)
    monkeypatch.setattr(vs, "FLOOR", 0.5)
    monkeypatch.setattr(vs, "CAP", 1.5)
    # atr/price = 8/1000 = 0.8% = 2x target → size halved-ish (0.5 floor)
    out = vs.apply_vol_target(100.0, atr=8.0, price=1000.0)
    assert out == 50.0


def test_apply_vol_target_noop_when_disabled(monkeypatch):
    monkeypatch.setattr(vs, "ENABLED", False)
    assert vs.apply_vol_target(100.0, atr=8.0, price=1000.0) == 100.0


def test_apply_vol_target_failneutral_on_missing(monkeypatch):
    monkeypatch.setattr(vs, "ENABLED", True)
    assert vs.apply_vol_target(100.0, atr=None, price=1000.0) == 100.0
    assert vs.apply_vol_target(100.0, atr=8.0, price=0.0) == 100.0

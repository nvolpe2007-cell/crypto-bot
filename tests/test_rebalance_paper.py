"""Tests for rebalance_paper.py — the crypto+gold+cash monthly-rebalance forward arm."""
import importlib
import os

import pytest


@pytest.fixture()
def R():
    os.environ.pop("REBALANCE_SYMBOLS", None)
    os.environ.pop("REBALANCE_CRYPTO_FRAC", None)
    os.environ.pop("REBALANCE_GOLD_FRAC", None)
    import rebalance_paper
    return importlib.reload(rebalance_paper)


def _flat_px(R, price=100.0):
    px = {s: {"px": price, "t": 0} for s in R.SYMBOLS}
    px[R.GOLD] = {"px": price, "t": 0}
    return px


def test_target_weights_sum_to_crypto_plus_gold(R):
    w = R.target_weights()
    assert w[R.GOLD] == pytest.approx(R.GOLD_FRAC)
    assert sum(w[s] for s in R.SYMBOLS) == pytest.approx(R.CRYPTO_FRAC)
    # cash is the remainder, not a weight
    assert R.CASH_FRAC == pytest.approx(1 - R.CRYPTO_FRAC - R.GOLD_FRAC)


def test_seed_hits_target_dollar_split(R):
    px = _flat_px(R)
    seed = R._seed(px)
    eq = R._equity(seed["units"], seed["cash"], px)
    assert eq == pytest.approx(R.START_EQUITY)
    assert seed["cash"] == pytest.approx(R.START_EQUITY * R.CASH_FRAC)
    crypto_val = sum(seed["units"][s] * px[s]["px"] for s in R.SYMBOLS)
    assert crypto_val == pytest.approx(R.START_EQUITY * R.CRYPTO_FRAC)


def test_rebalance_trims_winners_back_to_target(R):
    px0 = _flat_px(R, 100.0)
    seed = R._seed(px0)
    # crypto doubles, gold flat -> book grows, weights drift above target
    px1 = {s: {"px": 200.0, "t": 1} for s in R.SYMBOLS}
    px1[R.GOLD] = {"px": 100.0, "t": 1}
    newbook, cost, eq_pre = R._rebalance(seed["units"], seed["cash"], px1)
    eq_post = R._equity(newbook["units"], newbook["cash"], px1)
    # cost is positive (turnover) and small; equity preserved minus cost
    assert cost > 0
    assert eq_post == pytest.approx(eq_pre - cost, rel=1e-6)
    # weights restored to target
    crypto_w = sum(newbook["units"][s] * px1[s]["px"] for s in R.SYMBOLS) / eq_post
    gold_w = newbook["units"][R.GOLD] * px1[R.GOLD]["px"] / eq_post
    cash_w = newbook["cash"] / eq_post
    assert crypto_w == pytest.approx(R.CRYPTO_FRAC, abs=1e-6)
    assert gold_w == pytest.approx(R.GOLD_FRAC, abs=1e-6)
    assert cash_w == pytest.approx(R.CASH_FRAC, abs=1e-6)


def test_no_turnover_when_already_on_target(R):
    px = _flat_px(R)
    seed = R._seed(px)
    # rebalancing an already-on-target book at the same prices costs ~nothing
    _, cost, _ = R._rebalance(seed["units"], seed["cash"], px)
    assert cost == pytest.approx(0.0, abs=1e-9)

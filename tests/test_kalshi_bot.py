"""Kalshi paper bot: fee math, settlement model, decision rule, no-lookahead, no order path."""

import ast
from math import sqrt
from pathlib import Path

import pytest

from kalshi_bot import fair_value as fv
from kalshi_bot.paper_sim import KalshiPaperState, open_position, settle
from kalshi_bot.runner import decide

ROOT = Path(__file__).resolve().parents[1]


def test_fee_rounds_up_to_cent():
    # 0.07 * 10 * 0.5 * 0.5 = 0.175 -> 0.18
    assert fv.taker_fee(10, 0.50) == pytest.approx(0.18)
    # exact cent stays put: 0.07 * 100 * 0.5 * 0.5 = 1.75
    assert fv.taker_fee(100, 0.50) == pytest.approx(1.75)
    assert fv.taker_fee(1, 0.99) == pytest.approx(0.01)


def test_edge_subtracts_ask_and_fee():
    c = 20
    assert fv.edge_per_contract(0.60, 0.50, c) == pytest.approx(0.60 - 0.50 - fv.taker_fee(c, 0.5) / c)


def test_effective_remaining_accounts_for_60s_average():
    assert fv.effective_remaining(600) == pytest.approx(560)
    assert fv.effective_remaining(60) == pytest.approx(20)
    assert fv.effective_remaining(0) == 0
    # continuous at the 60s boundary
    assert fv.effective_remaining(59.999) == pytest.approx(20, abs=1e-3)


def test_prob_up_symmetry_and_direction():
    s = 1e-4
    assert fv.prob_up(100.0, 100.0, 300, s) == pytest.approx(0.5)
    assert fv.prob_up(100.0, 100.5, 300, s) > 0.5 > fv.prob_up(100.0, 99.5, 300, s)


def test_decide_removes_basis_and_buys_cheap_underpriced_side():
    # Kraken reads $50 over BRTI; raw spot looks above strike, basis-adjusted it's flat.
    q = {"yes_ask": 0.30, "yes_ask_size": 1000, "no_ask": 0.72, "no_ask_size": 1000}
    row, trade = decide(strike=80000, proxy_pre_open=80050, spot=80050, sigma=5e-5, elapsed=420, q=q)
    assert row["spot_adj"] == pytest.approx(80000)
    assert row["q_up"] == pytest.approx(0.5)
    assert trade is not None and trade[0] == "YES" and trade[1] == 0.30


def test_decide_holds_when_no_edge_or_outside_band_or_thin_book():
    flat = dict(strike=80000, proxy_pre_open=80000, spot=80000, sigma=5e-5, elapsed=420)
    assert decide(**flat, q={"yes_ask": 0.51, "yes_ask_size": 1e6, "no_ask": 0.51, "no_ask_size": 1e6})[1] is None
    assert decide(**flat, q={"yes_ask": 0.05, "yes_ask_size": 1e6, "no_ask": 0.96, "no_ask_size": 1e6})[1] is None
    assert decide(**flat, q={"yes_ask": 0.30, "yes_ask_size": 1, "no_ask": 0.72, "no_ask_size": 1})[1] is None


def test_settle_pays_one_per_contract_and_charges_fee_either_way(tmp_path):
    st = KalshiPaperState()
    open_position(st, "T1", "YES", 0.40, 25.0, 0.42, "2026-10-28T01:00:00Z")
    open_position(st, "T2", "NO", 0.40, 25.0, 0.42, "2026-10-28T01:00:00Z")
    win = settle(st, "T1", result_yes=True)[0]
    lose = settle(st, "T2", result_yes=True)[0]
    assert win["pnl"] == pytest.approx(25 - 10 - 0.42)
    assert lose["pnl"] == pytest.approx(-10 - 0.42)
    assert st.equity == pytest.approx(1000 + 14.58 - 10.42)
    st.save(tmp_path / "s.json")
    assert KalshiPaperState.load(tmp_path / "s.json").equity == pytest.approx(st.equity)


def test_backtest_features_never_see_the_future():
    import scripts.kalshi_backtest as bt
    o = 1_800_000_000
    w = {"open": o, "strike": 100.0, "quotes": {str(o + 300): (0.6, 0.5)}}
    candles = {o - 60 * i: (100.0, 100.0 + (i % 2) * 0.01) for i in range(1, 70)}
    for i in range(0, 15):
        candles[o + 60 * i] = (100.0, 100.0 + i)       # bar i closes at o + 60(i+1)
    basis = (100.0 + 100.01) / 2 - 100.0               # pre-open minute proxy minus strike
    spot, *_ = bt.features(w, candles, 5)
    assert spot == pytest.approx(104.0 - basis)        # bar starting o+240, closed at o+300
    candles[o + 300] = (999.0, 999.0)                  # the bar that is still forming
    assert bt.features(w, candles, 5)[0] == pytest.approx(104.0 - basis)
    assert bt.features(w, candles, 5, btc_lag_bars=1)[0] == pytest.approx(103.0 - basis)


def test_package_has_no_order_or_auth_path():
    for f in (ROOT / "kalshi_bot").glob("*.py"):
        tree = ast.parse(f.read_text(encoding="utf-8"))
        src = f.read_text(encoding="utf-8").lower()
        assert ".post(" not in src and ".delete(" not in src and ".put(" not in src, f
        assert "/portfolio" not in src and "/orders" not in src, f
        assert "api_key" not in src and "private_key" not in src, f
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr in ("post", "put", "delete"):
                pytest.fail(f"{f.name} uses requests.{node.attr}")

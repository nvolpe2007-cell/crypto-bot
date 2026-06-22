"""Tests for the paper flash-loan arbitrage simulator (arbitrage/flash_arb_paper.py).

Pure-logic only — no network, no wallet, no money. Verifies the honest cost model
gates out spreads that don't clear flash-fee + 2 swaps + slippage + gas.
"""

from arbitrage import flash_arb_paper as F


def _q(ask_a, bid_a, ask_b, bid_b):
    """Two-venue quote book: venue 'a' and venue 'b'."""
    return {"a": {"ask": ask_a, "bid": bid_a}, "b": {"ask": ask_b, "bid": bid_b}}


def test_tiny_spread_is_unprofitable_after_costs():
    # 0.1% gross spread on $10k = $10 gross; costs (flash 0.09% + 2x0.3% swaps +
    # 2x0.1% slippage + $8 gas) ~ $90 → deeply negative.
    quotes = {"X/USD": _q(ask_a=100.0, bid_a=99.0, ask_b=100.1, bid_b=100.1)}
    opp = F.evaluate_arb("X/USD", quotes["X/USD"], notional=10_000, gas_usd=8.0)
    assert opp is not None
    assert opp.net_usd < 0
    assert not opp.profitable


def test_large_spread_clears_costs_and_fires():
    # 2% gross spread on $10k = $200 gross; costs ~ $90 → net positive.
    quotes = {"X/USD": _q(ask_a=100.0, bid_a=99.0, ask_b=102.0, bid_b=102.0)}
    opp = F.evaluate_arb("X/USD", quotes["X/USD"], notional=10_000, gas_usd=8.0)
    assert opp.profitable
    assert opp.net_usd > 0
    assert opp.buy_venue == "a" and opp.sell_venue == "b"


def test_costs_are_all_charged():
    opp = F.evaluate_arb("X/USD", _q(100.0, 99.0, 103.0, 103.0), notional=10_000,
                         flashloan_fee_frac=0.0009, swap_fee_frac=0.003,
                         slippage_frac=0.001, gas_usd=8.0)
    assert opp.flashloan_fee == 9.0          # 0.0009 * 10000
    assert opp.swap_fees == 60.0             # 2 * 0.003 * 10000
    assert opp.slippage == 20.0              # 2 * 0.001 * 10000
    assert opp.gas == 8.0
    # net = gross - (9+60+20+8)
    assert abs(opp.net_usd - (opp.gross_usd - 97.0)) < 1e-6


def test_needed_pct_is_total_cost_over_notional():
    opp = F.evaluate_arb("X/USD", _q(100.0, 100.0, 100.0, 101.0), notional=10_000, gas_usd=8.0)
    expected_needed = (opp.flashloan_fee + opp.swap_fees + opp.slippage + opp.gas) / 10_000
    assert abs(opp.needed_pct - expected_needed) < 1e-9


def test_buy_and_sell_pick_best_venues():
    # cheapest ask on 'b', highest bid on 'a' → buy b, sell a
    opp = F.evaluate_arb("X/USD", {"a": {"ask": 105, "bid": 104}, "b": {"ask": 100, "bid": 99}},
                         notional=10_000, gas_usd=8.0)
    assert opp.buy_venue == "b"
    assert opp.sell_venue == "a"


def test_single_venue_returns_none():
    assert F.evaluate_arb("X/USD", {"a": {"ask": 100, "bid": 99}}) is None


def test_same_venue_both_sides_returns_none():
    # one venue has both the lowest ask and highest bid → no arb
    quotes = {"a": {"ask": 100, "bid": 99.9}, "b": {"ask": 101, "bid": 98}}
    opp = F.evaluate_arb("X/USD", quotes, notional=10_000, gas_usd=8.0)
    # buy a (ask 100), sell a (bid 99.9) would be same venue → must reject or be unprofitable
    assert opp is None or opp.buy_venue != opp.sell_venue


def test_best_opportunity_picks_max_net():
    quotes = {
        "SMALL/USD": _q(100.0, 99.9, 100.2, 100.2),   # tiny spread
        "BIG/USD":   _q(100.0, 99.0, 103.0, 103.0),   # big spread
    }
    best = F.best_opportunity(quotes, notional=10_000, gas_usd=8.0)
    assert best is not None
    assert best.token == "BIG/USD"


def test_best_opportunity_none_when_all_single_venue():
    quotes = {"X/USD": {"a": {"ask": 100, "bid": 99}}}
    assert F.best_opportunity(quotes) is None


def test_cheaper_l2_gas_lowers_breakeven():
    eth = F.evaluate_arb("X/USD", _q(100.0, 100.0, 100.5, 100.5), notional=10_000, gas_usd=8.0)
    base = F.evaluate_arb("X/USD", _q(100.0, 100.0, 100.5, 100.5), notional=10_000, gas_usd=0.05)
    assert base.net_usd > eth.net_usd
    assert base.needed_pct < eth.needed_pct

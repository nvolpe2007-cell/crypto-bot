"""Tests for the maker-only microstructure forward arm.

These target the properties that make the arm's evidence trustworthy, not its
plumbing. If a future change quietly turns non-fills into fills, or lets a
take-profit sit below the round-trip cost, the arm would start producing
flattering numbers that look like an edge — so those are what is pinned here.
"""
from __future__ import annotations

import importlib
import json
import os

import pytest

from src.maker_fill import apply_trade, expire, post_maker


# ---------------------------------------------------------------- fill model
def test_resting_bid_does_not_fill_when_tape_runs_away():
    """The maker's real cost: price leaves, the order never fills, no trade."""
    o = post_maker("buy", 100.0, 50.0, ts=0.0, timeout_secs=30)
    # buyers lifting offers above us — nothing trades down into the bid
    for i in range(1, 10):
        apply_trade(o, 100.0 + i, "buy", trade_ts=float(i))
    assert not o.filled
    expire(o, now_ts=31.0)
    assert o.cancelled and not o.filled
    assert o.fee_usd == 0.0, "a cancelled order must not be charged a fee"


def test_resting_bid_fills_only_on_a_seller_trading_down_into_it():
    """Adverse selection is the model: we are filled as the tape pushes against us."""
    o = post_maker("buy", 100.0, 50.0, ts=0.0, timeout_secs=30)
    apply_trade(o, 99.9, "buy", trade_ts=1.0)     # a BUY below us must not fill a bid
    assert not o.filled
    apply_trade(o, 99.9, "sell", trade_ts=2.0)    # a SELL through us does
    assert o.filled and o.fill_price == 100.0


def test_fill_is_at_our_limit_never_worse():
    """A maker sets the price. Filling at the tape print would be a hidden slip."""
    o = post_maker("buy", 100.0, 50.0, ts=0.0)
    apply_trade(o, 97.0, "sell", trade_ts=1.0)
    assert o.fill_price == 100.0


def test_timeout_beats_a_late_through_trade():
    o = post_maker("buy", 100.0, 50.0, ts=0.0, timeout_secs=5)
    apply_trade(o, 99.0, "sell", trade_ts=6.0)
    assert o.cancelled and not o.filled


# ---------------------------------------------------------------- the arm
def _reload(monkeypatch, **env):
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    import micro_paper
    return importlib.reload(micro_paper)


def test_refuses_a_take_profit_below_the_round_trip_cost(monkeypatch, tmp_path):
    """0.25% maker each side means a 0.5% round trip. A smaller target is
    negative-EV by construction — the arm must refuse rather than log losses."""
    mp = _reload(monkeypatch,
                 MICRO_TAKE_PROFIT="0.001",
                 MICRO_STATE_FILE=str(tmp_path / "s.json"))
    with pytest.raises(SystemExit):
        mp.MicroMakerArm(["BTC/USD"])


def test_accepts_a_take_profit_above_the_round_trip_cost(monkeypatch, tmp_path):
    mp = _reload(monkeypatch,
                 MICRO_TAKE_PROFIT="0.006",
                 MICRO_STATE_FILE=str(tmp_path / "s.json"))
    arm = mp.MicroMakerArm(["BTC/USD"])
    assert arm.arms["BTC/USD"].symbol == "BTC/USD"


def test_closed_record_carries_what_the_proof_arm_reads(monkeypatch, tmp_path):
    """proof_scorecard._microstructure_forward() reads pnl / entry_ts / exit_ts
    and clusters by entry MINUTE. A record missing any of those is invisible to
    the bar, which would silently understate or drop the arm."""
    state = tmp_path / "s.json"
    mp = _reload(monkeypatch,
                 MICRO_TAKE_PROFIT="0.006",
                 MICRO_STATE_FILE=str(state))
    arm = mp.MicroMakerArm(["BTC/USD"])
    a = arm.arms["BTC/USD"]
    a.position = mp.Position(
        symbol="BTC/USD", entry_ts=1_700_000_000.0, entry_price=100.0,
        size_usd=50.0, entry_fee=0.125, obi_at_entry=0.61,
        cvd_slope_at_entry=0.4, fill_wait_secs=2.0,
    )
    a.last_price = 101.0
    arm._close(a, 101.0, 0.125, "maker_tp", now=1_700_000_060.0)

    rec = json.loads(state.read_text())["closed"][0]
    for key in ("pnl", "entry_ts", "exit_ts"):
        assert key in rec, f"proof arm requires {key!r}"
    assert isinstance(rec["pnl"], (int, float))
    # 50 * 1% = 0.50 gross, minus 0.25 of fees
    assert rec["pnl"] == pytest.approx(0.25, abs=1e-6)
    assert rec["exit_kind"] == "maker_tp"


def test_a_stopped_trade_pays_the_taker_fee(monkeypatch, tmp_path):
    """A stop crosses the spread. Charging it the maker fee would invent a
    strategy whose downside is cheaper than its upside."""
    state = tmp_path / "s.json"
    mp = _reload(monkeypatch,
                 MICRO_TAKE_PROFIT="0.006",
                 MICRO_STATE_FILE=str(state))
    arm = mp.MicroMakerArm(["BTC/USD"])
    a = arm.arms["BTC/USD"]
    a.position = mp.Position(
        symbol="BTC/USD", entry_ts=1_700_000_000.0, entry_price=100.0,
        size_usd=50.0, entry_fee=0.125, obi_at_entry=0.6,
        cvd_slope_at_entry=0.1, fill_wait_secs=1.0,
    )
    taker_fee = 50.0 * mp.TAKER_FEE_SPOT
    arm._close(a, 99.0, taker_fee, "taker_stop", now=1_700_000_030.0)
    rec = json.loads(state.read_text())["closed"][0]
    assert rec["fees"] > 0.125 * 2, "a taker exit must cost more than a maker one"
    assert rec["pnl"] < 0


def test_nonfills_are_counted_not_discarded(monkeypatch, tmp_path):
    """Non-fills ARE the maker's cost. An arm that only records its fills is
    measuring a different, flattering strategy."""
    state = tmp_path / "s.json"
    mp = _reload(monkeypatch,
                 MICRO_TAKE_PROFIT="0.006",
                 MICRO_STATE_FILE=str(state))
    arm = mp.MicroMakerArm(["BTC/USD"])
    a = arm.arms["BTC/USD"]
    a.pending = post_maker("buy", 100.0, 50.0, ts=0.0, timeout_secs=1)
    arm.step()                       # now >> post_ts, so it expires unfilled
    assert a.pending is None
    assert arm.state["nonfills"] == 1
    assert arm.state.get("closed", []) == [], "a non-fill is not a trade"


def test_state_write_is_atomic(monkeypatch, tmp_path):
    """A crash mid-write must not truncate the forward record — 90 days of
    evidence is not something to lose to a half-written file."""
    state = tmp_path / "s.json"
    mp = _reload(monkeypatch,
                 MICRO_TAKE_PROFIT="0.006",
                 MICRO_STATE_FILE=str(state))
    mp._save_state({"closed": [], "nonfills": 3})
    assert json.loads(state.read_text())["nonfills"] == 3
    assert not state.with_suffix(".tmp").exists(), "temp file must be renamed away"


def test_long_only(monkeypatch, tmp_path):
    """The proof arm marks this executable because maker-only LONGS on Kraken
    spot are what a US retail account can actually do. A short would make that
    claim false."""
    src = (importlib.import_module("micro_paper").__file__)
    text = open(src, encoding="utf-8").read()
    assert 'post_maker("sell"' in text, "exits rest on the ask"
    assert 'post_maker("short"' not in text
    assert '"side": "long"' in text

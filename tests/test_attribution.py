"""Tests for the cross-arm attribution ledger (src/attribution.py)."""

import os
from datetime import datetime, timezone, timedelta

import pytest

from src.attribution import (
    AttributionLedger,
    format_scorecard,
    _slippage_from_prices,
    ARM_DIRECTIONAL,
    ARM_FUNDING_AGGR,
    ARM_FUNDING_KRAKEN,
    ARM_TRIARB,
)


@pytest.fixture
def ledger(tmp_path):
    db = os.path.join(str(tmp_path), "attribution.db")
    led = AttributionLedger(db_path=db)
    yield led
    led.close()


# ── Slippage derivation ──────────────────────────────────────────────────────

def test_slippage_buy_higher_fill_is_cost():
    # Bought 100 USD of an asset at intended 100, filled 101 → paid 1% more.
    cost = _slippage_from_prices("buy", 100.0, 101.0, 100.0)
    assert cost == pytest.approx(1.0)  # 1 unit * $1


def test_slippage_sell_lower_fill_is_cost():
    cost = _slippage_from_prices("sell", 100.0, 99.0, 100.0)
    assert cost == pytest.approx(1.0)


def test_slippage_buy_better_fill_is_negative_cost():
    # Filled cheaper than intended → slippage helped us (negative cost).
    cost = _slippage_from_prices("buy", 100.0, 99.0, 100.0)
    assert cost == pytest.approx(-1.0)


def test_slippage_neutral_side_is_zero():
    assert _slippage_from_prices("cycle", 100.0, 101.0, 100.0) == 0.0
    assert _slippage_from_prices(None, 100.0, 101.0, 100.0) == 0.0


def test_slippage_missing_data_is_zero():
    assert _slippage_from_prices("buy", None, 101.0, 100.0) == 0.0
    assert _slippage_from_prices("buy", 0.0, 101.0, 100.0) == 0.0


# ── record + aggregation ─────────────────────────────────────────────────────

def test_record_and_summary_by_arm(ledger):
    ledger.record(ARM_FUNDING_AGGR, "BTC/USD", side="long",
                  fees_paid=0.30, gross_pnl=0.50, reason="rollup")
    ledger.record(ARM_FUNDING_AGGR, "ETH/USD", side="long",
                  fees_paid=0.20, gross_pnl=-0.10, reason="rollup")
    ledger.record(ARM_DIRECTIONAL, "SOL/USD", side="buy",
                  fees_paid=0.05, net_pnl=-0.08, reason="STOP_LOSS")

    summ = ledger.summary_by_arm()
    assert set(summ) == {ARM_FUNDING_AGGR, ARM_DIRECTIONAL}

    aggr = summ[ARM_FUNDING_AGGR]
    assert aggr["n"] == 2
    assert aggr["gross"] == pytest.approx(0.40)
    assert aggr["fees"] == pytest.approx(0.50)
    # net = gross - fees - slippage = 0.40 - 0.50 - 0 = -0.10
    assert aggr["net"] == pytest.approx(-0.10)
    assert aggr["wins"] == 1            # only the +0.50 gross row had net>0
    assert aggr["win_rate"] == pytest.approx(50.0)


def test_net_derived_from_gross(ledger):
    ledger.record(ARM_FUNDING_AGGR, "BTC/USD", side="long",
                  fees_paid=0.10, slippage_cost=0.05, gross_pnl=1.00)
    row = ledger.summary_by_arm()[ARM_FUNDING_AGGR]
    assert row["net"] == pytest.approx(0.85)  # 1.00 - 0.10 - 0.05


def test_gross_derived_from_net(ledger):
    ledger.record(ARM_DIRECTIONAL, "BTC/USD", side="buy",
                  fees_paid=0.10, slippage_cost=0.05, net_pnl=0.85)
    row = ledger.summary_by_arm()[ARM_DIRECTIONAL]
    assert row["gross"] == pytest.approx(1.00)


def test_slippage_auto_derived_from_prices(ledger):
    # No explicit slippage_cost — derived from intended vs fill on a buy.
    ledger.record(ARM_DIRECTIONAL, "BTC/USD", side="buy",
                  intended_price=100.0, fill_price=101.0, size_usd=100.0,
                  fees_paid=0.0, gross_pnl=0.0)
    row = ledger.summary_by_arm()[ARM_DIRECTIONAL]
    assert row["slippage"] == pytest.approx(1.0)
    assert row["net"] == pytest.approx(-1.0)  # gross 0 - fees 0 - slip 1


def test_totals_rolls_up_all_arms(ledger):
    ledger.record(ARM_FUNDING_AGGR, "BTC/USD", net_pnl=0.50, fees_paid=0.10)
    ledger.record(ARM_FUNDING_KRAKEN, "ETH/USD", net_pnl=-0.30, fees_paid=0.20)
    tot = ledger.totals()
    assert tot["n"] == 2
    assert tot["net"] == pytest.approx(0.20)
    assert tot["fees"] == pytest.approx(0.30)


# ── daily scorecard windowing ────────────────────────────────────────────────

def test_daily_scorecard_filters_by_day(ledger):
    today = datetime.now(timezone.utc)
    yesterday = today - timedelta(days=1)
    ledger.record(ARM_FUNDING_AGGR, "BTC/USD", net_pnl=1.0, fees_paid=0.1,
                  ts=today.isoformat())
    ledger.record(ARM_FUNDING_AGGR, "BTC/USD", net_pnl=9.0, fees_paid=0.1,
                  ts=yesterday.isoformat())

    sc = ledger.daily_scorecard(today.date())
    assert sc["total"]["n"] == 1
    assert sc["total"]["net"] == pytest.approx(1.0)
    assert sc["day"] == today.date().isoformat()


# ── formatting ───────────────────────────────────────────────────────────────

def test_format_scorecard_contains_arms_and_total(ledger):
    ledger.record(ARM_FUNDING_AGGR, "BTC/USD", net_pnl=0.50, fees_paid=0.10)
    ledger.record(ARM_TRIARB, "BTC/USD", side="cycle", net_pnl=0.0, fees_paid=0.0)
    msg = format_scorecard(ledger.daily_scorecard())
    assert "Per-arm attribution" in msg
    assert ARM_FUNDING_AGGR in msg
    assert "TOTAL" in msg
    assert "<pre>" in msg  # monospace block for alignment


def test_format_scorecard_empty_is_safe():
    msg = format_scorecard({"day": "2026-06-12", "by_arm": {}, "total": {}})
    assert "Per-arm attribution" in msg


# ── robustness: a broken ledger never raises into the trading path ───────────

def test_disabled_ledger_record_returns_false():
    led = AttributionLedger(db_path="/nonexistent_dir_xyz/\0bad/attribution.db")
    # Bad path → init fails → disabled, but record must not raise.
    assert led.record(ARM_DIRECTIONAL, "BTC/USD", net_pnl=1.0) is False
    assert led.summary_by_arm() == {}


def test_record_with_garbage_does_not_raise(ledger):
    # Non-numeric prices must be coerced/ignored, never crash.
    assert ledger.record(ARM_DIRECTIONAL, "BTC/USD", side="buy",
                         intended_price="oops", fill_price=None,
                         net_pnl=0.0) is True

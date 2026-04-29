"""Unit tests for src/paper_trading.py — trade execution, PnL, SL/TP."""

import pytest
from datetime import datetime, timezone

from src.paper_trading import PaperTrader


def _now():
    return datetime.now(timezone.utc)


def _make_trader(**kwargs):
    """Return a PaperTrader with sensible defaults that can be overridden."""
    defaults = dict(
        initial_capital=1_000.0,
        position_size=200.0,
        fee_pct=0.0,
        slippage_pct=0.0,
        stop_loss_pct=2.0,
        take_profit_pct=3.0,
    )
    defaults.update(kwargs)
    return PaperTrader(**defaults)


# ── execute_buy ───────────────────────────────────────────────────────────────

class TestExecuteBuy:
    def test_position_created(self):
        trader = _make_trader()
        pos = trader.execute_buy('BTC/USD', 1_000.0, _now())
        assert pos is not None
        assert 'BTC/USD' in trader.account.positions

    def test_cash_reduced(self):
        trader = _make_trader(position_size=200.0)
        trader.execute_buy('BTC/USD', 1_000.0, _now())
        assert trader.account.cash < 1_000.0

    def test_position_size_capped_by_cash(self):
        """position_size > cash → use all remaining cash."""
        trader = _make_trader(initial_capital=100.0, position_size=500.0)
        pos = trader.execute_buy('BTC/USD', 1_000.0, _now())
        assert pos is not None
        # size should be at most cash/price
        assert pos.size <= 100.0 / 1_000.0 + 1e-9

    def test_position_size_capped_by_max(self):
        """position_size < cash → size capped at position_size/price."""
        trader = _make_trader(initial_capital=1_000.0, position_size=100.0)
        pos = trader.execute_buy('BTC/USD', 1_000.0, _now())
        assert abs(pos.size - 100.0 / 1_000.0) < 1e-9

    def test_returns_none_when_no_cash(self):
        trader = _make_trader(initial_capital=0.0, position_size=200.0)
        pos = trader.execute_buy('BTC/USD', 1_000.0, _now())
        assert pos is None

    def test_slippage_increases_entry_price(self):
        trader = _make_trader(slippage_pct=0.5)
        pos = trader.execute_buy('BTC/USD', 1_000.0, _now())
        assert pos.entry_price == pytest.approx(1_005.0)

    def test_fee_stored_on_position(self):
        trader = _make_trader(fee_pct=0.26, slippage_pct=0.0)
        pos = trader.execute_buy('BTC/USD', 1_000.0, _now())
        assert pos.entry_fee > 0.0

    def test_cannot_open_duplicate_position(self):
        """Buying twice on the same symbol replaces the first position (current design)."""
        trader = _make_trader(initial_capital=2_000.0, position_size=200.0)
        trader.execute_buy('BTC/USD', 1_000.0, _now())
        cash_after_first = trader.account.cash
        trader.execute_buy('BTC/USD', 1_000.0, _now())
        # Each buy reduces cash — the second buy should also reduce it
        assert trader.account.cash < cash_after_first


# ── execute_sell ─────────────────────────────────────────────────────────────

class TestExecuteSell:
    def test_closes_position(self):
        trader = _make_trader()
        trader.execute_buy('BTC/USD', 1_000.0, _now())
        trader.execute_sell('BTC/USD', 1_000.0, _now())
        assert 'BTC/USD' not in trader.account.positions

    def test_returns_none_with_no_position(self):
        trader = _make_trader()
        result = trader.execute_sell('BTC/USD', 1_000.0, _now())
        assert result is None

    def test_trade_recorded(self):
        trader = _make_trader()
        trader.execute_buy('BTC/USD', 1_000.0, _now())
        trader.execute_sell('BTC/USD', 1_050.0, _now())
        assert len(trader.account.closed_trades) == 1

    def test_winning_trade_positive_pnl(self):
        trader = _make_trader()
        trader.execute_buy('BTC/USD', 1_000.0, _now())
        trade = trader.execute_sell('BTC/USD', 1_100.0, _now())
        assert trade.pnl > 0

    def test_losing_trade_negative_pnl(self):
        trader = _make_trader()
        trader.execute_buy('BTC/USD', 1_000.0, _now())
        trade = trader.execute_sell('BTC/USD', 900.0, _now())
        assert trade.pnl < 0

    def test_slippage_reduces_exit_price(self):
        trader = _make_trader(slippage_pct=0.5)
        trader.execute_buy('BTC/USD', 1_000.0, _now())
        trade = trader.execute_sell('BTC/USD', 1_000.0, _now())
        # Exit price should be lower than market price due to slippage
        assert trade.exit_price == pytest.approx(995.0)


# ── PnL accuracy (the bug regression test) ───────────────────────────────────

class TestPnLAccuracy:
    def test_round_trip_same_price_pnl_equals_cash_change(self):
        """
        After buying and selling at the same price, total_pnl must equal
        the actual change in cash (both buy and sell fees deducted).
        Previously, the buy-side fee was missing from total_pnl.
        """
        trader = _make_trader(
            initial_capital=1_000.0,
            position_size=200.0,
            fee_pct=0.26,
            slippage_pct=0.0,
        )
        initial_cash = trader.account.cash

        trader.execute_buy('BTC/USD', 1_000.0, _now())
        trader.execute_sell('BTC/USD', 1_000.0, _now())

        cash_change = trader.account.cash - initial_cash
        assert abs(trader.account.total_pnl - cash_change) < 1e-9, (
            f"total_pnl ({trader.account.total_pnl:.6f}) must match "
            f"actual cash change ({cash_change:.6f}) — buy-side fee must be included"
        )

    def test_round_trip_same_price_has_negative_pnl(self):
        """A round trip at the same price should always lose money (fees)."""
        trader = _make_trader(fee_pct=0.26, slippage_pct=0.0)
        trader.execute_buy('ETH/USD', 2_000.0, _now())
        trader.execute_sell('ETH/USD', 2_000.0, _now())
        assert trader.account.total_pnl < 0.0

    def test_fees_field_includes_both_sides(self):
        """Trade.fees should reflect the total cost of entry + exit."""
        trader = _make_trader(
            initial_capital=1_000.0,
            position_size=200.0,
            fee_pct=0.26,
            slippage_pct=0.0,
        )
        trader.execute_buy('SOL/USD', 100.0, _now())
        trade = trader.execute_sell('SOL/USD', 100.0, _now())

        size = trade.size
        expected_fee_each_side = 100.0 * size * (0.26 / 100)
        expected_total_fees = expected_fee_each_side * 2
        assert abs(trade.fees - expected_total_fees) < 1e-9

    def test_winning_pnl_matches_cash_gain(self):
        trader = _make_trader(
            initial_capital=10_000.0,
            position_size=1_000.0,
            fee_pct=0.0,
            slippage_pct=0.0,
        )
        initial_cash = trader.account.cash
        trader.execute_buy('BTC/USD', 100.0, _now())
        trader.execute_sell('BTC/USD', 110.0, _now())

        cash_change = trader.account.cash - initial_cash
        assert abs(trader.account.total_pnl - cash_change) < 1e-9


# ── check_stop_loss_take_profit ───────────────────────────────────────────────

class TestStopLossTakeProfit:
    def _open(self, trader, price=1_000.0):
        trader.execute_buy('BTC/USD', price, _now())

    def test_stop_loss_triggers(self):
        trader = _make_trader(stop_loss_pct=2.0, take_profit_pct=5.0)
        self._open(trader)
        # Drop 3 % → below the 2 % stop
        trade = trader.check_stop_loss_take_profit('BTC/USD', 970.0, _now())
        assert trade is not None
        assert 'BTC/USD' not in trader.account.positions

    def test_stop_loss_not_triggered_within_tolerance(self):
        trader = _make_trader(stop_loss_pct=2.0, take_profit_pct=5.0)
        self._open(trader)
        # Drop only 1 % → still within the 2 % stop buffer
        trade = trader.check_stop_loss_take_profit('BTC/USD', 990.0, _now())
        assert trade is None

    def test_take_profit_triggers(self):
        trader = _make_trader(stop_loss_pct=2.0, take_profit_pct=3.0)
        self._open(trader)
        # Rise 4 % → above the 3 % TP
        trade = trader.check_stop_loss_take_profit('BTC/USD', 1_040.0, _now())
        assert trade is not None
        assert 'BTC/USD' not in trader.account.positions

    def test_take_profit_not_triggered_below_threshold(self):
        trader = _make_trader(stop_loss_pct=2.0, take_profit_pct=3.0)
        self._open(trader)
        # Rise only 2 % → below the 3 % TP
        trade = trader.check_stop_loss_take_profit('BTC/USD', 1_020.0, _now())
        assert trade is None

    def test_no_position_returns_none(self):
        trader = _make_trader()
        trade = trader.check_stop_loss_take_profit('BTC/USD', 1_000.0, _now())
        assert trade is None


# ── get_account_summary ───────────────────────────────────────────────────────

class TestAccountSummary:
    def test_initial_state(self):
        trader = _make_trader(initial_capital=500.0)
        s = trader.get_account_summary()
        assert s['cash'] == pytest.approx(500.0)
        assert s['total_equity'] == pytest.approx(500.0)
        assert s['total_pnl'] == pytest.approx(0.0)
        assert s['open_positions'] == 0
        assert s['closed_trades'] == 0
        assert s['winning_trades'] == 0
        assert s['losing_trades'] == 0

    def test_win_loss_counters(self):
        trader = _make_trader(
            initial_capital=10_000.0,
            position_size=500.0,
            fee_pct=0.0,
            slippage_pct=0.0,
        )
        t = _now()
        # Winning trade
        trader.execute_buy('BTC/USD', 100.0, t)
        trader.execute_sell('BTC/USD', 110.0, t)
        # Losing trade
        trader.execute_buy('BTC/USD', 100.0, t)
        trader.execute_sell('BTC/USD', 90.0, t)

        s = trader.get_account_summary()
        assert s['winning_trades'] == 1
        assert s['losing_trades'] == 1
        assert s['closed_trades'] == 2

    def test_equity_includes_unrealized(self):
        trader = _make_trader(
            initial_capital=10_000.0,
            position_size=1_000.0,
            fee_pct=0.0,
            slippage_pct=0.0,
        )
        trader.execute_buy('BTC/USD', 100.0, _now())
        trader.update_unrealized_pnl({'BTC/USD': 110.0})

        s = trader.get_account_summary()
        # Cash was reduced by 1000, equity should include the 10 * size unrealized gain
        assert s['total_equity'] > s['cash']

    def test_pnl_pct_matches_total_pnl(self):
        trader = _make_trader(
            initial_capital=1_000.0,
            position_size=200.0,
            fee_pct=0.0,
            slippage_pct=0.0,
        )
        trader.execute_buy('ETH/USD', 100.0, _now())
        trader.execute_sell('ETH/USD', 120.0, _now())

        s = trader.get_account_summary()
        expected_pct = s['total_pnl'] / 1_000.0 * 100
        assert abs(s['pnl_pct'] - expected_pct) < 1e-6

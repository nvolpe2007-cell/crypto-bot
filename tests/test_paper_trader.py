"""
Tests for src.paper_trader — the spot + perp paper execution engine.

No mocks needed: PaperTrader does not call TelegramNotifier or
ExchangeConnection during the methods tested here; both are optional
constructor args we simply omit.  The ccxt / pandas_ta stubs in conftest.py
handle the transitive import chain.
"""
from __future__ import annotations

import pytest
from datetime import datetime, timezone, timedelta

from src.paper_trader import PaperTrader, _PERP_MAINT_MARGIN

T0 = datetime(2024, 1, 1, tzinfo=timezone.utc)
T1 = T0 + timedelta(hours=1)
T9 = T0 + timedelta(hours=9)    # 9 h → exactly 1 funding cycle  (9 // 8 = 1)
T25 = T0 + timedelta(hours=25)  # 25 h → 3 funding cycles         (25 // 8 = 3)


# ── helpers ───────────────────────────────────────────────────────────────────

def spot(**kw) -> PaperTrader:
    """Spot-mode trader with zero fees and zero slippage by default."""
    defaults = dict(initial_capital=1000.0, fee_pct=0.0, slippage_pct=0.0)
    defaults.update(kw)
    return PaperTrader(**defaults)


def perp(**kw) -> PaperTrader:
    """Perp-mode trader with zero fees, zero slippage, 2× leverage by default."""
    defaults = dict(initial_capital=1000.0, fee_pct=0.0, slippage_pct=0.0,
                    perp_mode=True, leverage=2.0)
    defaults.update(kw)
    return PaperTrader(**defaults)


# ── slippage ──────────────────────────────────────────────────────────────────

class TestSlippage:
    def test_returns_floor_when_no_spread(self):
        pt = spot(slippage_pct=0.1)
        assert pt._slippage_pct_for("BTC/USD", 100.0) == pytest.approx(0.001)

    def test_half_spread_when_wider_than_floor(self):
        # spread=$0.60 on $100 → 0.6%; half=0.3%=0.003 > floor(0%), below 0.5% cap
        pt = spot(slippage_pct=0.0)
        pt.live_spreads["BTC/USD"] = 0.60
        assert pt._slippage_pct_for("BTC/USD", 100.0) == pytest.approx(0.003)

    def test_floor_wins_when_half_spread_smaller(self):
        # half-spread = 0.05% < floor = 0.2%
        pt = spot(slippage_pct=0.2)
        pt.live_spreads["BTC/USD"] = 0.1   # $0.10 on $100 → 0.1%; half = 0.05%
        assert pt._slippage_pct_for("BTC/USD", 100.0) == pytest.approx(0.002)

    def test_caps_at_0_5_pct(self):
        # enormous spread → capped at 0.5%
        pt = spot(slippage_pct=0.0)
        pt.live_spreads["BTC/USD"] = 200.0
        assert pt._slippage_pct_for("BTC/USD", 100.0) == pytest.approx(0.005)

    def test_unknown_symbol_falls_back_to_floor(self):
        pt = spot(slippage_pct=0.15)
        assert pt._slippage_pct_for("UNKNOWN/USD", 100.0) == pytest.approx(0.0015)


# ── spot buy ──────────────────────────────────────────────────────────────────

class TestSpotBuy:
    def test_creates_position(self):
        pt = spot()
        pos = pt.execute_buy("BTC/USD", 100.0, T0, 100.0)
        assert pos is not None
        assert pos.side == 'buy'
        assert "BTC/USD" in pt.account.positions

    def test_cash_deducted_by_notional(self):
        # No fee, no slip: 100 USD deducted from 1000 account
        pt = spot()
        pt.execute_buy("BTC/USD", 100.0, T0, 100.0)
        assert pt.account.cash == pytest.approx(900.0)

    def test_fee_deducted_on_buy(self):
        # fee_pct=1%: exec_price=100, size=1, fee=1.0, total_cost=101
        pt = spot(fee_pct=1.0)
        pt.execute_buy("BTC/USD", 100.0, T0, 100.0)
        assert pt.account.cash == pytest.approx(899.0)

    def test_slippage_raises_exec_price(self):
        pt = spot(slippage_pct=1.0)   # 1% slip
        pos = pt.execute_buy("BTC/USD", 100.0, T0, 100.0)
        assert pos.entry_price == pytest.approx(101.0)

    def test_spot_position_has_zero_liq_price(self):
        pt = spot()
        pos = pt.execute_buy("BTC/USD", 100.0, T0, 100.0)
        assert not pos.is_perp
        assert pos.liquidation_price == pytest.approx(0.0)

    def test_scale_down_when_size_exceeds_cash(self):
        # Only $10 available but requesting $100 buy
        pt = spot(initial_capital=10.0)
        pos = pt.execute_buy("BTC/USD", 100.0, T0, 100.0)
        assert pos is not None
        assert pt.account.cash >= 0.0

    def test_returns_none_when_account_empty(self):
        pt = spot(initial_capital=0.0)
        assert pt.execute_buy("BTC/USD", 100.0, T0, 100.0) is None


# ── spot sell ─────────────────────────────────────────────────────────────────

class TestSpotSell:
    def test_returns_none_when_no_position(self):
        pt = spot()
        assert pt.execute_sell("BTC/USD", 100.0, T0) is None

    def test_closes_position(self):
        pt = spot()
        pt.execute_buy("BTC/USD", 100.0, T0, 100.0)
        pt.execute_sell("BTC/USD", 100.0, T1)
        assert "BTC/USD" not in pt.account.positions

    def test_profit_pnl(self):
        # Buy 1 BTC at 100, sell at 110 → +10
        pt = spot()
        pt.execute_buy("BTC/USD", 100.0, T0, 100.0)
        trade = pt.execute_sell("BTC/USD", 110.0, T1)
        assert trade.pnl == pytest.approx(10.0)

    def test_loss_pnl(self):
        pt = spot()
        pt.execute_buy("BTC/USD", 100.0, T0, 100.0)
        trade = pt.execute_sell("BTC/USD", 90.0, T1)
        assert trade.pnl == pytest.approx(-10.0)

    def test_fees_in_trade_record(self):
        # 1% fee each way: entry_fee=1, exit_fee=1, total=2
        pt = spot(fee_pct=1.0)
        pt.execute_buy("BTC/USD", 100.0, T0, 100.0)
        trade = pt.execute_sell("BTC/USD", 100.0, T1)
        assert trade.fees == pytest.approx(2.0)
        assert trade.pnl == pytest.approx(-2.0)

    def test_cash_returns_to_initial_after_breakeven(self):
        pt = spot()
        pt.execute_buy("BTC/USD", 100.0, T0, 100.0)
        pt.execute_sell("BTC/USD", 100.0, T1)
        assert pt.account.cash == pytest.approx(1000.0)

    def test_total_pnl_accumulates_across_trades(self):
        pt = spot()
        pt.execute_buy("BTC/USD", 100.0, T0, 100.0)
        pt.execute_sell("BTC/USD", 110.0, T1)              # +10
        pt.execute_buy("ETH/USD", 50.0, T1, 50.0)
        pt.execute_sell("ETH/USD", 55.0, T0 + timedelta(hours=2))   # +5
        assert pt.account.total_pnl == pytest.approx(15.0)

    def test_trade_record_added_to_closed_trades(self):
        pt = spot()
        pt.execute_buy("BTC/USD", 100.0, T0, 100.0)
        pt.execute_sell("BTC/USD", 110.0, T1)
        assert len(pt.account.closed_trades) == 1
        assert pt.account.closed_trades[0].side == 'sell'


# ── spot short / cover ────────────────────────────────────────────────────────

class TestSpotShortCover:
    def test_short_blocked_when_not_allowed(self):
        pt = spot(allow_spot_shorts=False)
        assert pt.execute_short("BTC/USD", 100.0, T0, 100.0) is None

    def test_short_allowed_by_default(self):
        pt = spot()
        pos = pt.execute_short("BTC/USD", 100.0, T0, 100.0)
        assert pos is not None
        assert pos.side == 'short'

    def test_cover_returns_none_when_no_position(self):
        pt = spot()
        assert pt.execute_cover("BTC/USD", 100.0, T0) is None

    def test_cover_delegates_to_sell_for_long(self):
        # execute_cover on a LONG redirects to execute_sell
        pt = spot()
        pt.execute_buy("BTC/USD", 100.0, T0, 100.0)
        trade = pt.execute_cover("BTC/USD", 110.0, T1)
        assert trade is not None
        assert trade.pnl == pytest.approx(10.0)

    def test_cover_short_profit(self):
        # Short at 100, cover at 90 → +10
        pt = spot()
        pt.execute_short("BTC/USD", 100.0, T0, 100.0)
        trade = pt.execute_cover("BTC/USD", 90.0, T1)
        assert trade.pnl == pytest.approx(10.0)

    def test_cover_short_loss(self):
        # Short at 100, cover at 110 → -10
        pt = spot()
        pt.execute_short("BTC/USD", 100.0, T0, 100.0)
        trade = pt.execute_cover("BTC/USD", 110.0, T1)
        assert trade.pnl == pytest.approx(-10.0)

    def test_cash_restored_after_profitable_short(self):
        pt = spot()
        pt.execute_short("BTC/USD", 100.0, T0, 100.0)
        pt.execute_cover("BTC/USD", 90.0, T1)
        assert pt.account.cash == pytest.approx(1010.0)


# ── partial exits ─────────────────────────────────────────────────────────────

class TestPartialExit:
    def test_partial_sell_reduces_position_size(self):
        pt = spot()
        pt.execute_buy("BTC/USD", 100.0, T0, 100.0)   # size=1
        pt.execute_partial_sell("BTC/USD", 100.0, T1, fraction=0.5)
        assert pt.account.positions["BTC/USD"].size == pytest.approx(0.5)

    def test_partial_sell_breakeven_pnl(self):
        pt = spot()
        pt.execute_buy("BTC/USD", 100.0, T0, 100.0)
        pnl = pt.execute_partial_sell("BTC/USD", 100.0, T1, fraction=0.5)
        assert pnl == pytest.approx(0.0)

    def test_partial_sell_profit_pnl(self):
        # Sell half at 110: (110-100)*0.5 = 5
        pt = spot()
        pt.execute_buy("BTC/USD", 100.0, T0, 100.0)
        pnl = pt.execute_partial_sell("BTC/USD", 110.0, T1, fraction=0.5)
        assert pnl == pytest.approx(5.0)

    def test_partial_sell_returns_none_when_no_position(self):
        pt = spot()
        assert pt.execute_partial_sell("BTC/USD", 100.0, T0) is None

    def test_partial_sell_returns_none_for_short_position(self):
        pt = spot()
        pt.execute_short("BTC/USD", 100.0, T0, 100.0)
        assert pt.execute_partial_sell("BTC/USD", 100.0, T1) is None

    def test_partial_cover_reduces_short_size(self):
        pt = spot()
        pt.execute_short("BTC/USD", 100.0, T0, 100.0)
        pt.execute_partial_cover("BTC/USD", 100.0, T1, fraction=0.5)
        assert pt.account.positions["BTC/USD"].size == pytest.approx(0.5)

    def test_partial_cover_profit_pnl(self):
        # Cover half at 90: (100-90)*0.5 = 5
        pt = spot()
        pt.execute_short("BTC/USD", 100.0, T0, 100.0)
        pnl = pt.execute_partial_cover("BTC/USD", 90.0, T1, fraction=0.5)
        assert pnl == pytest.approx(5.0)

    def test_partial_cover_returns_none_when_no_position(self):
        pt = spot()
        assert pt.execute_partial_cover("BTC/USD", 100.0, T0) is None

    def test_partial_cover_returns_none_for_long_position(self):
        pt = spot()
        pt.execute_buy("BTC/USD", 100.0, T0, 100.0)
        assert pt.execute_partial_cover("BTC/USD", 100.0, T1) is None


# ── perp buy / sell ───────────────────────────────────────────────────────────

class TestPerpBuy:
    def test_only_margin_deducted_from_cash(self):
        # notional=100, leverage=2 → margin=50
        pt = perp(leverage=2.0)
        pt.execute_buy("BTC/USD", 1000.0, T0, 100.0)
        assert pt.account.cash == pytest.approx(950.0)

    def test_long_liquidation_price_formula(self):
        # liq = exec_price × (1 − (1−MAINT)/leverage)
        #      = 1000 × (1 − 0.98/2) = 1000 × 0.51 = 510
        pt = perp(leverage=2.0)
        pos = pt.execute_buy("BTC/USD", 1000.0, T0, 100.0)
        expected = 1000.0 * (1.0 - (1.0 - _PERP_MAINT_MARGIN) / 2.0)
        assert pos.liquidation_price == pytest.approx(expected)

    def test_position_marked_as_perp(self):
        pt = perp()
        pos = pt.execute_buy("BTC/USD", 1000.0, T0, 100.0)
        assert pos.is_perp
        assert pos.leverage == pytest.approx(2.0)
        assert pos.margin_locked == pytest.approx(50.0)


class TestPerpSell:
    def test_margin_and_profit_returned_to_cash(self):
        # margin=50, cash=950; close at 1100 → profit=10; cash=1010
        pt = perp(leverage=2.0)
        pt.execute_buy("BTC/USD", 1000.0, T0, 100.0)
        pt.execute_sell("BTC/USD", 1100.0, T1)
        # pnl = (1100-1000)*0.1 = 10 (size = 100/1000 = 0.1)
        assert pt.account.cash == pytest.approx(1010.0)

    def test_loss_reduces_cash_on_close(self):
        pt = perp(leverage=2.0)
        pt.execute_buy("BTC/USD", 1000.0, T0, 100.0)
        pt.execute_sell("BTC/USD", 900.0, T1)
        # pnl = (900-1000)*0.1 = -10; cash = 950+50-10=990
        assert pt.account.cash == pytest.approx(990.0)


class TestPerpShort:
    def test_short_liquidation_price_formula(self):
        # liq = exec_price × (1 + (1−MAINT)/leverage)
        #      = 1000 × (1 + 0.98/2) = 1000 × 1.49 = 1490
        pt = perp(leverage=2.0)
        pos = pt.execute_short("BTC/USD", 1000.0, T0, 100.0)
        expected = 1000.0 * (1.0 + (1.0 - _PERP_MAINT_MARGIN) / 2.0)
        assert pos.liquidation_price == pytest.approx(expected)

    def test_short_cover_profit(self):
        pt = perp(leverage=2.0)
        pt.execute_short("BTC/USD", 1000.0, T0, 100.0)
        pt.execute_cover("BTC/USD", 900.0, T1)
        # pnl = (1000-900)*0.1 = 10; cash = 950+50+10=1010
        assert pt.account.cash == pytest.approx(1010.0)

    def test_short_blocked_in_spot_mode_when_not_allowed(self):
        pt = spot(allow_spot_shorts=False)
        assert pt.execute_short("BTC/USD", 1000.0, T0, 100.0) is None


# ── funding accrual ───────────────────────────────────────────────────────────

class TestFundingAccrual:
    def test_noop_in_spot_mode(self):
        pt = spot()
        pt.execute_buy("BTC/USD", 1000.0, T0, 100.0)
        pt.accrue_funding(T9)   # should not raise or mutate
        assert pt.account.positions["BTC/USD"].funding_accrued == pytest.approx(0.0)

    def test_no_accrual_within_8h(self):
        pt = perp()
        pt.execute_buy("BTC/USD", 1000.0, T0, 100.0)
        pt.set_funding_rate("BTC/USD", 0.001)
        pt.accrue_funding(T0 + timedelta(hours=7))   # < 8h → 0 cycles
        assert pt.account.positions["BTC/USD"].funding_accrued == pytest.approx(0.0)

    def test_long_pays_positive_funding(self):
        # notional = 1000 * 0.1 = 100; rate = 0.001; 1 cycle
        # delta = -1 * 0.001 * 100 * 1 = -0.1
        pt = perp()
        pt.execute_buy("BTC/USD", 1000.0, T0, 100.0)
        pt.set_funding_rate("BTC/USD", 0.001)
        pt.accrue_funding(T9)
        assert pt.account.positions["BTC/USD"].funding_accrued == pytest.approx(-0.1)

    def test_short_collects_positive_funding(self):
        # delta = +1 * 0.001 * 100 * 1 = +0.1
        pt = perp()
        pt.execute_short("BTC/USD", 1000.0, T0, 100.0)
        pt.set_funding_rate("BTC/USD", 0.001)
        pt.accrue_funding(T9)
        assert pt.account.positions["BTC/USD"].funding_accrued == pytest.approx(0.1)

    def test_multi_cycle_accrual(self):
        # 25h → 3 full cycles: delta = -3 * 0.001 * 100 = -0.3
        pt = perp()
        pt.execute_buy("BTC/USD", 1000.0, T0, 100.0)
        pt.set_funding_rate("BTC/USD", 0.001)
        pt.accrue_funding(T25)
        assert pt.account.positions["BTC/USD"].funding_accrued == pytest.approx(-0.3)

    def test_funding_included_in_close_pnl(self):
        # Close at same price (0 raw pnl) but funding = -0.1
        pt = perp()
        pt.execute_buy("BTC/USD", 1000.0, T0, 100.0)
        pt.set_funding_rate("BTC/USD", 0.001)
        pt.accrue_funding(T9)
        trade = pt.execute_sell("BTC/USD", 1000.0, T9)
        assert trade.pnl == pytest.approx(-0.1)

    def test_long_liq_price_rises_as_funding_paid(self):
        # Funding erodes margin → liq approaches from below
        pt = perp(leverage=2.0)
        pos = pt.execute_buy("BTC/USD", 1000.0, T0, 100.0)
        initial_liq = pos.liquidation_price
        pt.set_funding_rate("BTC/USD", 0.001)
        pt.accrue_funding(T9)
        assert pt.account.positions["BTC/USD"].liquidation_price > initial_liq

    def test_no_accrual_when_no_rate_set(self):
        pt = perp()
        pt.execute_buy("BTC/USD", 1000.0, T0, 100.0)
        pt.accrue_funding(T9)   # no set_funding_rate called
        assert pt.account.positions["BTC/USD"].funding_accrued == pytest.approx(0.0)


# ── liquidation ───────────────────────────────────────────────────────────────

class TestLiquidation:
    def test_long_liquidated_when_price_at_liq(self):
        pt = perp(leverage=2.0)
        pt.execute_buy("BTC/USD", 1000.0, T0, 100.0)
        liq = pt.account.positions["BTC/USD"].liquidation_price
        liquidated = pt.update_unrealized_pnl({"BTC/USD": liq})
        assert "BTC/USD" in liquidated
        assert "BTC/USD" not in pt.account.positions

    def test_long_not_liquidated_above_liq(self):
        pt = perp(leverage=2.0)
        pt.execute_buy("BTC/USD", 1000.0, T0, 100.0)
        liq = pt.account.positions["BTC/USD"].liquidation_price
        liquidated = pt.update_unrealized_pnl({"BTC/USD": liq + 1.0})
        assert "BTC/USD" not in liquidated

    def test_short_liquidated_when_price_at_liq(self):
        pt = perp(leverage=2.0)
        pt.execute_short("BTC/USD", 1000.0, T0, 100.0)
        liq = pt.account.positions["BTC/USD"].liquidation_price
        liquidated = pt.update_unrealized_pnl({"BTC/USD": liq})
        assert "BTC/USD" in liquidated

    def test_liquidation_records_trade_with_side_liquidation(self):
        pt = perp(leverage=2.0)
        pt.execute_buy("BTC/USD", 1000.0, T0, 100.0)
        liq = pt.account.positions["BTC/USD"].liquidation_price
        pt.update_unrealized_pnl({"BTC/USD": liq})
        assert len(pt.account.closed_trades) == 1
        assert pt.account.closed_trades[0].side == 'liquidation'

    def test_spot_position_never_liquidated(self):
        # Spot has liquidation_price=0, so the liq check never fires
        pt = spot()
        pt.execute_buy("BTC/USD", 100.0, T0, 100.0)
        liquidated = pt.update_unrealized_pnl({"BTC/USD": 0.01})
        assert "BTC/USD" not in liquidated


# ── unrealized PnL + excursion tracking ───────────────────────────────────────

class TestUnrealizedPnL:
    def test_long_unrealized_pnl_profit(self):
        pt = spot()
        pt.execute_buy("BTC/USD", 100.0, T0, 100.0)   # size=1
        pt.update_unrealized_pnl({"BTC/USD": 110.0})
        assert pt.account.positions["BTC/USD"].unrealized_pnl == pytest.approx(10.0)

    def test_long_unrealized_pnl_loss(self):
        pt = spot()
        pt.execute_buy("BTC/USD", 100.0, T0, 100.0)
        pt.update_unrealized_pnl({"BTC/USD": 90.0})
        assert pt.account.positions["BTC/USD"].unrealized_pnl == pytest.approx(-10.0)

    def test_excursion_peak_favorable_long(self):
        pt = spot()
        pt.execute_buy("BTC/USD", 100.0, T0, 100.0)
        pt.update_unrealized_pnl({"BTC/USD": 120.0})
        pt.update_unrealized_pnl({"BTC/USD": 90.0})
        pos = pt.account.positions["BTC/USD"]
        assert pos.peak_favorable_price == pytest.approx(120.0)

    def test_excursion_peak_adverse_long(self):
        pt = spot()
        pt.execute_buy("BTC/USD", 100.0, T0, 100.0)
        pt.update_unrealized_pnl({"BTC/USD": 120.0})
        pt.update_unrealized_pnl({"BTC/USD": 80.0})
        pos = pt.account.positions["BTC/USD"]
        assert pos.peak_adverse_price == pytest.approx(80.0)

    def test_missing_symbol_ignored_gracefully(self):
        pt = spot()
        pt.execute_buy("BTC/USD", 100.0, T0, 100.0)
        # ETH not in positions → should not raise
        pt.update_unrealized_pnl({"ETH/USD": 3000.0})
        assert pt.account.positions["BTC/USD"].unrealized_pnl == pytest.approx(0.0)

    def test_perp_unrealized_includes_funding(self):
        pt = perp()
        pos = pt.execute_buy("BTC/USD", 1000.0, T0, 100.0)
        # Manually set funding to simulate one cycle
        pos.funding_accrued = -5.0
        pt.update_unrealized_pnl({"BTC/USD": 1000.0})  # no price move
        # unrealized_pnl = 0 + (-5) = -5
        assert pos.unrealized_pnl == pytest.approx(-5.0)


# ── account summary ───────────────────────────────────────────────────────────

class TestAccountSummary:
    def test_empty_account(self):
        pt = spot()
        s = pt.get_account_summary()
        assert s['cash'] == pytest.approx(1000.0)
        assert s['total_equity'] == pytest.approx(1000.0)
        assert s['open_positions'] == 0
        assert s['closed_trades'] == 0

    def test_equity_includes_open_spot_position(self):
        # cash=900, pos_val=100+10=110, equity=1010
        pt = spot()
        pt.execute_buy("BTC/USD", 100.0, T0, 100.0)
        pt.update_unrealized_pnl({"BTC/USD": 110.0})
        s = pt.get_account_summary()
        assert s['total_equity'] == pytest.approx(1010.0)

    def test_perp_equity_uses_margin_not_notional(self):
        # margin=50, cash=950, unrealized=10 → equity=1010
        pt = perp(leverage=2.0)
        pt.execute_buy("BTC/USD", 1000.0, T0, 100.0)   # notional=100, margin=50
        pt.update_unrealized_pnl({"BTC/USD": 1100.0})   # +10
        s = pt.get_account_summary()
        assert s['total_equity'] == pytest.approx(1010.0)

    def test_win_loss_counts(self):
        pt = spot()
        pt.execute_buy("BTC/USD", 100.0, T0, 100.0)
        pt.execute_sell("BTC/USD", 110.0, T1)                             # win
        pt.execute_buy("ETH/USD", 100.0, T1, 100.0)
        pt.execute_sell("ETH/USD", 90.0, T0 + timedelta(hours=2))         # loss
        s = pt.get_account_summary()
        assert s['winning_trades'] == 1
        assert s['losing_trades'] == 1

    def test_pnl_pct_calculation(self):
        pt = spot()
        pt.execute_buy("BTC/USD", 100.0, T0, 500.0)
        pt.execute_sell("BTC/USD", 110.0, T1)    # +50 on 1000 initial = 5%
        s = pt.get_account_summary()
        assert s['pnl_pct'] == pytest.approx(5.0)

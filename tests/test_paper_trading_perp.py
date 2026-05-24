"""
Unit tests for PaperTrader perp (futures) mode accounting.

Covers:
- execute_buy (perp long): margin locked, cash deduction, notional vs margin
- execute_sell (perp long close): cash credit, accounting identity
- execute_short (perp short): margin locked, cash deduction
- execute_cover (perp short close): cash credit, accounting identity
- get_account_summary with open perp position: equity == cash + margin + unrealized
- Accounting identity invariant: total_equity == initial_capital + total_pnl
  (after close, no open positions)
- Leveraged PnL: gains/losses scale with full notional, not just margin
- accrue_funding: funding deducted (long) / credited (short) over 8h cycles
"""

import pytest
from datetime import datetime, timezone, timedelta
from src.paper_trading import PaperTrader, PaperPosition

# ── Fixtures ───────────────────────────────────────────────────────────────────

def _perp_trader(initial_capital: float = 1_000.0,
                 fee_pct: float = 0.26,
                 slippage_pct: float = 0.0,
                 leverage: float = 2.0) -> PaperTrader:
    return PaperTrader(
        initial_capital=initial_capital,
        fee_pct=fee_pct,
        slippage_pct=slippage_pct,
        perp_mode=True,
        leverage=leverage,
    )


T0 = datetime(2024, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
T1 = datetime(2024, 1, 1, 0, 1, 0, tzinfo=timezone.utc)
SYMBOL = "BTC/USD"
PRICE  = 50_000.0


# ── execute_buy (perp long open) ───────────────────────────────────────────────

class TestPerpExecuteBuy:
    def test_returns_position(self):
        t = _perp_trader()
        pos = t.execute_buy(SYMBOL, PRICE, T0, size_usd=100.0)
        assert pos is not None

    def test_position_is_perp(self):
        t = _perp_trader()
        pos = t.execute_buy(SYMBOL, PRICE, T0, size_usd=100.0)
        assert pos.is_perp is True

    def test_margin_locked_equals_notional_over_leverage(self):
        # size_usd=100 → notional=$100, leverage=2 → margin=$50
        t = _perp_trader(leverage=2.0, fee_pct=0.0)
        pos = t.execute_buy(SYMBOL, PRICE, T0, size_usd=100.0)
        assert abs(pos.margin_locked - 50.0) < 1e-6

    def test_cash_reduced_by_margin_plus_fee(self):
        # notional=100, leverage=2 → margin=50; fee=100*0.0026=0.26
        t = _perp_trader(initial_capital=1_000.0, leverage=2.0, fee_pct=0.26)
        t.execute_buy(SYMBOL, PRICE, T0, size_usd=100.0)
        expected_fee    = 100.0 * 0.0026
        expected_margin = 50.0
        assert abs(t.account.cash - (1_000.0 - expected_margin - expected_fee)) < 1e-6

    def test_cash_not_reduced_by_full_notional(self):
        """With 2x leverage, cash outflow must be ~half the notional, not the full notional."""
        t = _perp_trader(initial_capital=1_000.0, leverage=2.0, fee_pct=0.0)
        t.execute_buy(SYMBOL, PRICE, T0, size_usd=100.0)
        # notional=$100, margin=$50 → cash should be 950, not 900
        assert t.account.cash > 940.0
        assert abs(t.account.cash - 950.0) < 1e-6

    def test_no_position_with_zero_cash(self):
        t = _perp_trader(initial_capital=0.0)
        pos = t.execute_buy(SYMBOL, PRICE, T0, size_usd=100.0)
        assert pos is None


# ── execute_sell (perp long close) ────────────────────────────────────────────

class TestPerpExecuteSell:
    def test_returns_trade(self):
        t = _perp_trader()
        t.execute_buy(SYMBOL, PRICE, T0, size_usd=100.0)
        trade = t.execute_sell(SYMBOL, PRICE * 1.05, T1)
        assert trade is not None

    def test_position_removed(self):
        t = _perp_trader()
        t.execute_buy(SYMBOL, PRICE, T0, size_usd=100.0)
        t.execute_sell(SYMBOL, PRICE * 1.05, T1)
        assert SYMBOL not in t.account.positions

    def test_pnl_positive_when_price_rises(self):
        t = _perp_trader(fee_pct=0.0, leverage=2.0)
        t.execute_buy(SYMBOL, PRICE, T0, size_usd=100.0)
        trade = t.execute_sell(SYMBOL, PRICE * 1.10, T1)
        assert trade.pnl > 0

    def test_pnl_negative_when_price_falls(self):
        t = _perp_trader(fee_pct=0.0, leverage=2.0)
        t.execute_buy(SYMBOL, PRICE, T0, size_usd=100.0)
        trade = t.execute_sell(SYMBOL, PRICE * 0.90, T1)
        assert trade.pnl < 0

    def test_leveraged_pnl_scales_with_notional(self):
        """2x leverage on $100 notional: 10% move → $10 gross PnL (= 20% on $50 margin)."""
        t = _perp_trader(fee_pct=0.0, leverage=2.0)
        t.execute_buy(SYMBOL, PRICE, T0, size_usd=100.0)
        trade = t.execute_sell(SYMBOL, PRICE * 1.10, T1)
        # size = 100/50000 = 0.002 BTC; pnl = 0.002 * (55000-50000) = $10
        assert abs(trade.pnl - 10.0) < 1e-6

    def test_round_trip_same_price_loses_fees(self):
        t = _perp_trader(fee_pct=0.26, leverage=2.0)
        t.execute_buy(SYMBOL, PRICE, T0, size_usd=100.0)
        trade = t.execute_sell(SYMBOL, PRICE, T1)
        assert trade.pnl < 0


# ── Perp accounting identity (long) ───────────────────────────────────────────

class TestPerpAccountingIdentityLong:
    """
    Core invariant: after all positions are closed,
        total_equity == initial_capital + total_pnl

    The pre-fix bug: execute_sell for perp did
        cash += margin_locked + pnl
    but pnl already deducts entry_fee (via total_fees), so entry_fee was
    double-counted. Fixed to:
        cash += margin_locked + entry_fee + pnl
    """

    def test_identity_holds_after_profitable_perp_long(self):
        t = _perp_trader(fee_pct=0.26, leverage=2.0)
        t.execute_buy(SYMBOL, PRICE, T0, size_usd=100.0)
        t.execute_sell(SYMBOL, PRICE * 1.05, T1)
        s = t.get_account_summary()
        assert abs(s["total_equity"] - (1_000.0 + s["total_pnl"])) < 1e-6

    def test_identity_holds_after_losing_perp_long(self):
        t = _perp_trader(fee_pct=0.26, leverage=2.0)
        t.execute_buy(SYMBOL, PRICE, T0, size_usd=100.0)
        t.execute_sell(SYMBOL, PRICE * 0.97, T1)
        s = t.get_account_summary()
        assert abs(s["total_equity"] - (1_000.0 + s["total_pnl"])) < 1e-6

    def test_identity_holds_same_price_round_trip(self):
        t = _perp_trader(fee_pct=0.26, leverage=2.0)
        t.execute_buy(SYMBOL, PRICE, T0, size_usd=100.0)
        t.execute_sell(SYMBOL, PRICE, T1)
        s = t.get_account_summary()
        assert abs(s["total_equity"] - (1_000.0 + s["total_pnl"])) < 1e-6

    def test_identity_holds_zero_fee(self):
        t = _perp_trader(fee_pct=0.0, leverage=2.0)
        t.execute_buy(SYMBOL, PRICE, T0, size_usd=100.0)
        t.execute_sell(SYMBOL, PRICE * 1.10, T1)
        s = t.get_account_summary()
        assert abs(s["total_equity"] - (1_000.0 + s["total_pnl"])) < 1e-6

    def test_identity_holds_1x_leverage(self):
        """At 1x leverage perp should behave identically to spot."""
        t = _perp_trader(fee_pct=0.26, leverage=1.0)
        t.execute_buy(SYMBOL, PRICE, T0, size_usd=100.0)
        t.execute_sell(SYMBOL, PRICE * 1.05, T1)
        s = t.get_account_summary()
        assert abs(s["total_equity"] - (1_000.0 + s["total_pnl"])) < 1e-6

    def test_identity_holds_high_leverage(self):
        t = _perp_trader(fee_pct=0.26, leverage=5.0)
        t.execute_buy(SYMBOL, PRICE, T0, size_usd=100.0)
        t.execute_sell(SYMBOL, PRICE * 1.03, T1)
        s = t.get_account_summary()
        assert abs(s["total_equity"] - (1_000.0 + s["total_pnl"])) < 1e-6

    def test_identity_holds_across_multiple_perp_trades(self):
        t = _perp_trader(fee_pct=0.26, leverage=2.0, initial_capital=5_000.0)
        prices = [(50_000.0, 51_000.0), (51_000.0, 50_000.0), (50_500.0, 52_000.0)]
        for i, (ep, cp) in enumerate(prices):
            ent = datetime(2024, 1, i * 2 + 1, tzinfo=timezone.utc)
            ext = datetime(2024, 1, i * 2 + 2, tzinfo=timezone.utc)
            t.execute_buy(SYMBOL, ep, ent, size_usd=200.0)
            t.execute_sell(SYMBOL, cp, ext)
        s = t.get_account_summary()
        assert abs(s["total_equity"] - (5_000.0 + s["total_pnl"])) < 1e-6


# ── Perp accounting identity (short) ──────────────────────────────────────────

class TestPerpAccountingIdentityShort:
    """
    Same invariant for perp shorts. Pre-fix bug identical to longs:
        cash += margin_locked + pnl  (wrong — entry_fee double-counted)
    Fixed to:
        cash += margin_locked + entry_fee + pnl
    """

    def test_identity_holds_after_profitable_perp_short(self):
        t = _perp_trader(fee_pct=0.26, leverage=2.0)
        t.execute_short(SYMBOL, PRICE, T0, size_usd=100.0)
        t.execute_cover(SYMBOL, PRICE * 0.95, T1)
        s = t.get_account_summary()
        assert abs(s["total_equity"] - (1_000.0 + s["total_pnl"])) < 1e-6

    def test_identity_holds_after_losing_perp_short(self):
        t = _perp_trader(fee_pct=0.26, leverage=2.0)
        t.execute_short(SYMBOL, PRICE, T0, size_usd=100.0)
        t.execute_cover(SYMBOL, PRICE * 1.03, T1)
        s = t.get_account_summary()
        assert abs(s["total_equity"] - (1_000.0 + s["total_pnl"])) < 1e-6

    def test_identity_holds_same_price_short_round_trip(self):
        t = _perp_trader(fee_pct=0.26, leverage=2.0)
        t.execute_short(SYMBOL, PRICE, T0, size_usd=100.0)
        t.execute_cover(SYMBOL, PRICE, T1)
        s = t.get_account_summary()
        assert abs(s["total_equity"] - (1_000.0 + s["total_pnl"])) < 1e-6

    def test_leveraged_short_pnl(self):
        """Short with 2x leverage on $100 notional: -5% price → +$5 gross PnL."""
        t = _perp_trader(fee_pct=0.0, leverage=2.0)
        t.execute_short(SYMBOL, PRICE, T0, size_usd=100.0)
        trade = t.execute_cover(SYMBOL, PRICE * 0.95, T1)
        # size = 0.002 BTC; pnl = 0.002 * (50000 - 47500) = $5
        assert abs(trade.pnl - 5.0) < 1e-6


# ── get_account_summary with open perp position ────────────────────────────────

class TestPerpGetAccountSummaryOpen:
    """
    Pre-fix bug: get_account_summary used entry_price * size (full notional)
    for all positions. For 2x leverage, this inflated equity by
    (leverage - 1) * margin_locked per open perp position.

    Fixed to use margin_locked for perp positions.
    """

    def test_equity_at_entry_reflects_margin_not_notional(self):
        """
        Open 2x perp long, no price move, no fees.
        equity == initial - margin_locked (no unrealized PnL yet), not initial.
        Specifically: cash = initial - margin, pos_val = margin → equity = initial.
        With fee: equity = initial - entry_fee.
        """
        t = _perp_trader(initial_capital=1_000.0, fee_pct=0.0, leverage=2.0)
        t.execute_buy(SYMBOL, PRICE, T0, size_usd=100.0)
        t.update_unrealized_pnl({SYMBOL: PRICE})  # no price move
        s = t.get_account_summary()
        # cash = 1000 - 50 (margin) = 950; pos_val = 50 (margin) + 0 (unreal) = 50
        # equity = 950 + 50 = 1000
        assert abs(s["total_equity"] - 1_000.0) < 1e-6

    def test_equity_not_inflated_by_leverage(self):
        """
        2x leverage: before fix, equity was inflated by 50 (= (2-1)*margin).
        After fix, equity at entry with no fee = initial_capital exactly.
        """
        t = _perp_trader(initial_capital=1_000.0, fee_pct=0.0, leverage=2.0)
        pos = t.execute_buy(SYMBOL, PRICE, T0, size_usd=100.0)
        t.update_unrealized_pnl({SYMBOL: PRICE})
        s = t.get_account_summary()
        # Before fix: equity = 950 + (50000 * 0.002) + 0 = 950 + 100 = 1050 (WRONG)
        # After fix:  equity = 950 + 50 + 0 = 1000 (CORRECT)
        assert abs(s["total_equity"] - 1_000.0) < 1e-6

    def test_equity_includes_unrealized_pnl(self):
        """Price rises 10% → unrealized PnL = +$10; equity should reflect that."""
        t = _perp_trader(initial_capital=1_000.0, fee_pct=0.0, leverage=2.0)
        t.execute_buy(SYMBOL, PRICE, T0, size_usd=100.0)
        t.update_unrealized_pnl({SYMBOL: PRICE * 1.10})
        s = t.get_account_summary()
        # unrealized = (55000 - 50000) * 0.002 = $10
        # equity = 950 (cash) + 50 (margin) + 10 (unreal) = 1010
        assert abs(s["total_equity"] - 1_010.0) < 1e-6

    def test_equity_decreases_on_adverse_move(self):
        """Price drops 5% → unrealized = -$5; equity decreases."""
        t = _perp_trader(initial_capital=1_000.0, fee_pct=0.0, leverage=2.0)
        t.execute_buy(SYMBOL, PRICE, T0, size_usd=100.0)
        t.update_unrealized_pnl({SYMBOL: PRICE * 0.95})
        s = t.get_account_summary()
        assert s["total_equity"] < 1_000.0
        assert abs(s["total_equity"] - 995.0) < 1e-6

    def test_spot_equity_unchanged_by_fix(self):
        """Spot (non-perp) positions should value the same as before."""
        from src.paper_trading import PaperTrader as PT
        t = PT(initial_capital=1_000.0, fee_pct=0.0, slippage_pct=0.0)
        t.execute_buy(SYMBOL, PRICE, T0, size_usd=100.0)
        t.update_unrealized_pnl({SYMBOL: PRICE})
        s = t.get_account_summary()
        # Spot: cash = 900, pos_val = 100, equity = 1000
        assert abs(s["total_equity"] - 1_000.0) < 1e-6


# ── accrue_funding ─────────────────────────────────────────────────────────────

class TestPerpAccrueFunding:
    def test_no_accrual_for_spot_mode(self):
        from src.paper_trading import PaperTrader as PT
        t = PT(initial_capital=1_000.0, fee_pct=0.0, slippage_pct=0.0)
        t.execute_buy(SYMBOL, PRICE, T0, size_usd=100.0)
        pos = t.account.positions[SYMBOL]
        t.accrue_funding(T0 + timedelta(hours=8))
        assert pos.funding_accrued == 0.0  # spot, no-op

    def test_long_pays_positive_funding(self):
        """Positive funding rate → long pays; funding_accrued < 0."""
        t = _perp_trader(fee_pct=0.0, leverage=1.0)
        t.set_funding_rate(SYMBOL, 0.0001)   # 0.01% per 8h
        t.execute_buy(SYMBOL, PRICE, T0, size_usd=100.0)
        pos = t.account.positions[SYMBOL]
        pos.last_funding_ts = T0
        t.accrue_funding(T0 + timedelta(hours=8))
        # notional = 100; payment = -0.0001 * 100 = -$0.01
        assert pos.funding_accrued < 0.0
        assert abs(pos.funding_accrued - (-0.01)) < 1e-9

    def test_short_collects_positive_funding(self):
        """Positive funding rate → short collects; funding_accrued > 0."""
        t = _perp_trader(fee_pct=0.0, leverage=1.0)
        t.set_funding_rate(SYMBOL, 0.0001)
        t.execute_short(SYMBOL, PRICE, T0, size_usd=100.0)
        pos = t.account.positions[SYMBOL]
        pos.last_funding_ts = T0
        t.accrue_funding(T0 + timedelta(hours=8))
        assert pos.funding_accrued > 0.0
        assert abs(pos.funding_accrued - 0.01) < 1e-9

    def test_no_accrual_for_partial_cycle(self):
        """Less than 8 hours elapsed → no full funding cycle yet."""
        t = _perp_trader(fee_pct=0.0, leverage=1.0)
        t.set_funding_rate(SYMBOL, 0.0001)
        t.execute_buy(SYMBOL, PRICE, T0, size_usd=100.0)
        pos = t.account.positions[SYMBOL]
        pos.last_funding_ts = T0
        t.accrue_funding(T0 + timedelta(hours=7, minutes=59))
        assert pos.funding_accrued == 0.0

    def test_multiple_funding_cycles(self):
        """24h elapsed = 3 × 8h cycles; funding triples."""
        t = _perp_trader(fee_pct=0.0, leverage=1.0)
        t.set_funding_rate(SYMBOL, 0.0001)
        t.execute_buy(SYMBOL, PRICE, T0, size_usd=100.0)
        pos = t.account.positions[SYMBOL]
        pos.last_funding_ts = T0
        t.accrue_funding(T0 + timedelta(hours=24))
        # 3 cycles × -0.0001 × 100 = -$0.03
        assert abs(pos.funding_accrued - (-0.03)) < 1e-9

    def test_funding_affects_pnl_on_close(self):
        """Funding accrued during the hold period is reflected in the closed trade PnL."""
        t = _perp_trader(fee_pct=0.0, leverage=1.0)
        t.set_funding_rate(SYMBOL, 0.0001)
        t.execute_buy(SYMBOL, PRICE, T0, size_usd=100.0)
        pos = t.account.positions[SYMBOL]
        pos.last_funding_ts = T0
        close_time = T0 + timedelta(hours=8)
        trade = t.execute_sell(SYMBOL, PRICE, close_time)
        # At same price, pnl = funding_accrued = -$0.01
        assert abs(trade.pnl - (-0.01)) < 1e-9


# ── update_unrealized_pnl includes funding_accrued for perp ───────────────────

class TestPerpUnrealizedPnlIncludesFunding:
    """
    update_unrealized_pnl must add funding_accrued to unrealized PnL for perp
    positions so that get_account_summary() and the daily circuit breaker see
    the true equity — not an inflated value that ignores ongoing funding costs.

    Bug: before the fix, update_unrealized_pnl used only the price-based PnL,
    so accrued funding was invisible until the position was closed.
    """

    def test_long_equity_reduced_by_accrued_funding(self):
        """After one 8h cycle at 0.01%/8h, long equity drops by $0.01."""
        t = _perp_trader(fee_pct=0.0, leverage=1.0, initial_capital=1_000.0)
        t.set_funding_rate(SYMBOL, 0.0001)   # 0.01% per 8h
        t.execute_buy(SYMBOL, PRICE, T0, size_usd=100.0)
        pos = t.account.positions[SYMBOL]
        pos.last_funding_ts = T0
        t.accrue_funding(T0 + timedelta(hours=8))
        # funding_accrued = -0.0001 * 100 = -$0.01
        assert abs(pos.funding_accrued - (-0.01)) < 1e-9

        t.update_unrealized_pnl({SYMBOL: PRICE})   # no price move
        s = t.get_account_summary()
        # Without funding: equity = 1000; with funding: equity = 999.99
        assert abs(s["total_equity"] - 999.99) < 1e-6

    def test_short_equity_increased_by_collected_funding(self):
        """After one 8h cycle at 0.01%/8h, short equity rises by $0.01."""
        t = _perp_trader(fee_pct=0.0, leverage=1.0, initial_capital=1_000.0)
        t.set_funding_rate(SYMBOL, 0.0001)
        t.execute_short(SYMBOL, PRICE, T0, size_usd=100.0)
        pos = t.account.positions[SYMBOL]
        pos.last_funding_ts = T0
        t.accrue_funding(T0 + timedelta(hours=8))
        # funding_accrued = +0.0001 * 100 = +$0.01
        assert abs(pos.funding_accrued - 0.01) < 1e-9

        t.update_unrealized_pnl({SYMBOL: PRICE})
        s = t.get_account_summary()
        assert abs(s["total_equity"] - 1_000.01) < 1e-6

    def test_multiple_cycles_compound_correctly(self):
        """3 × 8h cycles → funding_accrued = -$0.03; equity down by $0.03."""
        t = _perp_trader(fee_pct=0.0, leverage=1.0, initial_capital=1_000.0)
        t.set_funding_rate(SYMBOL, 0.0001)
        t.execute_buy(SYMBOL, PRICE, T0, size_usd=100.0)
        pos = t.account.positions[SYMBOL]
        pos.last_funding_ts = T0
        t.accrue_funding(T0 + timedelta(hours=24))   # 3 cycles
        t.update_unrealized_pnl({SYMBOL: PRICE})
        s = t.get_account_summary()
        assert abs(s["total_equity"] - 999.97) < 1e-6

    def test_funding_plus_price_move_combined(self):
        """Price +10% AND one funding cycle: equity = 1000 + 10 - 0.01 = 1009.99."""
        t = _perp_trader(fee_pct=0.0, leverage=1.0, initial_capital=1_000.0)
        t.set_funding_rate(SYMBOL, 0.0001)
        t.execute_buy(SYMBOL, PRICE, T0, size_usd=100.0)
        pos = t.account.positions[SYMBOL]
        pos.last_funding_ts = T0
        t.accrue_funding(T0 + timedelta(hours=8))
        t.update_unrealized_pnl({SYMBOL: PRICE * 1.10})   # +10% → +$10 raw PnL
        s = t.get_account_summary()
        # raw_unrealized = (55000 - 50000) * 0.002 = $10; funding = -$0.01
        # equity = 900 (cash) + 100 (margin) + 10 - 0.01 = 1009.99
        assert abs(s["total_equity"] - 1_009.99) < 1e-6

    def test_no_funding_zero_accrual_unchanged(self):
        """Zero funding_accrued: unrealized PnL is purely price-based, no change."""
        t = _perp_trader(fee_pct=0.0, leverage=1.0, initial_capital=1_000.0)
        t.execute_buy(SYMBOL, PRICE, T0, size_usd=100.0)
        t.update_unrealized_pnl({SYMBOL: PRICE * 1.05})
        s = t.get_account_summary()
        # raw_unrealized = (52500 - 50000) * 0.002 = $5; funding = $0
        assert abs(s["total_equity"] - 1_005.0) < 1e-6

    def test_spot_position_unaffected_by_funding(self):
        """Spot positions have funding_accrued=0 always; equity is price-based only."""
        from src.paper_trading import PaperTrader as PT
        t = PT(initial_capital=1_000.0, fee_pct=0.0, slippage_pct=0.0)
        t.execute_buy(SYMBOL, PRICE, T0, size_usd=100.0)
        pos = t.account.positions[SYMBOL]
        pos.funding_accrued = -99.0   # forcibly set to check it's NOT used for spot
        t.update_unrealized_pnl({SYMBOL: PRICE})
        s = t.get_account_summary()
        # Spot: unrealized_pnl should NOT include funding_accrued
        assert abs(s["total_equity"] - 1_000.0) < 1e-6

    def test_accounting_identity_holds_with_funding_while_open(self):
        """
        Verify that equity with an open perp position matches the expected value:
        cash + margin + (price_move * size) + funding_accrued.

        size = 100/50000 = 0.002 BTC; 5% move = $2500 price delta → raw = $5.
        """
        t = _perp_trader(fee_pct=0.0, leverage=1.0, initial_capital=1_000.0)
        t.set_funding_rate(SYMBOL, 0.0001)
        t.execute_buy(SYMBOL, PRICE, T0, size_usd=100.0)
        pos = t.account.positions[SYMBOL]
        pos.last_funding_ts = T0
        t.accrue_funding(T0 + timedelta(hours=16))   # 2 cycles, -$0.02
        t.update_unrealized_pnl({SYMBOL: PRICE * 1.05})   # +5 raw PnL (0.002 * 2500)
        s = t.get_account_summary()
        # cash=900, margin=100, price_pnl=5, funding=-0.02 → equity = 1004.98
        assert abs(s["total_equity"] - 1_004.98) < 1e-6

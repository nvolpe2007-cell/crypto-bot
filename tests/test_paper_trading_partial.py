"""Unit tests for PaperTrader partial exit accounting.

execute_partial_sell  — closes a fraction of a long position
execute_partial_cover — closes a fraction of a short position

Key invariants verified:
  1. Position size decreases by the correct fraction.
  2. Cash delta equals proceeds minus exit fee (no more, no less).
  3. pnl_partial is net of exit fee (not gross).
  4. A 50-50 split (partial + final full close) produces identical cash and
     total_pnl to a single full close at the same prices.

Invariant 4 is the critical regression test: if partial close accounting is
wrong, the two paths diverge and the paper trader gives unrealistic results.
"""

import pytest
from datetime import datetime, timezone

from src.paper_trading import PaperTrader

SYMBOL = "BTC/USD"
PRICE  = 50_000.0     # reference market price
T0     = datetime(2024, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
T1     = datetime(2024, 1, 1, 0, 1, 0, tzinfo=timezone.utc)

# Use round numbers so arithmetic is easy to verify.
FEE_PCT  = 0.26   # Kraken taker (percent, passed to PaperTrader as fee_pct)
SLIP_PCT = 0.10   # flat slippage floor (percent)


def _trader() -> PaperTrader:
    return PaperTrader(
        initial_capital=1_000.0,
        fee_pct=FEE_PCT,
        slippage_pct=SLIP_PCT,
    )


# ── helpers ───────────────────────────────────────────────────────────────────

def _exec_buy(t: PaperTrader, size_usd: float = 100.0) -> float:
    """Open a long and return the entry exec_price."""
    t.execute_buy(SYMBOL, PRICE, T0, size_usd=size_usd)
    return t.account.positions[SYMBOL].entry_price


def _exec_short(t: PaperTrader, size_usd: float = 100.0) -> float:
    """Open a short and return the entry exec_price."""
    t.execute_short(SYMBOL, PRICE, T0, size_usd=size_usd)
    return t.account.positions[SYMBOL].entry_price


# ── execute_partial_sell ──────────────────────────────────────────────────────

class TestPartialSell:
    """execute_partial_sell: closes half of a long position."""

    # ── guard conditions ──────────────────────────────────────────────────────

    def test_returns_none_when_no_position(self):
        t = _trader()
        assert t.execute_partial_sell(SYMBOL, PRICE, T1) is None

    def test_returns_none_for_short_position(self):
        t = _trader()
        _exec_short(t)
        assert t.execute_partial_sell(SYMBOL, PRICE, T1) is None

    def test_position_still_open_after_partial(self):
        t = _trader()
        _exec_buy(t)
        t.execute_partial_sell(SYMBOL, PRICE * 1.01, T1)
        assert SYMBOL in t.account.positions

    # ── size accounting ───────────────────────────────────────────────────────

    def test_reduces_position_size_by_half(self):
        t = _trader()
        _exec_buy(t)
        original_size = t.account.positions[SYMBOL].size
        t.execute_partial_sell(SYMBOL, PRICE * 1.02, T1)
        remaining = t.account.positions[SYMBOL].size
        assert abs(remaining - original_size / 2) < 1e-10

    def test_custom_fraction_respected(self):
        t = _trader()
        _exec_buy(t)
        original_size = t.account.positions[SYMBOL].size
        t.execute_partial_sell(SYMBOL, PRICE, T1, fraction=0.25)
        remaining = t.account.positions[SYMBOL].size
        assert abs(remaining - original_size * 0.75) < 1e-10

    # ── cash accounting ───────────────────────────────────────────────────────

    def test_cash_increases_by_proceeds_minus_fee(self):
        t = _trader()
        _exec_buy(t)
        cash_before = t.account.cash
        pos = t.account.positions[SYMBOL]
        partial_size = pos.size * 0.5
        slip = t.slippage_pct
        exec_price = PRICE * 1.02 * (1 - slip)
        expected_fee = exec_price * partial_size * t.fee_pct
        expected_cash_delta = exec_price * partial_size - expected_fee

        t.execute_partial_sell(SYMBOL, PRICE * 1.02, T1)

        cash_delta = t.account.cash - cash_before
        assert abs(cash_delta - expected_cash_delta) < 1e-8

    def test_cash_correct_at_breakeven_price(self):
        """Selling at entry price: cash change = proceeds - exit_fee (small net positive)."""
        t = _trader()
        entry_exec = _exec_buy(t)
        cash_before = t.account.cash
        pos = t.account.positions[SYMBOL]
        partial_size = pos.size * 0.5
        slip = t.slippage_pct
        # Sell at the original market price → exec_price slightly below due to slip
        exec_price = PRICE * (1 - slip)
        expected_delta = exec_price * partial_size - exec_price * partial_size * t.fee_pct

        t.execute_partial_sell(SYMBOL, PRICE, T1)

        assert abs((t.account.cash - cash_before) - expected_delta) < 1e-8

    # ── pnl accounting ────────────────────────────────────────────────────────

    def test_pnl_partial_is_net_of_exit_fee(self):
        """At break-even price, pnl_partial = −exit_fee (only fee is deducted)."""
        t = _trader()
        _exec_buy(t)
        pos = t.account.positions[SYMBOL]
        partial_size = pos.size * 0.5
        slip = t.slippage_pct
        exec_price = PRICE * (1 - slip)
        exit_fee = exec_price * partial_size * t.fee_pct
        # Gross price P&L ≈ 0 at break-even; net = -fee
        gross_pnl = (exec_price - pos.entry_price) * partial_size
        expected_pnl = gross_pnl - exit_fee

        pnl = t.execute_partial_sell(SYMBOL, PRICE, T1)

        assert abs(pnl - expected_pnl) < 1e-8

    def test_total_pnl_updated_by_pnl_partial(self):
        t = _trader()
        _exec_buy(t)
        pnl_partial = t.execute_partial_sell(SYMBOL, PRICE * 1.02, T1)
        assert abs(t.account.total_pnl - pnl_partial) < 1e-10

    # ── consistency: partial + full == single full ────────────────────────────

    def test_partial_plus_full_cash_equals_single_full_close(self):
        """Cash balance after partial-sell + execute_sell must equal a single
        execute_sell at the same prices — regardless of the interim split."""
        # Path A: open, sell half at PRICE, sell remainder at PRICE*1.02
        t_a = _trader()
        _exec_buy(t_a)
        t_a.execute_partial_sell(SYMBOL, PRICE, T1)
        t_a.execute_sell(SYMBOL, PRICE * 1.02, T1)

        # Path B: open, sell everything at PRICE (half) then PRICE*1.02 (half)
        # → not a meaningful comparison; instead compare to a weighted average.
        #
        # Actual invariant: split at the SAME single price must match full close.
        t_a2 = _trader()
        _exec_buy(t_a2)
        t_a2.execute_partial_sell(SYMBOL, PRICE * 1.02, T1)
        t_a2.execute_sell(SYMBOL, PRICE * 1.02, T1)

        t_b = _trader()
        _exec_buy(t_b)
        t_b.execute_sell(SYMBOL, PRICE * 1.02, T1)

        assert abs(t_a2.account.cash - t_b.account.cash) < 1e-6

    def test_partial_plus_full_pnl_equals_single_full_pnl(self):
        """total_pnl must agree between partial-then-full and single-full paths."""
        exit_price = PRICE * 1.02

        t_a = _trader()
        _exec_buy(t_a)
        t_a.execute_partial_sell(SYMBOL, exit_price, T1)
        t_a.execute_sell(SYMBOL, exit_price, T1)

        t_b = _trader()
        _exec_buy(t_b)
        t_b.execute_sell(SYMBOL, exit_price, T1)

        assert abs(t_a.account.total_pnl - t_b.account.total_pnl) < 1e-6


# ── execute_partial_cover ─────────────────────────────────────────────────────

class TestPartialCover:
    """execute_partial_cover: closes half of a short position."""

    # ── guard conditions ──────────────────────────────────────────────────────

    def test_returns_none_when_no_position(self):
        t = _trader()
        assert t.execute_partial_cover(SYMBOL, PRICE, T1) is None

    def test_returns_none_for_long_position(self):
        t = _trader()
        _exec_buy(t)
        assert t.execute_partial_cover(SYMBOL, PRICE, T1) is None

    def test_position_still_open_after_partial(self):
        t = _trader()
        _exec_short(t)
        t.execute_partial_cover(SYMBOL, PRICE * 0.99, T1)
        assert SYMBOL in t.account.positions

    # ── size accounting ───────────────────────────────────────────────────────

    def test_reduces_position_size_by_half(self):
        t = _trader()
        _exec_short(t)
        original_size = t.account.positions[SYMBOL].size
        t.execute_partial_cover(SYMBOL, PRICE * 0.98, T1)
        remaining = t.account.positions[SYMBOL].size
        assert abs(remaining - original_size / 2) < 1e-10

    def test_custom_fraction_respected(self):
        t = _trader()
        _exec_short(t)
        original_size = t.account.positions[SYMBOL].size
        t.execute_partial_cover(SYMBOL, PRICE, T1, fraction=0.30)
        remaining = t.account.positions[SYMBOL].size
        assert abs(remaining - original_size * 0.70) < 1e-10

    # ── pnl accounting ────────────────────────────────────────────────────────

    def test_pnl_partial_is_net_of_exit_fee(self):
        """Covering at same market price as entry: pnl = gross − exit_fee."""
        t = _trader()
        _exec_short(t)
        pos = t.account.positions[SYMBOL]
        partial_size = pos.size * 0.5
        slip = t.slippage_pct
        exec_price = PRICE * (1 + slip)   # cover at a slightly higher price
        exit_fee = exec_price * partial_size * t.fee_pct
        gross_pnl = (pos.entry_price - exec_price) * partial_size
        expected_pnl = gross_pnl - exit_fee

        pnl = t.execute_partial_cover(SYMBOL, PRICE, T1)

        assert abs(pnl - expected_pnl) < 1e-8

    def test_total_pnl_updated_by_pnl_partial(self):
        t = _trader()
        _exec_short(t)
        pnl_partial = t.execute_partial_cover(SYMBOL, PRICE * 0.99, T1)
        assert abs(t.account.total_pnl - pnl_partial) < 1e-10

    # ── cash accounting ───────────────────────────────────────────────────────

    def test_cash_deducts_exit_fee(self):
        """Covering at break-even market price: cash should decrease by exit_fee only
        (collateral released equals cost to buy back; only fee leaves the account)."""
        t = _trader()
        _exec_short(t)
        cash_before = t.account.cash
        pos = t.account.positions[SYMBOL]
        partial_size = pos.size * 0.5
        slip = t.slippage_pct
        exec_price = PRICE * (1 + slip)
        exit_fee = exec_price * partial_size * t.fee_pct
        # At break-even: (entry_price - exec_price) × partial ≈ −slippage_loss
        gross_gain = (pos.entry_price - exec_price) * partial_size
        expected_cash_delta = pos.entry_price * partial_size + gross_gain - exit_fee

        t.execute_partial_cover(SYMBOL, PRICE, T1)

        cash_delta = t.account.cash - cash_before
        assert abs(cash_delta - expected_cash_delta) < 1e-8

    # ── consistency: partial + full == single full ────────────────────────────

    def test_partial_plus_full_cash_equals_single_full_cover(self):
        """Cash balance after partial-cover + execute_cover must equal a single
        execute_cover at the same prices."""
        cover_price = PRICE * 0.98   # profitable short

        t_a = _trader()
        _exec_short(t_a)
        t_a.execute_partial_cover(SYMBOL, cover_price, T1)
        t_a.execute_cover(SYMBOL, cover_price, T1)

        t_b = _trader()
        _exec_short(t_b)
        t_b.execute_cover(SYMBOL, cover_price, T1)

        assert abs(t_a.account.cash - t_b.account.cash) < 1e-6

    def test_partial_plus_full_pnl_equals_single_full_pnl(self):
        """total_pnl must agree between partial-then-full and single-full paths."""
        cover_price = PRICE * 0.98

        t_a = _trader()
        _exec_short(t_a)
        t_a.execute_partial_cover(SYMBOL, cover_price, T1)
        t_a.execute_cover(SYMBOL, cover_price, T1)

        t_b = _trader()
        _exec_short(t_b)
        t_b.execute_cover(SYMBOL, cover_price, T1)

        assert abs(t_a.account.total_pnl - t_b.account.total_pnl) < 1e-6

    def test_partial_cover_at_loss_cash_correct(self):
        """Short covered at a higher price (loss trade): cash still consistent."""
        cover_price = PRICE * 1.02   # short went against us

        t_a = _trader()
        _exec_short(t_a)
        t_a.execute_partial_cover(SYMBOL, cover_price, T1)
        t_a.execute_cover(SYMBOL, cover_price, T1)

        t_b = _trader()
        _exec_short(t_b)
        t_b.execute_cover(SYMBOL, cover_price, T1)

        assert abs(t_a.account.cash - t_b.account.cash) < 1e-6
        assert abs(t_a.account.total_pnl - t_b.account.total_pnl) < 1e-6

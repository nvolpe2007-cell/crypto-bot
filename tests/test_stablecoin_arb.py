"""
Unit tests for arbitrage/stablecoin_arb.py

Covers:
- scan_exchange: profit calculation with per-leg fees, false-positive rejection
  at peg, real depeg detection above threshold, correct field values
- get_best_opportunity: empty list, best by profit_usd, max_age_seconds staleness filter
- Memory safety: opportunities deque stays bounded at maxlen=500
"""

import sys
import os
import pytest
import asyncio
from collections import deque
from datetime import datetime, timedelta
from unittest.mock import AsyncMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from arbitrage.stablecoin_arb import StablecoinArbBot, TriangleOpportunity

_TRIANGLE = ["USDC", "USDT", "DAI", "USDC"]


def _make_bot(**kwargs) -> StablecoinArbBot:
    defaults = dict(
        exchanges=["kraken"],
        min_profit_pct=0.1,
        trade_size_usd=500.0,
        fee_pct=0.0026,
    )
    defaults.update(kwargs)
    return StablecoinArbBot(**defaults)


def _make_opp(profit_usd: float = 1.0,
              seconds_ago: float = 0.0) -> TriangleOpportunity:
    ts = datetime.now() - timedelta(seconds=seconds_ago)
    return TriangleOpportunity(
        path=_TRIANGLE.copy(),
        start_amount=500.0,
        end_amount=500.0 + profit_usd,
        profit_pct=profit_usd / 500.0 * 100,
        profit_usd=profit_usd,
        exchange="kraken",
        timestamp=ts,
    )


# ── scan_exchange ─────────────────────────────────────────────────────────────

class TestScanExchange:
    @pytest.mark.asyncio
    async def test_at_peg_no_opportunity_with_fees(self):
        """All prices at 1.0 → after 3× 0.26% fees the round-trip loses money."""
        bot = _make_bot(fee_pct=0.0026, min_profit_pct=0.1)
        bot.get_triangle_prices = AsyncMock(return_value={
            "USDC_USDT": 1.0,
            "USDT_DAI":  1.0,
            "DAI_USDC":  1.0,
        })
        await bot.scan_exchange("kraken")
        assert len(bot.opportunities) == 0

    @pytest.mark.asyncio
    async def test_zero_fee_at_peg_below_threshold(self):
        """Zero fees + prices at 1.0 → exactly 0% profit → below min_profit_pct=0.1."""
        bot = _make_bot(fee_pct=0.0, min_profit_pct=0.1)
        bot.get_triangle_prices = AsyncMock(return_value={
            "USDC_USDT": 1.0,
            "USDT_DAI":  1.0,
            "DAI_USDC":  1.0,
        })
        await bot.scan_exchange("kraken")
        assert len(bot.opportunities) == 0

    @pytest.mark.asyncio
    async def test_depeg_sufficient_to_clear_fees(self):
        """Prices depeg enough to exceed 3× fee cost → opportunity recorded."""
        # 3 legs at 0.26% each ≈ 0.78% cost; need prices that yield > 0.88% gross
        bot = _make_bot(fee_pct=0.0026, min_profit_pct=0.1)
        # Prices that give ~1% gross → ~0.22% net after fees
        bot.get_triangle_prices = AsyncMock(return_value={
            "USDC_USDT": 1.004,
            "USDT_DAI":  1.004,
            "DAI_USDC":  1.002,
        })
        await bot.scan_exchange("kraken")
        assert len(bot.opportunities) == 1
        assert bot.opportunities[0].profit_pct > 0.1

    @pytest.mark.asyncio
    async def test_profit_calculation_exact_zero_fee(self):
        """Exact round-trip amount with zero fees matches arithmetic."""
        bot = _make_bot(fee_pct=0.0, min_profit_pct=0.0, trade_size_usd=1_000.0)
        p1, p2, p3 = 1.005, 1.002, 1.001
        bot.get_triangle_prices = AsyncMock(return_value={
            "USDC_USDT": p1,
            "USDT_DAI":  p2,
            "DAI_USDC":  p3,
        })
        await bot.scan_exchange("kraken")
        assert len(bot.opportunities) == 1
        opp = bot.opportunities[0]
        expected_end = 1_000.0 * p1 * p2 * p3
        assert abs(opp.end_amount - expected_end) < 1e-9
        assert abs(opp.start_amount - 1_000.0) < 1e-9

    @pytest.mark.asyncio
    async def test_fee_deduction_reduces_profit(self):
        """With fees, profit_pct is strictly lower than without fees at same prices."""
        prices = {"USDC_USDT": 1.005, "USDT_DAI": 1.003, "DAI_USDC": 1.002}

        bot_no_fee = _make_bot(fee_pct=0.0, min_profit_pct=0.0)
        bot_no_fee.get_triangle_prices = AsyncMock(return_value=prices)
        await bot_no_fee.scan_exchange("kraken")

        bot_with_fee = _make_bot(fee_pct=0.0026, min_profit_pct=0.0)
        bot_with_fee.get_triangle_prices = AsyncMock(return_value=prices)
        await bot_with_fee.scan_exchange("kraken")

        profit_no_fee   = bot_no_fee.opportunities[0].profit_pct
        profit_with_fee = bot_with_fee.opportunities[0].profit_pct
        assert profit_with_fee < profit_no_fee

    @pytest.mark.asyncio
    async def test_none_prices_skipped(self):
        """If get_triangle_prices returns None, scan returns without recording."""
        bot = _make_bot()
        bot.get_triangle_prices = AsyncMock(return_value=None)
        await bot.scan_exchange("kraken")
        assert len(bot.opportunities) == 0

    @pytest.mark.asyncio
    async def test_opportunity_fields_populated(self):
        """All TriangleOpportunity fields are set correctly."""
        bot = _make_bot(fee_pct=0.0, min_profit_pct=0.0, trade_size_usd=500.0)
        bot.get_triangle_prices = AsyncMock(return_value={
            "USDC_USDT": 1.01,
            "USDT_DAI":  1.0,
            "DAI_USDC":  1.0,
        })
        await bot.scan_exchange("kraken")
        assert len(bot.opportunities) == 1
        opp = bot.opportunities[0]
        assert opp.exchange == "kraken"
        assert opp.path == _TRIANGLE
        assert opp.start_amount == 500.0
        assert opp.profit_usd == pytest.approx(opp.end_amount - opp.start_amount)
        assert isinstance(opp.timestamp, datetime)

    @pytest.mark.asyncio
    async def test_exchanges_tagged_correctly(self):
        """Opportunity records which exchange it came from."""
        bot = _make_bot(fee_pct=0.0, min_profit_pct=0.0, exchanges=["coinbase"])
        bot.get_triangle_prices = AsyncMock(return_value={
            "USDC_USDT": 1.01,
            "USDT_DAI":  1.0,
            "DAI_USDC":  1.0,
        })
        await bot.scan_exchange("coinbase")
        assert bot.opportunities[0].exchange == "coinbase"


# ── get_best_opportunity ──────────────────────────────────────────────────────

class TestGetBestOpportunity:
    def test_returns_none_when_empty(self):
        bot = _make_bot()
        assert bot.get_best_opportunity() is None

    def test_returns_best_by_profit_usd(self):
        bot = _make_bot()
        bot.opportunities.append(_make_opp(profit_usd=1.0))
        bot.opportunities.append(_make_opp(profit_usd=5.0))
        bot.opportunities.append(_make_opp(profit_usd=2.5))
        best = bot.get_best_opportunity()
        assert best is not None
        assert best.profit_usd == pytest.approx(5.0)

    def test_ignores_stale_opportunities(self):
        """An opportunity older than max_age_seconds must not be returned."""
        bot = _make_bot()
        stale = _make_opp(profit_usd=10.0, seconds_ago=60.0)
        bot.opportunities.append(stale)
        assert bot.get_best_opportunity(max_age_seconds=10.0) is None

    def test_returns_recent_opportunity(self):
        """A fresh opportunity (0 seconds old) should always be returned."""
        bot = _make_bot()
        fresh = _make_opp(profit_usd=3.0, seconds_ago=0.0)
        bot.opportunities.append(fresh)
        result = bot.get_best_opportunity(max_age_seconds=10.0)
        assert result is fresh

    def test_mixed_ages_returns_best_recent_only(self):
        """Stale high-profit opportunity loses to a smaller recent one."""
        bot = _make_bot()
        stale_big  = _make_opp(profit_usd=100.0, seconds_ago=30.0)
        recent_small = _make_opp(profit_usd=1.0,   seconds_ago=0.0)
        bot.opportunities.append(stale_big)
        bot.opportunities.append(recent_small)
        best = bot.get_best_opportunity(max_age_seconds=10.0)
        assert best is recent_small

    def test_default_age_is_ten_seconds(self):
        """Default max_age_seconds of 10 filters entries older than 10 s."""
        bot = _make_bot()
        just_expired = _make_opp(profit_usd=5.0, seconds_ago=11.0)
        bot.opportunities.append(just_expired)
        assert bot.get_best_opportunity() is None  # default max_age=10


# ── memory safety ─────────────────────────────────────────────────────────────

class TestMemorySafety:
    @pytest.mark.asyncio
    async def test_opportunities_bounded_at_maxlen(self):
        """Adding more than maxlen entries keeps the deque at maxlen."""
        bot = _make_bot(fee_pct=0.0, min_profit_pct=0.0)
        bot.get_triangle_prices = AsyncMock(return_value={
            "USDC_USDT": 1.01,
            "USDT_DAI":  1.0,
            "DAI_USDC":  1.0,
        })
        for _ in range(600):
            await bot.scan_exchange("kraken")
        assert len(bot.opportunities) <= 500

    def test_opportunities_is_deque(self):
        """opportunities must be a deque (not a plain list) to have maxlen."""
        bot = _make_bot()
        assert isinstance(bot.opportunities, deque)

    def test_deque_has_maxlen(self):
        """The deque must have a finite maxlen set."""
        bot = _make_bot()
        assert bot.opportunities.maxlen is not None
        assert bot.opportunities.maxlen > 0

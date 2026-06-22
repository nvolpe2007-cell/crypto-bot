"""
Unit tests for arbitrage/dex_arb.py

Covers:
- get_top_opportunities: returns the N *best* (highest net_profit_usd)
  opportunities, in descending order — regression test for a sort/slice
  inversion bug that silently returned the worst N instead.
"""

import sys
import os
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from arbitrage.dex_arb import DEXArbitrageBot, ArbOpportunity


def _make_opp(net_profit_usd: float) -> ArbOpportunity:
    return ArbOpportunity(
        token_in="SOL",
        token_out="USDC",
        buy_dex="jupiter",
        sell_dex="raydium",
        buy_price=100.0,
        sell_price=101.0,
        spread_pct=1.0,
        profit_usd=net_profit_usd + 0.002,
        gas_cost_usd=0.002,
        net_profit_usd=net_profit_usd,
        timestamp=datetime.now(),
    )


def _make_bot(profits) -> DEXArbitrageBot:
    bot = DEXArbitrageBot()
    bot.opportunities = [_make_opp(p) for p in profits]
    return bot


class TestGetTopOpportunities:
    def test_returns_highest_profit_first(self):
        bot = _make_bot([1.0, 5.0, 3.0, 2.0, 4.0])
        top = bot.get_top_opportunities(limit=3)
        assert [o.net_profit_usd for o in top] == [5.0, 4.0, 3.0]

    def test_default_limit_five(self):
        bot = _make_bot([1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0])
        top = bot.get_top_opportunities()
        assert len(top) == 5
        assert [o.net_profit_usd for o in top] == [7.0, 6.0, 5.0, 4.0, 3.0]

    def test_limit_larger_than_available(self):
        bot = _make_bot([1.0, 2.0])
        top = bot.get_top_opportunities(limit=5)
        assert [o.net_profit_usd for o in top] == [2.0, 1.0]

    def test_empty_opportunities(self):
        bot = _make_bot([])
        assert bot.get_top_opportunities() == []

"""
Unit tests for src/strategy_advisor.py

Covers:
- StrategyAdvisor.start(): reloads its TradeJournal from disk on every loop
  iteration, so it picks up trades the trading loop wrote through a SEPARATE
  TradeJournal instance (bot.py constructs one TradeJournal() for the
  advisor and another for run_paper_trading_session/LiveTrader — they don't
  share in-memory state, only the file on disk).
"""

import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from src.strategy_advisor import StrategyAdvisor


def _make_advisor():
    journal = MagicMock()
    journal.records = []
    notifier = MagicMock()
    return StrategyAdvisor(notifier, journal), journal, notifier


class TestStrategyAdvisorReload:
    async def test_start_reloads_journal_before_first_check(self):
        advisor, journal, _ = _make_advisor()

        # First asyncio.sleep call is the 30s startup delay; the second is
        # the end of the first loop iteration — raise there to stop the
        # otherwise-infinite `while True` loop after exactly one pass.
        with patch("asyncio.sleep", AsyncMock(side_effect=[None, asyncio.CancelledError()])):
            with pytest.raises(asyncio.CancelledError):
                await advisor.start()

        journal.reload.assert_called_once()

    async def test_start_reloads_journal_on_every_iteration(self):
        advisor, journal, _ = _make_advisor()

        with patch(
            "asyncio.sleep",
            AsyncMock(side_effect=[None, None, None, asyncio.CancelledError()]),
        ):
            with pytest.raises(asyncio.CancelledError):
                await advisor.start()

        assert journal.reload.call_count == 3

    async def test_reload_exception_does_not_crash_loop(self):
        """A failed reload (e.g. transient disk hiccup) is swallowed by the
        loop's own exception handler, same as any other per-iteration error."""
        advisor, journal, _ = _make_advisor()
        journal.reload.side_effect = OSError("disk hiccup")

        with patch("asyncio.sleep", AsyncMock(side_effect=[None, asyncio.CancelledError()])):
            with pytest.raises(asyncio.CancelledError):
                await advisor.start()

        journal.reload.assert_called_once()

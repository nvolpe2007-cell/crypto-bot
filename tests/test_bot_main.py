"""
Unit tests for src/bot.py::main()

Regression coverage for a crash-isolation bug: main() used to gather its
top-level subsystems (bot, dashboard) as bare coroutines. An unhandled
exception in either one (e.g. a dashboard bug) would propagate through
asyncio.gather() and crash the entire process, taking the live/paper
trading loop down with it.

main() now wraps each subsystem in the existing `supervised()` auto-restart
helper (already used for the websocket feeds), so a crash in one subsystem
restarts only that subsystem and never propagates out of main().

Note: main() briefly also gathered a third subsystem, `_run_funding_scanner`
(added alongside this file in b1d1f80), removed a commit later in 8104e46
("Remove duplicate funding-scanner task from src/bot.py main()" — it built
its own FundingScanner + FundingArbPaperSim, bypassing FUNDING_ARB_ENABLED
and racing paper_trading.py's properly-gated multi-arm wiring on the same
state.json keys). That removal didn't update these tests to match, which
went undetected because src/bot.py's own main() isn't on the production
path (run_all_bots.py, the VPS entry point, calls ScalpingBot.start()
directly and never invokes this main()) — see CLAUDE.md's "How it actually
runs". Trimmed back to the two subsystems main() actually gathers today.
"""

import asyncio
from unittest.mock import AsyncMock

import src.bot as bot_mod

MINIMAL_CONFIG = {
    'trading': {'pairs': ['BTC/USD'], 'timeframe': '1m', 'mode': 'paper', 'initial_capital': 100},
    'risk': {},
    'strategy': {},
}


def _patch_common(monkeypatch):
    monkeypatch.setattr(asyncio, "sleep", AsyncMock(return_value=None))
    monkeypatch.setattr(bot_mod, "load_config", lambda *a, **kw: MINIMAL_CONFIG)
    monkeypatch.setattr(bot_mod, "create_notifier_from_env", lambda: None)
    monkeypatch.setattr(bot_mod.ScalpingBot, "start", AsyncMock(return_value=None))


class TestMainSubsystemIsolation:
    async def test_dashboard_crash_does_not_crash_main(self, monkeypatch):
        _patch_common(monkeypatch)
        crashy_dashboard = AsyncMock(side_effect=RuntimeError("dashboard boom"))
        monkeypatch.setattr(bot_mod, "run_dashboard", crashy_dashboard)

        await bot_mod.main()  # must not raise

        # supervised() retries on crash — confirms the dashboard ran under
        # supervision rather than being awaited bare.
        assert crashy_dashboard.await_count > 1

    async def test_bot_crash_does_not_crash_main(self, monkeypatch):
        _patch_common(monkeypatch)
        crashy_start = AsyncMock(side_effect=RuntimeError("bot boom"))
        bot_mod.ScalpingBot.start = crashy_start
        monkeypatch.setattr(bot_mod, "run_dashboard", AsyncMock(return_value=None))

        await bot_mod.main()  # must not raise

        assert crashy_start.await_count > 1

    async def test_clean_exit_of_all_returns_normally(self, monkeypatch):
        _patch_common(monkeypatch)
        monkeypatch.setattr(bot_mod, "run_dashboard", AsyncMock(return_value=None))

        await bot_mod.main()  # no crash anywhere -> single clean pass, no raise

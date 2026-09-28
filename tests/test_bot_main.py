"""
Unit tests for src/bot.py::main()

Regression coverage for a crash-isolation bug: main() used to gather the
three top-level subsystems (bot, dashboard, funding scanner) as bare
coroutines. An unhandled exception in any ONE of them (e.g. a dashboard bug)
would propagate through asyncio.gather() and crash the entire process,
taking the live/paper trading loop and funding scanner down with it.

main() now wraps each subsystem in the existing `supervised()` auto-restart
helper (already used for the websocket feeds), so a crash in one subsystem
restarts only that subsystem and never propagates out of main().

UPDATED 2026-09-08 — these tests had been RED in CI since 2026-08-09 and were
blocking every open PR. They patched `src.bot._run_funding_scanner`, which
commit 8104e46 ("Remove duplicate funding-scanner task from src/bot.py main()")
deliberately deleted: the scanner was running twice, and the copy that survives
is the one in src/paper_trading.py, still wrapped in `supervised()`. So main()
now gathers TWO subsystems, not three.

The crash-isolation guarantee for the funding scanner did not go away with the
duplicate — it moved. Deleting the test that guarded it would have quietly
dropped that coverage, so it is replaced below by one that checks the scanner is
still supervised where it now actually lives.
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

    async def test_main_no_longer_runs_the_funding_scanner(self, monkeypatch):
        """The duplicate was removed in 8104e46. If it ever comes back here it
        will be running twice again, which is the bug that commit fixed."""
        _patch_common(monkeypatch)
        monkeypatch.setattr(bot_mod, "run_dashboard", AsyncMock(return_value=None))

        assert not hasattr(bot_mod, "_run_funding_scanner"), (
            "src.bot grew a funding scanner again -- src/paper_trading.py already "
            "runs one, so this would scan twice (see commit 8104e46)"
        )
        await bot_mod.main()  # still must not raise

    async def test_bot_crash_does_not_crash_main(self, monkeypatch):
        _patch_common(monkeypatch)
        crashy_start = AsyncMock(side_effect=RuntimeError("bot boom"))
        bot_mod.ScalpingBot.start = crashy_start
        monkeypatch.setattr(bot_mod, "run_dashboard", AsyncMock(return_value=None))

        await bot_mod.main()  # must not raise

        assert crashy_start.await_count > 1

    async def test_clean_exit_of_both_subsystems_returns_normally(self, monkeypatch):
        _patch_common(monkeypatch)
        monkeypatch.setattr(bot_mod, "run_dashboard", AsyncMock(return_value=None))

        await bot_mod.main()  # no crash anywhere -> single clean pass, no raise


class TestFundingScannerSupervisionMoved:
    """The funding scanner's crash isolation moved, it did not disappear.

    `TestMainSubsystemIsolation` used to assert that a funding-scanner crash
    could not kill main(). Commit 8104e46 removed the duplicate scanner from
    src/bot.py, so that assertion no longer has a subject there -- but the
    guarantee still matters, because the surviving scanner in
    src/paper_trading.py is what actually runs on the VPS.

    These are source-level wiring checks rather than behavioural ones: exercising
    them for real means standing up the whole paper-trading loop. Stated plainly
    so nobody mistakes them for proof that supervision WORKS -- they prove only
    that it is still wired, which is the thing that silently regresses.
    """

    def _paper_source(self):
        from pathlib import Path
        return (Path(__file__).resolve().parent.parent
                / 'src' / 'paper_trading.py').read_text(encoding='utf-8')

    def test_funding_scanner_is_still_wrapped_in_supervised(self):
        src = self._paper_source()
        assert "supervised('funding_scanner'" in src, (
            "the surviving funding scanner is no longer under supervised(); a crash "
            "in it can now take the paper-trading loop down with it"
        )

    def test_every_funding_arm_is_supervised(self):
        """All four funding tasks share one failure domain -- if any is launched
        bare, its crash propagates to the others."""
        src = self._paper_source()
        for arm in ('funding_scanner', 'funding_arb_majors',
                    'funding_arb_kraken', 'funding_merge_state'):
            assert f"supervised('{arm}'" in src, f'{arm} is not supervised'

    def test_the_scanner_is_launched_exactly_once(self):
        """The bug 8104e46 fixed was running it TWICE. One call site only."""
        src = self._paper_source()
        assert src.count("supervised('funding_scanner'") == 1

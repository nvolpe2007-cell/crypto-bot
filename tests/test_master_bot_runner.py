"""
Unit tests for MasterBotRunner._check_critical_tasks() and MasterBotRunner._spawn().

_check_critical_tasks tests verify that the health-monitor correctly:
- ignores tasks that are still running
- ignores tasks that were cancelled (e.g. clean shutdown)
- raises RuntimeError (with original exc as __cause__) when a task crashed
- logs a warning (without raising) when a task exits cleanly unexpectedly
- sends a Telegram notification on crash and on unexpected clean exit
- swallows notifier failures so they never mask the real crash

_spawn tests verify that background tasks (dashboard, dex/stablecoin arb,
altperp, the signal-handler shutdown) are held by a strong reference so they
can't be silently garbage-collected mid-run — asyncio.create_task()'s return
value, if discarded, is only weakly referenced by the event loop.
"""

import asyncio
import pytest
from unittest.mock import AsyncMock, patch


# ---------------------------------------------------------------------------
# Minimal shim so we can import MasterBotRunner without pulling in the full
# dependency graph (ScalpingBot → exchange → ccxt, etc.).
# ---------------------------------------------------------------------------

import importlib
import sys
import types


def _ensure_module(name: str) -> None:
    """Register a minimal stub only if the real module can't be imported.

    Prefer the real module so that test_stablecoin_arb (and others) importing
    symbols like TriangleOpportunity from the same package don't get a blank
    stub stuck in sys.modules instead of the live module.
    """
    if name in sys.modules:
        return
    try:
        importlib.import_module(name)
    except (ImportError, ModuleNotFoundError):
        m = types.ModuleType(name)
        sys.modules[name] = m


# Stub heavy optional imports if not already present from conftest.py
for _mod in (
    "src.bot",
    "src.dashboard",
    "arbitrage.dex_arb",
    "arbitrage.stablecoin_arb",
):
    _ensure_module(_mod)

# Provide the symbols that run_all_bots.py imports from those stubs
if not hasattr(sys.modules["src.bot"], "ScalpingBot"):
    sys.modules["src.bot"].ScalpingBot = object          # type: ignore[attr-defined]
if not hasattr(sys.modules["src.dashboard"], "run_dashboard"):
    sys.modules["src.dashboard"].run_dashboard = AsyncMock()  # type: ignore[attr-defined]
if not hasattr(sys.modules["arbitrage.dex_arb"], "DEXArbitrageBot"):
    sys.modules["arbitrage.dex_arb"].DEXArbitrageBot = object  # type: ignore[attr-defined]
    sys.modules["arbitrage.dex_arb"].Chain = object            # type: ignore[attr-defined]
if not hasattr(sys.modules["arbitrage.stablecoin_arb"], "StablecoinArbBot"):
    sys.modules["arbitrage.stablecoin_arb"].StablecoinArbBot = object  # type: ignore[attr-defined]

# Now we can safely import the module under test
from run_all_bots import MasterBotRunner  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_notifier() -> AsyncMock:
    n = AsyncMock()
    n.send = AsyncMock(return_value=None)
    return n


def _make_runner() -> MasterBotRunner:
    return MasterBotRunner(config={})


async def _make_failed_task(exc: Exception) -> asyncio.Task:
    """Create an already-done task whose result is an exception."""
    async def _raise():
        raise exc
    t = asyncio.create_task(_raise())
    try:
        await t
    except Exception:
        pass
    return t


async def _make_clean_task() -> asyncio.Task:
    """Create an already-done task that returned cleanly."""
    t = asyncio.create_task(asyncio.sleep(0))
    await t
    return t


async def _make_cancelled_task() -> asyncio.Task:
    """Create an already-cancelled task."""
    t = asyncio.create_task(asyncio.sleep(100))
    t.cancel()
    try:
        await t
    except asyncio.CancelledError:
        pass
    return t


# ---------------------------------------------------------------------------
# Tests: still-running task
# ---------------------------------------------------------------------------

class TestStillRunning:
    async def test_running_task_not_raised(self):
        runner = _make_runner()
        notifier = _make_notifier()
        t = asyncio.create_task(asyncio.sleep(100), name="slow")
        try:
            await runner._check_critical_tasks([("slow", t)], notifier)
        finally:
            t.cancel()
            try:
                await t
            except asyncio.CancelledError:
                pass

    async def test_running_task_no_notification(self):
        runner = _make_runner()
        notifier = _make_notifier()
        t = asyncio.create_task(asyncio.sleep(100), name="slow")
        try:
            await runner._check_critical_tasks([("slow", t)], notifier)
            notifier.send.assert_not_awaited()
        finally:
            t.cancel()
            try:
                await t
            except asyncio.CancelledError:
                pass


# ---------------------------------------------------------------------------
# Tests: cancelled task
# ---------------------------------------------------------------------------

class TestCancelledTask:
    async def test_cancelled_task_does_not_raise(self):
        runner = _make_runner()
        notifier = _make_notifier()
        t = await _make_cancelled_task()
        await runner._check_critical_tasks([("bot", t)], notifier)  # must not raise

    async def test_cancelled_task_no_notification(self):
        runner = _make_runner()
        notifier = _make_notifier()
        t = await _make_cancelled_task()
        await runner._check_critical_tasks([("bot", t)], notifier)
        notifier.send.assert_not_awaited()


# ---------------------------------------------------------------------------
# Tests: crashed task
# ---------------------------------------------------------------------------

class TestCrashedTask:
    async def test_raises_runtime_error_on_crash(self):
        runner = _make_runner()
        notifier = _make_notifier()
        t = await _make_failed_task(ValueError("network gone"))
        with pytest.raises(RuntimeError):
            await runner._check_critical_tasks([("scalping_bot", t)], notifier)

    async def test_original_exc_is_chained(self):
        runner = _make_runner()
        notifier = _make_notifier()
        original = OSError("connect failed")
        t = await _make_failed_task(original)
        with pytest.raises(RuntimeError) as exc_info:
            await runner._check_critical_tasks([("scalping_bot", t)], notifier)
        assert exc_info.value.__cause__ is original

    async def test_task_name_in_error_message(self):
        runner = _make_runner()
        notifier = _make_notifier()
        t = await _make_failed_task(RuntimeError("boom"))
        with pytest.raises(RuntimeError, match="scalping_bot"):
            await runner._check_critical_tasks([("scalping_bot", t)], notifier)

    async def test_exc_type_in_error_message(self):
        runner = _make_runner()
        notifier = _make_notifier()
        t = await _make_failed_task(ConnectionError("lost"))
        with pytest.raises(RuntimeError, match="ConnectionError"):
            await runner._check_critical_tasks([("scalping_bot", t)], notifier)

    async def test_notifier_called_on_crash(self):
        runner = _make_runner()
        notifier = _make_notifier()
        t = await _make_failed_task(RuntimeError("boom"))
        with pytest.raises(RuntimeError):
            await runner._check_critical_tasks([("scalping_bot", t)], notifier)
        notifier.send.assert_awaited_once()

    async def test_notification_contains_task_name(self):
        runner = _make_runner()
        notifier = _make_notifier()
        t = await _make_failed_task(RuntimeError("boom"))
        with pytest.raises(RuntimeError):
            await runner._check_critical_tasks([("my_bot", t)], notifier)
        msg = notifier.send.call_args[0][0]
        assert "my_bot" in msg

    async def test_notifier_failure_does_not_mask_crash(self):
        """Telegram being down must not prevent the RuntimeError from propagating."""
        runner = _make_runner()
        notifier = _make_notifier()
        notifier.send = AsyncMock(side_effect=Exception("telegram down"))
        t = await _make_failed_task(RuntimeError("bot died"))
        with pytest.raises(RuntimeError):
            await runner._check_critical_tasks([("scalping_bot", t)], notifier)


# ---------------------------------------------------------------------------
# Tests: unexpected clean exit
# ---------------------------------------------------------------------------

class TestUnexpectedCleanExit:
    async def test_does_not_raise_on_clean_exit(self):
        runner = _make_runner()
        notifier = _make_notifier()
        t = await _make_clean_task()
        await runner._check_critical_tasks([("scalping_bot", t)], notifier)  # no raise

    async def test_notifier_called_on_clean_exit(self):
        runner = _make_runner()
        notifier = _make_notifier()
        t = await _make_clean_task()
        await runner._check_critical_tasks([("scalping_bot", t)], notifier)
        notifier.send.assert_awaited_once()

    async def test_task_name_in_clean_exit_notification(self):
        runner = _make_runner()
        notifier = _make_notifier()
        t = await _make_clean_task()
        await runner._check_critical_tasks([("my_bot", t)], notifier)
        msg = notifier.send.call_args[0][0]
        assert "my_bot" in msg

    async def test_notifier_failure_does_not_raise_on_clean_exit(self):
        runner = _make_runner()
        notifier = _make_notifier()
        notifier.send = AsyncMock(side_effect=Exception("telegram down"))
        t = await _make_clean_task()
        await runner._check_critical_tasks([("scalping_bot", t)], notifier)  # no raise


# ---------------------------------------------------------------------------
# Tests: multiple tasks
# ---------------------------------------------------------------------------

class TestMultipleTasks:
    async def test_healthy_task_before_crashed_task_still_raises(self):
        """Health check must inspect ALL tasks, not stop at the first healthy one."""
        runner = _make_runner()
        notifier = _make_notifier()
        healthy = asyncio.create_task(asyncio.sleep(100), name="healthy")
        crashed = await _make_failed_task(RuntimeError("boom"))
        try:
            with pytest.raises(RuntimeError):
                await runner._check_critical_tasks(
                    [("healthy_bot", healthy), ("crashed_bot", crashed)],
                    notifier,
                )
        finally:
            healthy.cancel()
            try:
                await healthy
            except asyncio.CancelledError:
                pass

    async def test_empty_task_list_does_not_raise(self):
        runner = _make_runner()
        notifier = _make_notifier()
        await runner._check_critical_tasks([], notifier)  # no raise


# ---------------------------------------------------------------------------
# Tests: _spawn() keeps a strong reference (no GC-mid-run hazard)
# ---------------------------------------------------------------------------

class TestSpawn:
    async def test_returns_a_task(self):
        runner = _make_runner()
        t = runner._spawn(asyncio.sleep(0), name="x")
        assert isinstance(t, asyncio.Task)
        await t

    async def test_task_tracked_while_running(self):
        runner = _make_runner()
        t = runner._spawn(asyncio.sleep(100), name="slow")
        try:
            assert t in runner._background_tasks
        finally:
            t.cancel()
            try:
                await t
            except asyncio.CancelledError:
                pass

    async def test_task_untracked_after_completion(self):
        """Confirms the done-callback removes the task once finished — proves
        the list isn't just an ever-growing leak of every task ever spawned."""
        runner = _make_runner()
        t = runner._spawn(asyncio.sleep(0), name="quick")
        await t
        # the done-callback runs via call_soon; yield once so it fires
        await asyncio.sleep(0)
        assert t not in runner._background_tasks

    async def test_task_survives_with_no_other_reference(self):
        """The core regression test: spawn a task via _spawn(), drop every
        local reference to it, force a GC pass, and confirm it still
        completes — proving the runner's internal list is what's keeping it
        alive, not the caller's variable."""
        import gc

        runner = _make_runner()
        done = asyncio.Event()

        async def _work():
            await asyncio.sleep(0.05)
            done.set()

        runner._spawn(_work(), name="orphan")
        # no local variable holds the task at all
        gc.collect()
        await asyncio.wait_for(done.wait(), timeout=2.0)
        assert done.is_set()

    async def test_multiple_spawned_tasks_all_tracked(self):
        runner = _make_runner()
        tasks = [runner._spawn(asyncio.sleep(100), name=f"t{i}") for i in range(3)]
        try:
            assert len(runner._background_tasks) == 3
            assert all(t in runner._background_tasks for t in tasks)
        finally:
            for t in tasks:
                t.cancel()
            for t in tasks:
                try:
                    await t
                except asyncio.CancelledError:
                    pass

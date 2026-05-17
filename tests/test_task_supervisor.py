"""
Unit tests for src/task_supervisor.py

Covers:
- supervised(): clean exit (no restart)
- supervised(): single crash → restart
- supervised(): crashes up to max_restarts → gives up cleanly
- supervised(): crash beyond max_restarts → gives up (doesn't restart)
- supervised(): CancelledError propagates immediately, is not swallowed
- supervised(): notifier called on each crash and on give-up
- supervised(): notifier failure is swallowed (best-effort)
- supervised(): exponential backoff delays
- supervised(): backoff is capped at 60 s
- _safe_notify(): no-op when notifier is None
- _safe_notify(): swallows notifier exceptions
"""

import asyncio
import pytest
from unittest.mock import AsyncMock, patch, call

from src.task_supervisor import supervised, _safe_notify


# ── fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    """Patch asyncio.sleep to a no-op so backoff doesn't slow tests."""
    monkeypatch.setattr(asyncio, "sleep", AsyncMock(return_value=None))


def _make_notifier():
    n = AsyncMock()
    n.send = AsyncMock(return_value=None)
    return n


# ── clean-exit behaviour ──────────────────────────────────────────────────────

class TestCleanExit:
    async def test_factory_called_once_on_clean_exit(self):
        factory = AsyncMock(return_value=None)
        await supervised("test", factory)
        factory.assert_awaited_once()

    async def test_no_sleep_on_clean_exit(self):
        factory = AsyncMock(return_value=None)
        await supervised("test", factory)
        asyncio.sleep.assert_not_awaited()

    async def test_no_notifier_call_on_clean_exit(self):
        notifier = _make_notifier()
        factory = AsyncMock(return_value=None)
        await supervised("test", factory, notifier=notifier)
        notifier.send.assert_not_awaited()


# ── single crash → restart ────────────────────────────────────────────────────

class TestSingleCrash:
    async def test_factory_called_twice_after_one_crash(self):
        factory = AsyncMock(side_effect=[RuntimeError("boom"), None])
        await supervised("test", factory)
        assert factory.await_count == 2

    async def test_sleep_called_once_after_one_crash(self):
        factory = AsyncMock(side_effect=[RuntimeError("boom"), None])
        await supervised("test", factory)
        asyncio.sleep.assert_awaited_once()

    async def test_backoff_delay_is_2s_for_first_crash(self):
        factory = AsyncMock(side_effect=[RuntimeError("boom"), None])
        await supervised("test", factory)
        asyncio.sleep.assert_awaited_once_with(2)

    async def test_notifier_called_once_on_single_crash(self):
        notifier = _make_notifier()
        factory = AsyncMock(side_effect=[RuntimeError("boom"), None])
        await supervised("test", factory, notifier=notifier)
        notifier.send.assert_awaited_once()

    async def test_notifier_message_contains_task_name(self):
        notifier = _make_notifier()
        factory = AsyncMock(side_effect=[RuntimeError("boom"), None])
        await supervised("my_task", factory, notifier=notifier)
        msg = notifier.send.call_args[0][0]
        assert "my_task" in msg

    async def test_notifier_message_contains_restart_count(self):
        notifier = _make_notifier()
        factory = AsyncMock(side_effect=[RuntimeError("boom"), None])
        await supervised("test", factory, notifier=notifier)
        msg = notifier.send.call_args[0][0]
        assert "#1" in msg


# ── multiple crashes ──────────────────────────────────────────────────────────

class TestMultipleCrashes:
    async def test_two_crashes_calls_factory_three_times(self):
        factory = AsyncMock(
            side_effect=[RuntimeError("a"), RuntimeError("b"), None]
        )
        await supervised("test", factory)
        assert factory.await_count == 3

    async def test_backoff_grows_exponentially(self):
        factory = AsyncMock(
            side_effect=[RuntimeError("a"), RuntimeError("b"), None]
        )
        await supervised("test", factory)
        calls = asyncio.sleep.call_args_list
        assert calls[0] == call(2)   # 2**1
        assert calls[1] == call(4)   # 2**2

    async def test_backoff_capped_at_60s(self):
        # 7 crashes: 2**7=128 → capped at 60
        errors = [RuntimeError("x")] * 7
        factory = AsyncMock(side_effect=[*errors, None])
        await supervised("test", factory, max_restarts=10)
        delays = [c[0][0] for c in asyncio.sleep.call_args_list]
        assert all(d <= 60 for d in delays)
        assert delays[-1] == 60

    async def test_notifier_called_for_each_crash(self):
        notifier = _make_notifier()
        factory = AsyncMock(
            side_effect=[RuntimeError("a"), RuntimeError("b"), None]
        )
        await supervised("test", factory, notifier=notifier)
        assert notifier.send.await_count == 2


# ── give-up after max_restarts ────────────────────────────────────────────────

class TestGiveUp:
    async def test_stops_after_max_restarts_exceeded(self):
        max_r = 3
        factory = AsyncMock(side_effect=RuntimeError("always fails"))
        await supervised("test", factory, max_restarts=max_r)
        # max_r crashes + 1 give-up crash = max_r+1 total calls
        assert factory.await_count == max_r + 1

    async def test_does_not_restart_after_give_up(self):
        factory = AsyncMock(side_effect=RuntimeError("fail"))
        await supervised("test", factory, max_restarts=2)
        assert factory.await_count == 3   # not infinite

    async def test_give_up_notifier_called_with_stop_message(self):
        notifier = _make_notifier()
        factory = AsyncMock(side_effect=RuntimeError("fail"))
        await supervised("my_task", factory, max_restarts=1, notifier=notifier)
        # First crash → restart warning; second crash → give-up message
        assert notifier.send.await_count == 2
        final_msg = notifier.send.call_args_list[-1][0][0]
        assert "giving up" in final_msg.lower()
        assert "my_task" in final_msg

    async def test_no_sleep_after_final_crash(self):
        factory = AsyncMock(side_effect=RuntimeError("fail"))
        await supervised("test", factory, max_restarts=1)
        # Only one sleep (before the second attempt); no sleep after give-up
        assert asyncio.sleep.await_count == 1


# ── CancelledError ────────────────────────────────────────────────────────────

class TestCancelledError:
    async def test_cancelled_error_propagates(self):
        factory = AsyncMock(side_effect=asyncio.CancelledError())
        with pytest.raises(asyncio.CancelledError):
            await supervised("test", factory)

    async def test_cancelled_error_not_retried(self):
        factory = AsyncMock(side_effect=asyncio.CancelledError())
        with pytest.raises(asyncio.CancelledError):
            await supervised("test", factory)
        factory.assert_awaited_once()

    async def test_no_sleep_on_cancelled_error(self):
        factory = AsyncMock(side_effect=asyncio.CancelledError())
        with pytest.raises(asyncio.CancelledError):
            await supervised("test", factory)
        asyncio.sleep.assert_not_awaited()

    async def test_no_notifier_call_on_cancelled_error(self):
        notifier = _make_notifier()
        factory = AsyncMock(side_effect=asyncio.CancelledError())
        with pytest.raises(asyncio.CancelledError):
            await supervised("test", factory, notifier=notifier)
        notifier.send.assert_not_awaited()


# ── notifier robustness ───────────────────────────────────────────────────────

class TestNotifierRobustness:
    async def test_notifier_failure_does_not_crash_supervisor(self):
        notifier = _make_notifier()
        notifier.send = AsyncMock(side_effect=Exception("telegram down"))
        factory = AsyncMock(side_effect=[RuntimeError("boom"), None])
        # Must not raise despite notifier failure
        await supervised("test", factory, notifier=notifier)
        assert factory.await_count == 2

    async def test_none_notifier_is_accepted(self):
        factory = AsyncMock(side_effect=[RuntimeError("boom"), None])
        await supervised("test", factory, notifier=None)
        assert factory.await_count == 2


# ── _safe_notify ──────────────────────────────────────────────────────────────

class TestSafeNotify:
    async def test_no_op_when_notifier_is_none(self):
        await _safe_notify(None, "hi")   # should not raise

    async def test_calls_notifier_send(self):
        notifier = _make_notifier()
        await _safe_notify(notifier, "test message")
        notifier.send.assert_awaited_once_with("test message")

    async def test_swallows_notifier_exception(self):
        notifier = _make_notifier()
        notifier.send = AsyncMock(side_effect=Exception("network error"))
        await _safe_notify(notifier, "msg")   # should not raise


# ── custom max_restarts ───────────────────────────────────────────────────────

class TestCustomMaxRestarts:
    async def test_max_restarts_zero_gives_up_after_one_crash(self):
        factory = AsyncMock(side_effect=[RuntimeError("fail"), None])
        await supervised("test", factory, max_restarts=0)
        # One crash exceeds max_restarts=0, so it gives up immediately
        factory.assert_awaited_once()

    async def test_max_restarts_one_allows_single_restart(self):
        factory = AsyncMock(side_effect=[RuntimeError("a"), RuntimeError("b"), None])
        await supervised("test", factory, max_restarts=1)
        # 2 crashes → gives up after 2nd; factory called exactly twice
        assert factory.await_count == 2

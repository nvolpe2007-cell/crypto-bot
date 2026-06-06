"""
Supervised task wrapper — auto-restarts async tasks on crash with
exponential backoff and optional Telegram notification.
"""
import asyncio
import logging
import time
from typing import Callable, Awaitable

logger = logging.getLogger(__name__)

# Registry of subsystem health states — readable by heartbeat logs.
# Values: "OK" | "DEGRADED(n/max)" | "DEAD"
_subsystem_health: dict = {}


def get_health() -> dict:
    """Return a snapshot of all supervised-task health states.

    Suitable for embedding in heartbeat log lines, e.g.:
        logger.info("[SUBSYSTEMS] %s", get_health())
    """
    return dict(_subsystem_health)


async def supervised(
    name: str,
    factory: Callable[[], Awaitable[None]],
    *,
    notifier=None,
    max_restarts: int = 5,
    recovery_window_secs: float = 300.0,
) -> None:
    """Run factory() under supervision, restarting it on crash.

    Stops when:
    - factory() returns normally (clean exit, not restarted)
    - asyncio.CancelledError propagates (re-raised, never swallowed)
    - crash count exceeds max_restarts

    Restart backoff: 2s, 4s, 8s, 16s, 32s, capped at 60s.

    If factory() runs for at least recovery_window_secs before crashing,
    the crash counter resets to 0.  This prevents a task that is stable
    for hours but occasionally flaps from being permanently killed by the
    same ceiling applied to a task that fails on every launch.

    Args:
        name: Human-readable task name used in log and alert messages.
        factory: Zero-arg callable returning an awaitable (e.g. ``obj.start``).
        notifier: Object with ``async send(msg: str)`` for Telegram alerts.
        max_restarts: Give up after this many *consecutive* crashes within
            a single recovery window.
        recovery_window_secs: Running longer than this without crashing resets
            the crash counter (default 300 s = 5 min).
    """
    restarts = 0
    _subsystem_health[name] = "OK"
    while True:
        start = time.monotonic()
        try:
            logger.info("[Supervisor] Starting '%s' (attempt %d)", name, restarts + 1)
            await factory()
            logger.info("[Supervisor] '%s' finished cleanly", name)
            _subsystem_health[name] = "OK"
            return
        except asyncio.CancelledError:
            logger.info("[Supervisor] '%s' cancelled — not restarting", name)
            _subsystem_health.pop(name, None)
            raise
        except Exception as exc:
            run_duration = time.monotonic() - start
            if run_duration >= recovery_window_secs:
                # Task ran stably long enough — treat this crash as the first
                # in a fresh window rather than accumulating toward give-up.
                restarts = 0
            restarts += 1
            logger.error(
                "[Supervisor] '%s' crashed (#%d/%d): %s",
                name, restarts, max_restarts, exc,
            )
            if restarts > max_restarts:
                _subsystem_health[name] = "DEAD"
                msg = f"⛔ Subsystem '{name}' crashed {restarts} times — giving up."
                logger.error(msg)
                await _safe_notify(notifier, msg)
                return

            _subsystem_health[name] = f"DEGRADED({restarts}/{max_restarts})"
            delay = min(2 ** restarts, 60)
            msg = (
                f"⚠️ Subsystem '{name}' crashed — "
                f"restart #{restarts}/{max_restarts} in {delay}s."
            )
            logger.warning(msg)
            await _safe_notify(notifier, msg)
            await asyncio.sleep(delay)


async def _safe_notify(notifier, msg: str) -> None:
    if notifier is None:
        return
    try:
        await notifier.send(msg)
    except Exception as exc:
        logger.debug("[Supervisor] Notification failed: %s", exc)

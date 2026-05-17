"""
Supervised task wrapper — auto-restarts async tasks on crash with
exponential backoff and optional Telegram notification.
"""
import asyncio
import logging
from typing import Callable, Awaitable

logger = logging.getLogger(__name__)


async def supervised(
    name: str,
    factory: Callable[[], Awaitable[None]],
    *,
    notifier=None,
    max_restarts: int = 5,
) -> None:
    """Run factory() under supervision, restarting it on crash.

    Stops when:
    - factory() returns normally (clean exit, not restarted)
    - asyncio.CancelledError propagates (re-raised, never swallowed)
    - crash count exceeds max_restarts

    Restart backoff: 2s, 4s, 8s, 16s, 32s, capped at 60s.

    Args:
        name: Human-readable task name used in log and alert messages.
        factory: Zero-arg callable returning an awaitable (e.g. ``obj.start``).
        notifier: Object with ``async send(msg: str)`` for Telegram alerts.
        max_restarts: Give up after this many consecutive crashes.
    """
    restarts = 0
    while True:
        try:
            logger.info("[Supervisor] Starting '%s' (attempt %d)", name, restarts + 1)
            await factory()
            logger.info("[Supervisor] '%s' finished cleanly", name)
            return
        except asyncio.CancelledError:
            logger.info("[Supervisor] '%s' cancelled — not restarting", name)
            raise
        except Exception as exc:
            restarts += 1
            logger.error(
                "[Supervisor] '%s' crashed (#%d/%d): %s",
                name, restarts, max_restarts, exc,
            )
            if restarts > max_restarts:
                msg = f"⛔ Subsystem '{name}' crashed {restarts} times — giving up."
                logger.error(msg)
                await _safe_notify(notifier, msg)
                return

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

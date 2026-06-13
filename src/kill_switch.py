"""
Master kill switch — one flag that halts NEW entries across every arm.

Two independent triggers (either engages the kill):
  - env  BOT_KILL_SWITCH in {1,true,yes,on}        (set at start / in .env)
  - a flag file  data/KILL_SWITCH                  (live-toggleable, no restart;
    a future Telegram command can touch / remove it, and the global funding
    drawdown guard engages it automatically)

The kill ONLY blocks new entries. Exits and position management continue — you
always want to be able to get OUT when killed. Every function is exception-safe;
is_killed() fails OPEN (returns False) so a filesystem glitch can never wedge the
whole bot into a halt.
"""
from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

KILL_FILE = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'data', 'KILL_SWITCH')

_TRUTHY = {"1", "true", "yes", "on"}


def is_killed() -> bool:
    """True if the master kill is engaged (env flag OR flag file present)."""
    try:
        if os.getenv("BOT_KILL_SWITCH", "").strip().lower() in _TRUTHY:
            return True
        return os.path.exists(KILL_FILE)
    except Exception:
        return False  # fail OPEN — never wedge trading on an fs/env glitch


def engage(reason: str = "") -> bool:
    """Engage the kill by writing the flag file. Returns True on success."""
    try:
        os.makedirs(os.path.dirname(KILL_FILE), exist_ok=True)
        with open(KILL_FILE, "w", encoding="utf-8") as f:
            f.write(reason or "engaged")
        logger.warning("[KILL] engaged: %s", reason or "(no reason)")
        return True
    except Exception as exc:
        logger.warning("[KILL] engage failed: %s", exc)
        return False


def release() -> bool:
    """Release the kill by removing the flag file. Returns True if now clear.

    Does NOT clear the BOT_KILL_SWITCH env trigger (that's owned by the
    environment) — reports whether the overall state is now live.
    """
    try:
        if os.path.exists(KILL_FILE):
            os.remove(KILL_FILE)
        logger.info("[KILL] flag file released")
        return not is_killed()
    except Exception as exc:
        logger.warning("[KILL] release failed: %s", exc)
        return False


def reason() -> str:
    """Human-readable current state for logs / status."""
    if os.getenv("BOT_KILL_SWITCH", "").strip().lower() in _TRUTHY:
        return "KILLED (env BOT_KILL_SWITCH)"
    try:
        if os.path.exists(KILL_FILE):
            with open(KILL_FILE, encoding="utf-8") as f:
                return f"KILLED (file: {f.read().strip() or 'engaged'})"
    except Exception:
        pass
    return "live"

"""
Daily Circuit Breaker — halts entries after N losing trades in a UTC day.

User preference: count-based, not %-based. Resets at 00:00 UTC daily.
State is persisted to data/daily_circuit.json so a restart mid-day
doesn't reset the count.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)

DATA_DIR  = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")
STATE_FILE = os.path.join(DATA_DIR, "daily_circuit.json")

# How many losing trades per UTC day before we halt new entries.
MAX_LOSSES_PER_DAY = int(os.getenv("MAX_LOSSES_PER_DAY", "2"))


@dataclass
class CircuitState:
    date_utc:    str = ""            # YYYY-MM-DD
    losses:      int = 0
    wins:        int = 0
    halted_at:   Optional[float] = None   # epoch when halt triggered
    last_loss_at: Optional[float] = None

    def is_today(self) -> bool:
        return self.date_utc == datetime.now(timezone.utc).strftime("%Y-%m-%d")


class DailyCircuitBreaker:
    def __init__(self, max_losses: int = MAX_LOSSES_PER_DAY):
        self.max_losses = max_losses
        self.state = self._load() or CircuitState(
            date_utc=datetime.now(timezone.utc).strftime("%Y-%m-%d"))
        if not self.state.is_today():
            self._reset_for_new_day()

    def _load(self) -> Optional[CircuitState]:
        try:
            with open(STATE_FILE, "r") as f:
                d = json.load(f)
            return CircuitState(**d)
        except FileNotFoundError:
            return None
        except Exception as e:
            # File exists but is unreadable/corrupt — most likely a crash mid-write
            # before the atomic-replace fix below. Starting fresh (0 losses) is the
            # existing fallback, but this case is NOT a normal "first run" and should
            # be loud: silently losing today's loss count is exactly the failure mode
            # this circuit breaker exists to prevent.
            logger.warning(f"[CIRCUIT] state file unreadable, starting fresh day: {e}")
            return None

    def _save(self):
        try:
            os.makedirs(DATA_DIR, exist_ok=True)
            tmp = STATE_FILE + ".tmp"
            with open(tmp, "w") as f:
                json.dump(asdict(self.state), f, indent=2)
            os.replace(tmp, STATE_FILE)
        except Exception as e:
            logger.warning(f"[CIRCUIT] save failed: {e}")

    def _reset_for_new_day(self):
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if self.state.date_utc and self.state.date_utc != today:
            logger.info(f"[CIRCUIT] new UTC day {today}, resetting (yesterday: "
                        f"{self.state.wins}W/{self.state.losses}L)")
        self.state = CircuitState(date_utc=today)
        self._save()

    def _roll_if_needed(self):
        if not self.state.is_today():
            self._reset_for_new_day()

    def can_enter(self) -> tuple[bool, str]:
        """Returns (allowed, reason). Call this before opening a new position."""
        self._roll_if_needed()
        if self.state.losses >= self.max_losses:
            mins_since = ((time.time() - (self.state.halted_at or 0)) / 60.0)
            return False, (f"daily circuit: {self.state.losses} losses today "
                           f"(limit {self.max_losses}); halt {mins_since:.0f}min ago")
        return True, ""

    def record_outcome(self, won: bool, pnl: float, symbol: str) -> tuple[bool, str]:
        """
        Call after every closed trade. Returns (just_halted, msg) — if
        this trade tipped us into halt, caller should send a Telegram
        alert.
        """
        self._roll_if_needed()
        if won:
            self.state.wins += 1
            self._save()
            return False, ""

        self.state.losses += 1
        self.state.last_loss_at = time.time()
        just_halted = (self.state.losses == self.max_losses and self.state.halted_at is None)
        if just_halted:
            self.state.halted_at = time.time()
        self._save()
        if just_halted:
            msg = (f"⛔ Daily circuit triggered — {self.state.losses} losses today "
                   f"(${pnl:+.2f} on {symbol}). No new entries until 00:00 UTC.")
            logger.warning(f"[CIRCUIT] {msg}")
            return True, msg
        return False, ""

    def status(self) -> dict:
        self._roll_if_needed()
        return {
            "date_utc": self.state.date_utc,
            "losses":   self.state.losses,
            "wins":     self.state.wins,
            "max_losses": self.max_losses,
            "halted":   self.state.losses >= self.max_losses,
        }

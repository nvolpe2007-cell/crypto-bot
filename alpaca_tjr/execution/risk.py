"""Position sizing and daily circuit breaker."""
from __future__ import annotations

import logging
import time as _time
from datetime import date, datetime
from typing import Optional

import pytz

logger = logging.getLogger(__name__)

ET = pytz.timezone("US/Eastern")


def size_position(
    equity: float,
    risk_pct: float,
    entry: float,
    stop: float,
) -> float:
    """Return fractional share quantity based on fixed-risk sizing.

    risk_dollars = equity * (risk_pct / 100)
    qty = risk_dollars / |entry - stop|
    """
    risk_dollars = equity * (risk_pct / 100.0)
    points_risk = abs(entry - stop)
    if points_risk < 1e-6:
        logger.warning("Points risk too small (%.6f) — returning 0", points_risk)
        return 0.0
    qty = risk_dollars / points_risk
    return max(round(qty, 4), 0.0001)


class DailyCircuit:
    """Halt trading when daily loss exceeds threshold or trade count is hit."""

    def __init__(
        self,
        max_daily_loss_pct: float = 3.0,
        max_trades_per_day: int = 6,
        max_open_positions: int = 3,
        cooldown_after_loss_sec: float = 900.0,
    ):
        self._max_loss_pct = max_daily_loss_pct
        self._max_trades = max_trades_per_day
        self._max_positions = max_open_positions
        self._cooldown = cooldown_after_loss_sec

        self._start_equity: Optional[float] = None
        self._trade_count: int = 0
        self._halted: bool = False
        self._last_loss_ts: float = 0.0
        self._open_positions: int = 0
        self._reset_date: Optional[date] = None

    def start_day(self, equity: float) -> None:
        today = datetime.now(ET).date()
        self._start_equity = equity
        self._trade_count = 0
        self._halted = False
        self._last_loss_ts = 0.0
        self._open_positions = 0
        self._reset_date = today
        logger.info("DailyCircuit reset — start equity %.2f", equity)

    def _auto_reset_if_new_day(self, equity: float) -> None:
        today = datetime.now(ET).date()
        if self._reset_date != today:
            self.start_day(equity)

    def ok(self, current_equity: float) -> tuple[bool, str]:
        """Return (allowed, reason). Call before placing any new entry."""
        self._auto_reset_if_new_day(current_equity)

        if self._halted:
            return False, "daily circuit halted"

        if self._start_equity:
            loss_pct = (self._start_equity - current_equity) / self._start_equity * 100
            if loss_pct >= self._max_daily_loss_pct:
                self._halted = True
                logger.warning("CIRCUIT HALT: daily loss %.2f%% >= %.2f%%",
                               loss_pct, self._max_daily_loss_pct)
                return False, f"daily loss limit {loss_pct:.1f}%"

        if self._trade_count >= self._max_trades:
            return False, f"max trades/day ({self._max_trades}) reached"

        if self._open_positions >= self._max_positions:
            return False, f"max open positions ({self._max_positions})"

        cooldown_remaining = self._cooldown - (_time.monotonic() - self._last_loss_ts)
        if self._last_loss_ts > 0 and cooldown_remaining > 0:
            return False, f"cooldown ({cooldown_remaining:.0f}s remaining)"

        return True, "ok"

    def on_entry(self) -> None:
        self._trade_count += 1
        self._open_positions += 1

    def on_exit(self, was_stop: bool = False) -> None:
        self._open_positions = max(0, self._open_positions - 1)
        if was_stop:
            self._last_loss_ts = _time.monotonic()

    @property
    def open_positions(self) -> int:
        return self._open_positions

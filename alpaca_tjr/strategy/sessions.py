"""Session window detection and pre-market range tracking."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, time, date
from typing import Optional

import pytz

logger = logging.getLogger(__name__)

ET = pytz.timezone("US/Eastern")

SESSION_WINDOWS: dict[str, tuple[time, time]] = {
    "premarket": (time(4, 0), time(9, 30)),
    "primary":   (time(9, 30), time(11, 30)),
    "dead":      (time(11, 30), time(13, 0)),
    "secondary": (time(13, 0), time(14, 30)),
    "eod":       (time(14, 30), time(16, 0)),
}

TRADEABLE = {"primary", "secondary"}


def current_session(now: Optional[datetime] = None) -> str:
    """Return the name of the current session based on ET clock."""
    if now is None:
        now = datetime.now(ET)
    elif now.tzinfo is None:
        now = ET.localize(now)
    else:
        now = now.astimezone(ET)

    t = now.time()
    for name, (start, end) in SESSION_WINDOWS.items():
        if start <= t < end:
            return name
    return "closed"


def is_tradeable(now: Optional[datetime] = None) -> bool:
    return current_session(now) in TRADEABLE


def is_premarket(now: Optional[datetime] = None) -> bool:
    return current_session(now) == "premarket"


def force_close_time(today: Optional[date] = None) -> datetime:
    """Return the EOD force-close datetime (15:45 ET) for a given date."""
    if today is None:
        today = datetime.now(ET).date()
    return ET.localize(datetime(today.year, today.month, today.day, 15, 45))


@dataclass
class PremarketRange:
    """Accumulates the pre-market high/low for the current trading day."""
    date: Optional[date] = None
    high: float = float("-inf")
    low: float = float("inf")
    _initialized: bool = field(default=False, repr=False)

    def reset(self, today: Optional[date] = None) -> None:
        self.date = today or datetime.now(ET).date()
        self.high = float("-inf")
        self.low = float("inf")
        self._initialized = False

    def update(self, high: float, low: float) -> None:
        self.high = max(self.high, high)
        self.low = min(self.low, low)
        self._initialized = True

    @property
    def valid(self) -> bool:
        return self._initialized and self.high > self.low

    @property
    def midpoint(self) -> float:
        return (self.high + self.low) / 2.0 if self.valid else float("nan")

    @property
    def range_pct(self) -> float:
        if not self.valid or self.low == 0:
            return 0.0
        return (self.high - self.low) / self.low

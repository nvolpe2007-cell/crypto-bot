"""
Funding-reset timing helpers. Bybit funding resets at 00:00 / 08:00 / 16:00 UTC.
"""

from datetime import datetime, timezone, timedelta
from typing import Optional

from . import config


def get_minutes_to_next_funding_reset(now: Optional[datetime] = None) -> int:
    """Whole minutes until the next funding reset (00/08/16 UTC)."""
    now = now or datetime.now(timezone.utc)
    resets = sorted(config.FUNDING_RESET_HOURS_UTC)
    today = now.date()
    candidates = [
        datetime(today.year, today.month, today.day, h, 0, 0, tzinfo=timezone.utc)
        for h in resets
    ]
    # add tomorrow's first reset so we always have a future one
    candidates.append(candidates[0] + timedelta(days=1))
    nxt = min(c for c in candidates if c > now)
    return int((nxt - now).total_seconds() // 60)


def minutes_since_last_funding_reset(now: Optional[datetime] = None) -> int:
    """Whole minutes since the most recent funding reset (00/08/16 UTC)."""
    now = now or datetime.now(timezone.utc)
    resets = sorted(config.FUNDING_RESET_HOURS_UTC)
    today = now.date()
    past = [
        datetime(today.year, today.month, today.day, h, tzinfo=timezone.utc)
        for h in resets
    ]
    past.insert(0, past[0] - timedelta(days=1))  # yesterday's last, as a floor
    last = max(c for c in past if c <= now)
    return int((now - last).total_seconds() // 60)


def in_post_funding_block(now: Optional[datetime] = None) -> bool:
    """True if we're within POST_FUNDING_BLOCK_MINS after a reset (block new shorts)."""
    return minutes_since_last_funding_reset(now) < config.POST_FUNDING_BLOCK_MINS


def in_pre_funding_window(now: Optional[datetime] = None) -> bool:
    """True if we're in the PRE_FUNDING_WINDOW_MINS before a reset (best short entry)."""
    return get_minutes_to_next_funding_reset(now) <= config.PRE_FUNDING_WINDOW_MINS


def _selftest():
    # 07:50 UTC → 10 min to 08:00 reset, in pre-window, not post-block
    t = datetime(2026, 5, 26, 7, 50, tzinfo=timezone.utc)
    assert get_minutes_to_next_funding_reset(t) == 10, get_minutes_to_next_funding_reset(t)
    assert in_pre_funding_window(t) is True
    assert in_post_funding_block(t) is False
    # 08:10 UTC → just after reset → post-block True
    t2 = datetime(2026, 5, 26, 8, 10, tzinfo=timezone.utc)
    assert in_post_funding_block(t2) is True
    assert minutes_since_last_funding_reset(t2) == 10
    # 23:30 → next reset is 00:00 tomorrow → 30 min
    t3 = datetime(2026, 5, 26, 23, 30, tzinfo=timezone.utc)
    assert get_minutes_to_next_funding_reset(t3) == 30, get_minutes_to_next_funding_reset(t3)
    print("time_utils selftest OK")


if __name__ == "__main__":
    _selftest()

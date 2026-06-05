"""
Tests for src/event_calendar.py — the high-impact-event entry blackout.
"""
from datetime import datetime, timedelta, timezone

from src.event_calendar import blackout_reason, load_events


_EVENTS = [("FOMC", datetime(2026, 6, 17, 18, 0, tzinfo=timezone.utc))]


def test_inside_window_before_event_is_blackout():
    now = datetime(2026, 6, 17, 12, 0, tzinfo=timezone.utc)   # 6h before
    r = blackout_reason(now, hours_before=8, hours_after=2, events=_EVENTS)
    assert r is not None and "FOMC" in r


def test_inside_window_after_event_is_blackout():
    now = datetime(2026, 6, 17, 19, 0, tzinfo=timezone.utc)   # 1h after
    assert blackout_reason(now, hours_before=8, hours_after=2, events=_EVENTS) is not None


def test_outside_window_is_clear():
    now = datetime(2026, 6, 17, 4, 0, tzinfo=timezone.utc)    # 14h before
    assert blackout_reason(now, hours_before=8, hours_after=2, events=_EVENTS) is None


def test_zero_hours_disables_veto():
    now = datetime(2026, 6, 17, 17, 0, tzinfo=timezone.utc)   # 1h before
    assert blackout_reason(now, hours_before=0, events=_EVENTS) is None


def test_naive_datetime_treated_as_utc():
    now = datetime(2026, 6, 17, 12, 0)                         # naive
    assert blackout_reason(now, hours_before=8, events=_EVENTS) is not None


def test_seed_calendar_loads_and_is_sorted():
    evs = load_events()
    assert len(evs) > 0
    times = [t for _, t in evs]
    assert times == sorted(times)

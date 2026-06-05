"""
Event calendar — high-impact macro events to blackout new entries around.

WHY: a swing position opened hours before a scheduled high-impact event (FOMC,
CPI) can gap straight through its stop on the release. A discretionary trader
simply doesn't open into that. This provides a simple, pluggable "are we inside
a blackout window?" check used as an ENTRY VETO only (it never forces exits).

DATA: events are read from data/event_calendar.json if present (so you can keep
it current without code changes), otherwise from the built-in SEED below. Each
event is {"name": str, "utc": ISO-8601}. The seed dates are the 2026 FOMC
announcement days and approximate monthly US CPI releases — VERIFY/UPDATE them;
treat the seed as a starting point, not gospel. Times are approximate UTC.

Blackout window: entries are vetoed if `now` is within BLACKOUT_HOURS_BEFORE
hours before any event (and a short BLACKOUT_HOURS_AFTER after). Tunable via
SWING_EVENT_BLACKOUT_HOURS (set 0 to disable the veto entirely).
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List, Optional, Tuple

_CALENDAR_FILE = Path("data/event_calendar.json")

# Hours before an event during which new entries are vetoed (0 disables).
BLACKOUT_HOURS_BEFORE = float(os.getenv("SWING_EVENT_BLACKOUT_HOURS", "8"))
# Short window after the event to let the initial spike settle before re-entering.
BLACKOUT_HOURS_AFTER = float(os.getenv("SWING_EVENT_BLACKOUT_HOURS_AFTER", "2"))

# Built-in seed — 2026 FOMC announcement days (~19:00 UTC ≈ 2pm ET) and approx
# monthly US CPI releases (~13:00 UTC ≈ 8:30am ET). VERIFY against the official
# Fed/BLS schedules and edit data/event_calendar.json to keep current.
SEED: List[dict] = [
    {"name": "FOMC", "utc": "2026-01-28T19:00:00+00:00"},
    {"name": "FOMC", "utc": "2026-03-18T18:00:00+00:00"},
    {"name": "FOMC", "utc": "2026-04-29T18:00:00+00:00"},
    {"name": "FOMC", "utc": "2026-06-17T18:00:00+00:00"},
    {"name": "FOMC", "utc": "2026-07-29T18:00:00+00:00"},
    {"name": "FOMC", "utc": "2026-09-16T18:00:00+00:00"},
    {"name": "FOMC", "utc": "2026-10-28T18:00:00+00:00"},
    {"name": "FOMC", "utc": "2026-12-09T19:00:00+00:00"},
    {"name": "CPI", "utc": "2026-01-13T13:30:00+00:00"},
    {"name": "CPI", "utc": "2026-02-11T13:30:00+00:00"},
    {"name": "CPI", "utc": "2026-03-11T12:30:00+00:00"},
    {"name": "CPI", "utc": "2026-04-10T12:30:00+00:00"},
    {"name": "CPI", "utc": "2026-05-12T12:30:00+00:00"},
    {"name": "CPI", "utc": "2026-06-10T12:30:00+00:00"},
    {"name": "CPI", "utc": "2026-07-14T12:30:00+00:00"},
    {"name": "CPI", "utc": "2026-08-12T12:30:00+00:00"},
    {"name": "CPI", "utc": "2026-09-11T12:30:00+00:00"},
    {"name": "CPI", "utc": "2026-10-13T12:30:00+00:00"},
    {"name": "CPI", "utc": "2026-11-12T13:30:00+00:00"},
    {"name": "CPI", "utc": "2026-12-10T13:30:00+00:00"},
]


def _parse(ev: dict) -> Optional[Tuple[str, datetime]]:
    try:
        dt = datetime.fromisoformat(ev["utc"])
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return ev.get("name", "event"), dt
    except (KeyError, ValueError, TypeError):
        return None


def load_events(path: Path = _CALENDAR_FILE) -> List[Tuple[str, datetime]]:
    """Events as (name, aware-UTC datetime), from the JSON file if present else
    the seed. Malformed entries are skipped."""
    raw = SEED
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, list) and data:
                raw = data
        except (json.JSONDecodeError, OSError):
            raw = SEED
    out = [p for ev in raw if (p := _parse(ev)) is not None]
    out.sort(key=lambda x: x[1])
    return out


def blackout_reason(now: datetime,
                    hours_before: float = BLACKOUT_HOURS_BEFORE,
                    hours_after: float = BLACKOUT_HOURS_AFTER,
                    events: Optional[List[Tuple[str, datetime]]] = None) -> Optional[str]:
    """If `now` is inside a blackout window, return a short reason string
    (e.g. "FOMC in 3.2h"); otherwise None. hours_before<=0 disables the veto."""
    if hours_before <= 0:
        return None
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    for name, dt in (events if events is not None else load_events()):
        start = dt - timedelta(hours=hours_before)
        end = dt + timedelta(hours=hours_after)
        if start <= now <= end:
            delta_h = (dt - now).total_seconds() / 3600.0
            when = f"in {delta_h:.1f}h" if delta_h >= 0 else f"{-delta_h:.1f}h ago"
            return f"{name} {when}"
    return None

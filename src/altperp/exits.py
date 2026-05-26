"""
Exit logic — scaled take-profits, hard stop, post-TP1 trailing stop, funding
stop (shorts), time stop (shorts), and OI-reversal stop (flush longs).

`check_exit(pos, ...)` returns at most one ExitAction per call (highest priority
first). The caller (position_manager) applies the close, updates remaining size,
and sets the tp/trail flags. The trailing anchor is updated here as a side effect
once trailing is active.

`pos` is duck-typed; it must expose: direction, entry_price, opened_at (datetime),
remaining_fraction, tp1_hit, tp2_hit, tp3_hit, trail_active, trail_anchor.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from . import config


@dataclass
class ExitAction:
    kind: str        # 'partial' | 'full'
    fraction: float  # fraction of the ORIGINAL position to close
    reason: str      # TP1/TP2/TP3/STOP/TRAIL/FUNDING_STOP/TIME_STOP/OI_REVERSAL


def check_exit(pos, price: float, funding_rate: Optional[float],
               oi_4hr_change: Optional[float], now: Optional[datetime] = None) -> Optional[ExitAction]:
    now = now or datetime.now(timezone.utc)
    if pos.direction == "short":
        return _check_short(pos, price, funding_rate, now)
    return _check_long(pos, price, oi_4hr_change, now)


def _check_short(pos, price, funding_rate, now) -> Optional[ExitAction]:
    entry = pos.entry_price
    profit = (entry - price) / entry          # +ve = in our favor
    rem = pos.remaining_fraction

    if pos.trail_active:
        pos.trail_anchor = price if pos.trail_anchor is None else min(pos.trail_anchor, price)

    # 1. Funding stop — crowd unwound before us, thesis dead.
    if funding_rate is not None and funding_rate < config.FUNDING_EXIT_THRESHOLD:
        return ExitAction("full", rem, "FUNDING_STOP")

    # 2. Risk stop: trailing (after TP1) takes over from the hard stop.
    if pos.trail_active and pos.trail_anchor is not None:
        if price >= pos.trail_anchor * (1 + config.SHORT_TRAIL_PCT):
            return ExitAction("full", rem, "TRAIL")
    else:
        if price >= entry * (1 + config.SHORT_STOP_PCT):
            return ExitAction("full", rem, "STOP")

    # 3. Take-profits (one per call, deepest first).
    if not pos.tp3_hit and profit >= config.SHORT_TP3_PCT:
        return ExitAction("full", rem, "TP3")
    if not pos.tp2_hit and profit >= config.SHORT_TP2_PCT:
        return ExitAction("partial", config.SHORT_TP2_CLOSE_PCT, "TP2")
    if not pos.tp1_hit and profit >= config.SHORT_TP1_PCT:
        return ExitAction("partial", config.SHORT_TP1_CLOSE_PCT, "TP1")

    # 4. Time stop — thesis expired without movement.
    age_h = (now - pos.opened_at).total_seconds() / 3600.0
    if age_h >= config.TIME_STOP_HOURS and profit < config.TIME_STOP_MIN_PROFIT_PCT:
        return ExitAction("full", rem, "TIME_STOP")
    return None


def _check_long(pos, price, oi_4hr_change, now) -> Optional[ExitAction]:
    entry = pos.entry_price
    profit = (price - entry) / entry
    rem = pos.remaining_fraction

    if pos.trail_active:
        pos.trail_anchor = price if pos.trail_anchor is None else max(pos.trail_anchor, price)

    # 1. OI reversal — new longs piling back in; the flush bounce is over.
    if oi_4hr_change is not None and oi_4hr_change >= config.LONG_OI_REVERSAL_PCT:
        return ExitAction("full", rem, "OI_REVERSAL")

    # 2. Risk stop: trailing (after TP) takes over from the hard stop.
    if pos.trail_active and pos.trail_anchor is not None:
        if price <= pos.trail_anchor * (1 - config.LONG_TRAIL_PCT):
            return ExitAction("full", rem, "TRAIL")
    else:
        if price <= entry * (1 - config.LONG_STOP_PCT):
            return ExitAction("full", rem, "STOP")

    # 3. Single scaled take-profit (close 70%, trail the rest).
    if not pos.tp1_hit and profit >= config.LONG_TP_PCT:
        return ExitAction("partial", config.LONG_TP_CLOSE_PCT, "TP1")
    return None


def _selftest():
    from types import SimpleNamespace
    t0 = datetime(2026, 5, 26, 0, 0, tzinfo=timezone.utc)

    def short_pos(**kw):
        d = dict(direction="short", entry_price=100.0, opened_at=t0,
                 remaining_fraction=1.0, tp1_hit=False, tp2_hit=False,
                 tp3_hit=False, trail_active=False, trail_anchor=None)
        d.update(kw)
        return SimpleNamespace(**d)

    # Hard stop: price +2% on a short → STOP full
    a = check_exit(short_pos(), 102.0, 0.0006, 0.1, t0)
    assert a and a.reason == "STOP" and a.kind == "full", a
    # TP1: price -1.5% → partial 0.40
    a = check_exit(short_pos(), 98.5, 0.0006, 0.1, t0)
    assert a and a.reason == "TP1" and abs(a.fraction - 0.40) < 1e-9, a
    # TP3: price -5% → full close remaining
    a = check_exit(short_pos(remaining_fraction=0.25, tp1_hit=True, tp2_hit=True), 95.0, 0.0006, 0.1, t0)
    assert a and a.reason == "TP3", a
    # Funding stop: funding collapsed below threshold → full
    a = check_exit(short_pos(), 99.5, 0.0001, 0.1, t0)
    assert a and a.reason == "FUNDING_STOP", a
    # Trailing: after TP1, anchor at 98, price bounces to 99 (>98×1.01=98.98) → TRAIL
    sp = short_pos(remaining_fraction=0.6, tp1_hit=True, trail_active=True, trail_anchor=98.0)
    a = check_exit(sp, 99.0, 0.0006, 0.1, t0)
    assert a and a.reason == "TRAIL", a
    # Time stop: 9h open, ~flat → TIME_STOP
    a = check_exit(short_pos(), 99.9, 0.0006, 0.1, datetime(2026, 5, 26, 9, 0, tzinfo=timezone.utc))
    assert a and a.reason == "TIME_STOP", a

    def long_pos(**kw):
        d = dict(direction="long", entry_price=100.0, opened_at=t0,
                 remaining_fraction=1.0, tp1_hit=False, tp2_hit=False,
                 tp3_hit=False, trail_active=False, trail_anchor=None)
        d.update(kw)
        return SimpleNamespace(**d)

    # Long TP: +2.5% → partial 0.70
    a = check_exit(long_pos(), 102.5, None, 0.0, t0)
    assert a and a.reason == "TP1" and abs(a.fraction - 0.70) < 1e-9, a
    # Long hard stop: -2% → STOP
    a = check_exit(long_pos(), 98.0, None, 0.0, t0)
    assert a and a.reason == "STOP", a
    # OI reversal: OI rebuilt +10% → full close
    a = check_exit(long_pos(), 101.0, None, 0.10, t0)
    assert a and a.reason == "OI_REVERSAL", a
    print("exits selftest OK")


if __name__ == "__main__":
    _selftest()

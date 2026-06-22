"""
Opening-Range Breakout (ORB) — a classic, defensible *intraday momentum* setup.

WHY ORB (and not a sub-minute scalper): one high-conviction setup per symbol per
day — it survives the cost wall and the PDT trade-count limit, and it's the
intraday edge with the most credible published evidence (5–30 min ORB on liquid
US names). It is NOT a money-printer; whether it has an edge on YOUR data + costs
is decided by metrics.py's pre-registered bar, not by hope.

Rules (long shown; short is the mirror, enabled by direction='both'):
  • Opening range = the high/low of the first `or_minutes` of the session.
  • ENTER long when price breaks ABOVE the OR high after the OR window closes
    (a stop-buy at the level; a gap-through fills at the bar open). One entry/day.
  • STOP = OR low (gap-aware fill). TARGET = entry + target_r × (entry − stop),
    or None → ride to the close.
  • Always FLAT by session end (intraday — no overnight risk, no PDT overnight).
No look-ahead: entries only on bars AFTER the OR window; exits only on bars at/
after entry. All decisions use that bar's OHLC, never future bars.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import time
from typing import List, Optional

import pandas as pd


@dataclass
class ORBConfig:
    or_minutes: int = 15            # opening-range length
    direction: str = "long"         # 'long' | 'short' | 'both'
    target_r: Optional[float] = 2.0  # R-multiple target; None → exit at close
    session_start: time = time(9, 30)
    session_end: time = time(16, 0)
    entry_cutoff: time = time(15, 30)  # no NEW entries after this (time to manage/exit)
    cost_bps_per_side: float = 2.0   # spread + slippage per side (commission ~0 on US stocks)

    @property
    def round_trip_cost_frac(self) -> float:
        return 2.0 * self.cost_bps_per_side / 1e4


@dataclass
class Trade:
    symbol: str
    date: str
    side: str               # 'long' | 'short'
    entry_time: str
    entry_px: float
    exit_time: str
    exit_px: float
    reason: str             # 'stop' | 'target' | 'eod'
    gross_ret: float        # side-adjusted, fraction (profit > 0)
    cost_frac: float
    net_ret: float          # gross_ret − round-trip cost


def opening_range(day: pd.DataFrame, cfg: ORBConfig):
    """(or_high, or_low) from the first `or_minutes` of the session, or (None,None)
    if there aren't enough bars in the window."""
    t = day.index.time
    start = cfg.session_start
    end_min = start.hour * 60 + start.minute + cfg.or_minutes
    in_or = [start <= ti and (ti.hour * 60 + ti.minute) < end_min for ti in t]
    window = day[in_or]
    if window.empty:
        return None, None
    return float(window["high"].max()), float(window["low"].min())


def _enter_side(bar, or_high, or_low, direction):
    """Which side (if any) breaks out on this bar, and the fill price (gap-aware)."""
    if direction in ("long", "both") and bar["high"] >= or_high:
        return "long", max(or_high, float(bar["open"]))
    if direction in ("short", "both") and bar["low"] <= or_low:
        return "short", min(or_low, float(bar["open"]))
    return None, None


def simulate_day(day: pd.DataFrame, cfg: ORBConfig, symbol: str = "?") -> Optional[Trade]:
    """At most ONE ORB trade for a single session's bars (ascending). Returns the
    Trade (with gross + net return) or None if no breakout triggered."""
    day = day.sort_index()
    or_high, or_low = opening_range(day, cfg)
    if or_high is None or or_high <= or_low:
        return None
    or_end_min = cfg.session_start.hour * 60 + cfg.session_start.minute + cfg.or_minutes

    # bars eligible for ENTRY: after the OR window, on/before the entry cutoff
    for ts, bar in day.iterrows():
        ti = ts.time()
        if (ti.hour * 60 + ti.minute) < or_end_min:
            continue                      # still inside the opening range
        if ti > cfg.entry_cutoff or ti >= cfg.session_end:
            break                         # too late to start a new trade
        side, entry_px = _enter_side(bar, or_high, or_low, cfg.direction)
        if side is None:
            continue
        stop = or_low if side == "long" else or_high
        if cfg.target_r is not None:
            risk = abs(entry_px - stop)
            target = entry_px + cfg.target_r * risk * (1 if side == "long" else -1)
        else:
            target = None
        return _manage(day.loc[ts:], bar.name, side, entry_px, stop, target, cfg, symbol)
    return None


def _manage(rest: pd.DataFrame, entry_ts, side, entry_px, stop, target, cfg, symbol) -> Trade:
    """Walk bars from entry to resolution: stop, target, or forced EOD flat.
    Gap-aware fills (a gap through the stop fills at the worse open)."""
    exit_px = exit_time = reason = None
    bars = rest.iloc[1:] if len(rest) > 1 else rest.iloc[0:0]  # bars AFTER entry bar
    for ts, bar in bars.iterrows():
        if ts.time() >= cfg.session_end:
            break
        lo, hi, op = float(bar["low"]), float(bar["high"]), float(bar["open"])
        if side == "long":
            if lo <= stop:                       # stop (market) — gap fills at open
                exit_px, reason = min(stop, op), "stop"
            elif target is not None and hi >= target:
                exit_px, reason = target, "target"   # limit fills at the level
        else:
            if hi >= stop:
                exit_px, reason = max(stop, op), "stop"
            elif target is not None and lo <= target:
                exit_px, reason = target, "target"
        if exit_px is not None:
            exit_time = ts
            break
    if exit_px is None:                          # EOD flat at the last bar's close
        last = rest.iloc[-1]
        exit_px, exit_time, reason = float(last["close"]), rest.index[-1], "eod"

    gross = (exit_px - entry_px) / entry_px * (1 if side == "long" else -1)
    net = gross - cfg.round_trip_cost_frac
    return Trade(symbol=symbol, date=str(entry_ts.date()), side=side,
                 entry_time=str(entry_ts), entry_px=round(entry_px, 4),
                 exit_time=str(exit_time), exit_px=round(exit_px, 4), reason=reason,
                 gross_ret=round(gross, 6), cost_frac=cfg.round_trip_cost_frac,
                 net_ret=round(net, 6))

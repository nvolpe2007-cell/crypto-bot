"""Swing high/low detection via N-bar confirmation."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import List, Optional

import pandas as pd

logger = logging.getLogger(__name__)


@dataclass
class SwingPoint:
    kind: str          # "high" | "low"
    price: float
    bar_index: int     # positional index in the DataFrame at detection time
    timestamp: datetime


def find_swings(bars: pd.DataFrame, n: int = 3) -> List[SwingPoint]:
    """Return confirmed swing highs/lows in the given OHLCV bars.

    A swing high at index i is confirmed when bars[i].high is strictly
    greater than highs in [i-n … i-1] AND [i+1 … i+n].
    A swing low is the mirror image with lows.

    The last n bars cannot be confirmed (not enough right-side context).
    """
    if len(bars) < 2 * n + 1:
        return []

    swings: List[SwingPoint] = []
    highs = bars["high"].values
    lows = bars["low"].values
    timestamps = bars.index

    for i in range(n, len(bars) - n):
        window_h = list(highs[i - n: i]) + list(highs[i + 1: i + n + 1])
        if highs[i] > max(window_h):
            swings.append(SwingPoint(
                kind="high",
                price=float(highs[i]),
                bar_index=i,
                timestamp=timestamps[i],
            ))

        window_l = list(lows[i - n: i]) + list(lows[i + 1: i + n + 1])
        if lows[i] < min(window_l):
            swings.append(SwingPoint(
                kind="low",
                price=float(lows[i]),
                bar_index=i,
                timestamp=timestamps[i],
            ))

    return swings


def recent_swing_high(swings: List[SwingPoint]) -> Optional[float]:
    highs = [s.price for s in swings if s.kind == "high"]
    return max(highs) if highs else None


def recent_swing_low(swings: List[SwingPoint]) -> Optional[float]:
    lows = [s.price for s in swings if s.kind == "low"]
    return min(lows) if lows else None


def last_n_swings(swings: List[SwingPoint], n: int) -> List[SwingPoint]:
    return swings[-n:] if swings else []

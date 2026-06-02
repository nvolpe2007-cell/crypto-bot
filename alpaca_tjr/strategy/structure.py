"""Break of Structure (BOS) detection after a liquidity sweep."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import List, Optional

import pandas as pd

from .sweep import Sweep
from .swing_points import SwingPoint

logger = logging.getLogger(__name__)


@dataclass
class BOS:
    direction: str       # "bullish" | "bearish" (matches sweep direction)
    broken_level: float  # the swing point that was broken
    impulse_open: float
    impulse_close: float
    impulse_high: float
    impulse_low: float
    bar_index: int
    timestamp: datetime

    @property
    def body_ratio(self) -> float:
        body = abs(self.impulse_close - self.impulse_open)
        rng = self.impulse_high - self.impulse_low
        return body / rng if rng > 0 else 0.0


def _is_impulse(bar: pd.Series, body_ratio_min: float = 0.5) -> bool:
    body = abs(bar["close"] - bar["open"])
    rng = bar["high"] - bar["low"]
    return rng > 0 and (body / rng) >= body_ratio_min


def detect_bos(
    bars: pd.DataFrame,
    sweep: Sweep,
    swing_points: List[SwingPoint],
    lookback: int = 10,
    body_ratio_min: float = 0.5,
) -> Optional[BOS]:
    """Look for a BOS impulse candle in the `lookback` bars AFTER the sweep.

    Bullish BOS: an impulse candle (body/range >= body_ratio_min) whose close
        exceeds the most recent confirmed swing high.
    Bearish BOS: an impulse candle whose close falls below the most recent
        confirmed swing low.

    The search starts from the bar immediately following the sweep candle.
    """
    if not swing_points:
        return None

    start_idx = sweep.bar_index + 1
    search_slice = bars.iloc[start_idx: start_idx + lookback]

    if search_slice.empty:
        return None

    if sweep.direction == "bullish":
        highs = [s.price for s in swing_points if s.kind == "high"]
        if not highs:
            return None
        target_level = max(highs)  # must close above the most recent swing high

        for i, (ts, bar) in enumerate(search_slice.iterrows()):
            if _is_impulse(bar, body_ratio_min) and bar["close"] > target_level:
                logger.info(
                    "Bullish BOS at %s: close=%.4f > swing_high=%.4f body_ratio=%.2f",
                    ts, bar["close"], target_level,
                    abs(bar["close"] - bar["open"]) / max(bar["high"] - bar["low"], 1e-9),
                )
                return BOS(
                    direction="bullish",
                    broken_level=target_level,
                    impulse_open=float(bar["open"]),
                    impulse_close=float(bar["close"]),
                    impulse_high=float(bar["high"]),
                    impulse_low=float(bar["low"]),
                    bar_index=start_idx + i,
                    timestamp=ts,
                )

    else:  # bearish
        lows = [s.price for s in swing_points if s.kind == "low"]
        if not lows:
            return None
        target_level = min(lows)  # must close below the most recent swing low

        for i, (ts, bar) in enumerate(search_slice.iterrows()):
            if _is_impulse(bar, body_ratio_min) and bar["close"] < target_level:
                logger.info(
                    "Bearish BOS at %s: close=%.4f < swing_low=%.4f body_ratio=%.2f",
                    ts, bar["close"], target_level,
                    abs(bar["close"] - bar["open"]) / max(bar["high"] - bar["low"], 1e-9),
                )
                return BOS(
                    direction="bearish",
                    broken_level=target_level,
                    impulse_open=float(bar["open"]),
                    impulse_close=float(bar["close"]),
                    impulse_high=float(bar["high"]),
                    impulse_low=float(bar["low"]),
                    bar_index=start_idx + i,
                    timestamp=ts,
                )

    return None

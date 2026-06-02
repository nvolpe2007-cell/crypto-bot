"""Liquidity sweep detection: wick beyond a key level, body closes back inside."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

import pandas as pd

logger = logging.getLogger(__name__)


@dataclass
class Sweep:
    direction: str      # "bullish" (swept lows → expect up) | "bearish" (swept highs → expect down)
    level: float        # the key level that was swept
    level_name: str     # "pm_low", "pm_high", "pdh", "pdl", "swing_high", "swing_low"
    sweep_price: float  # the wick extreme (low for bullish, high for bearish)
    close_price: float  # where the sweeping candle closed
    bar_index: int      # positional index of the sweeping candle
    timestamp: datetime


def detect_sweep(
    bars: pd.DataFrame,
    level: float,
    level_name: str,
    lookback: int = 10,
    tolerance: float = 0.0002,  # 0.02% — level must be "meaningfully" pierced
) -> Optional[Sweep]:
    """Scan the last `lookback` bars for a sweep of `level`.

    Bullish sweep: candle wick goes below the level AND candle body closes above it.
    Bearish sweep: candle wick goes above the level AND candle body closes below it.

    Returns the most recent sweep found, or None.
    """
    if len(bars) < 2:
        return None

    window = bars.tail(lookback)

    for i in range(len(window) - 1, -1, -1):
        bar = window.iloc[i]
        bar_idx = len(bars) - len(window) + i

        # Bullish sweep: wick dips below level, close recovers above
        wick_below = bar["low"] < level * (1 - tolerance)
        close_above = bar["close"] > level
        if wick_below and close_above:
            logger.debug(
                "Bullish sweep of %s (%.4f) at bar %d: low=%.4f close=%.4f",
                level_name, level, bar_idx, bar["low"], bar["close"],
            )
            return Sweep(
                direction="bullish",
                level=level,
                level_name=level_name,
                sweep_price=float(bar["low"]),
                close_price=float(bar["close"]),
                bar_index=bar_idx,
                timestamp=window.index[i],
            )

        # Bearish sweep: wick pokes above level, close falls back below
        wick_above = bar["high"] > level * (1 + tolerance)
        close_below = bar["close"] < level
        if wick_above and close_below:
            logger.debug(
                "Bearish sweep of %s (%.4f) at bar %d: high=%.4f close=%.4f",
                level_name, level, bar_idx, bar["high"], bar["close"],
            )
            return Sweep(
                direction="bearish",
                level=level,
                level_name=level_name,
                sweep_price=float(bar["high"]),
                close_price=float(bar["close"]),
                bar_index=bar_idx,
                timestamp=window.index[i],
            )

    return None


def scan_all_levels(
    bars: pd.DataFrame,
    levels: dict[str, float],
    lookback: int = 10,
) -> Optional[Sweep]:
    """Check all key levels and return the most recent sweep found."""
    best: Optional[Sweep] = None
    for name, value in levels.items():
        if value <= 0:
            continue
        sweep = detect_sweep(bars, value, name, lookback)
        if sweep is not None:
            if best is None or sweep.bar_index > best.bar_index:
                best = sweep
    return best

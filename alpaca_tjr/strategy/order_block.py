"""Order Block detection — the last opposing candle before a BOS impulse."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

import pandas as pd

from .structure import BOS

logger = logging.getLogger(__name__)


@dataclass
class OrderBlock:
    kind: str        # "bullish" | "bearish" (trade direction, NOT candle direction)
    high: float
    low: float
    open_: float
    close_: float
    bar_index: int
    timestamp: datetime

    @property
    def midpoint(self) -> float:
        return (self.high + self.low) / 2.0

    @property
    def entry_zone_top(self) -> float:
        return self.high

    @property
    def entry_zone_bottom(self) -> float:
        return self.midpoint

    def price_in_zone(self, price: float, tolerance: float = 0.001) -> bool:
        """Price is in the OB entry zone (50%–100% retrace into the block)."""
        lower = self.entry_zone_bottom * (1 - tolerance)
        upper = self.entry_zone_top * (1 + tolerance)
        return lower <= price <= upper

    def approaching(self, price: float, distance_pct: float = 0.002) -> bool:
        """Price is within distance_pct of the near edge of the OB."""
        if self.kind == "bullish":
            return abs(price - self.entry_zone_top) / max(self.entry_zone_top, 1e-9) <= distance_pct
        else:
            return abs(price - self.entry_zone_bottom) / max(self.entry_zone_bottom, 1e-9) <= distance_pct


def find_order_block(
    bars: pd.DataFrame,
    bos: BOS,
    lookback: int = 10,
) -> Optional[OrderBlock]:
    """Walk backward from the BOS impulse to find the last opposing candle.

    Bullish BOS → look for the last BEARISH candle (close < open) before the
        BOS impulse. That candle is the bullish Order Block (demand zone).

    Bearish BOS → look for the last BULLISH candle (close > open) before the
        BOS impulse. That candle is the bearish Order Block (supply zone).
    """
    start = bos.bar_index - 1
    end = max(0, bos.bar_index - lookback)

    for i in range(start, end - 1, -1):
        if i < 0 or i >= len(bars):
            continue
        bar = bars.iloc[i]

        if bos.direction == "bullish" and bar["close"] < bar["open"]:
            logger.debug(
                "Bullish OB found at bar %d (%s): high=%.4f low=%.4f",
                i, bars.index[i], bar["high"], bar["low"],
            )
            return OrderBlock(
                kind="bullish",
                high=float(bar["high"]),
                low=float(bar["low"]),
                open_=float(bar["open"]),
                close_=float(bar["close"]),
                bar_index=i,
                timestamp=bars.index[i],
            )

        elif bos.direction == "bearish" and bar["close"] > bar["open"]:
            logger.debug(
                "Bearish OB found at bar %d (%s): high=%.4f low=%.4f",
                i, bars.index[i], bar["high"], bar["low"],
            )
            return OrderBlock(
                kind="bearish",
                high=float(bar["high"]),
                low=float(bar["low"]),
                open_=float(bar["open"]),
                close_=float(bar["close"]),
                bar_index=i,
                timestamp=bars.index[i],
            )

    return None

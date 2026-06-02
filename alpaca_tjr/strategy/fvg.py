"""Fair Value Gap (FVG) detection and tracking."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional

import pandas as pd

logger = logging.getLogger(__name__)


@dataclass
class FVG:
    kind: str         # "bullish" | "bearish"
    top: float        # upper boundary of the gap zone
    bottom: float     # lower boundary of the gap zone
    bar_index: int    # index of the middle candle
    timestamp: datetime
    filled: bool = False

    @property
    def midpoint(self) -> float:
        return (self.top + self.bottom) / 2.0

    @property
    def size(self) -> float:
        return self.top - self.bottom

    def check_filled(self, bar: pd.Series) -> None:
        """Mark as filled if price has traded through the entire zone."""
        if self.filled:
            return
        if self.kind == "bullish" and bar["low"] <= self.bottom:
            self.filled = True
        elif self.kind == "bearish" and bar["high"] >= self.top:
            self.filled = True

    def price_in_zone(self, price: float, tolerance: float = 0.001) -> bool:
        """True when price is at or within tolerance of entering the FVG zone."""
        lower = self.bottom * (1 - tolerance)
        upper = self.top * (1 + tolerance)
        return lower <= price <= upper

    def approaching(self, price: float, distance_pct: float = 0.002) -> bool:
        """True when price is within distance_pct of the near edge of the zone."""
        if self.kind == "bullish":
            return abs(price - self.top) / max(self.top, 1e-9) <= distance_pct
        else:
            return abs(price - self.bottom) / max(self.bottom, 1e-9) <= distance_pct


def scan_fvgs(bars: pd.DataFrame, max_age_bars: int = 50) -> List[FVG]:
    """Scan bars for all active (unfilled) Fair Value Gaps.

    Bullish FVG: bars[i-2].high < bars[i].low  (gap to the upside)
        zone = [bars[i-2].high, bars[i].low]

    Bearish FVG: bars[i-2].low > bars[i].high  (gap to the downside)
        zone = [bars[i].high, bars[i-2].low]

    The third bar (index i) is the confirmation candle; index i-1 is the
    impulse, and i-2 is the reference candle.
    """
    if len(bars) < 3:
        return []

    fvgs: List[FVG] = []
    n = len(bars)
    cutoff = n - max_age_bars  # ignore very old gaps

    for i in range(max(2, cutoff), n):
        c0 = bars.iloc[i - 2]  # reference
        c1 = bars.iloc[i - 1]  # impulse (middle — creates the gap)
        c2 = bars.iloc[i]      # confirmation

        # Bullish FVG
        if c0["high"] < c2["low"]:
            fvg = FVG(
                kind="bullish",
                top=float(c2["low"]),
                bottom=float(c0["high"]),
                bar_index=i - 1,
                timestamp=bars.index[i - 1],
            )
            # Check if subsequent bars have already filled it
            for j in range(i, n):
                fvg.check_filled(bars.iloc[j])
            if not fvg.filled:
                fvgs.append(fvg)

        # Bearish FVG
        elif c0["low"] > c2["high"]:
            fvg = FVG(
                kind="bearish",
                top=float(c0["low"]),
                bottom=float(c2["high"]),
                bar_index=i - 1,
                timestamp=bars.index[i - 1],
            )
            for j in range(i, n):
                fvg.check_filled(bars.iloc[j])
            if not fvg.filled:
                fvgs.append(fvg)

    return fvgs


def nearest_fvg(
    fvgs: List[FVG],
    price: float,
    direction: str,
) -> Optional[FVG]:
    """Return the unfilled FVG in `direction` nearest to current price."""
    candidates = [f for f in fvgs if f.kind == direction and not f.filled]
    if not candidates:
        return None
    return min(candidates, key=lambda f: abs(f.midpoint - price))

"""Key price levels: PDH/PDL, pre-market range, VWAP, opening price."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

import pandas as pd

logger = logging.getLogger(__name__)


@dataclass
class KeyLevels:
    pdh: float            # Previous Day High
    pdl: float            # Previous Day Low
    pm_high: float        # Pre-market High (4am–9:30am ET)
    pm_low: float         # Pre-market Low
    vwap: float           # Current-day VWAP (updated each bar)
    opening_price: float  # First print at 9:30 ET

    def all_levels(self) -> dict[str, float]:
        return {
            "pdh": self.pdh,
            "pdl": self.pdl,
            "pm_high": self.pm_high,
            "pm_low": self.pm_low,
            "vwap": self.vwap,
        }

    def nearest_level(self, price: float, side: str) -> tuple[str, float]:
        """Return the key level name and value most relevant for a sweep check.

        side='below' → levels above which a bullish sweep would retest
        side='above' → levels below which a bearish sweep would retest
        """
        if side == "below":
            candidates = {k: v for k, v in self.all_levels().items() if v < price}
            if not candidates:
                return ("pdl", self.pdl)
            name = min(candidates, key=lambda k: price - candidates[k])
        else:
            candidates = {k: v for k, v in self.all_levels().items() if v > price}
            if not candidates:
                return ("pdh", self.pdh)
            name = min(candidates, key=lambda k: candidates[k] - price)
        return (name, candidates[name])


def build_key_levels(
    prev_day_bars: pd.DataFrame,
    pm_high: float,
    pm_low: float,
    vwap: float,
    opening_price: float,
) -> Optional[KeyLevels]:
    """Construct KeyLevels from previous day daily bar + pre-market data."""
    if prev_day_bars.empty:
        logger.warning("No previous day bars — key levels unavailable")
        return None

    last = prev_day_bars.iloc[-1]
    return KeyLevels(
        pdh=float(last["high"]),
        pdl=float(last["low"]),
        pm_high=pm_high,
        pm_low=pm_low,
        vwap=vwap,
        opening_price=opening_price,
    )

"""Higher-timeframe trend bias using a daily SMA."""
from __future__ import annotations

import logging
from typing import Optional

import pandas as pd
import numpy as np

logger = logging.getLogger(__name__)

BIAS_BULL = "bull"
BIAS_BEAR = "bear"
BIAS_NEUTRAL = "neutral"


def compute_bias(
    daily_bars: pd.DataFrame,
    sma_period: int = 20,
    neutral_band: float = 0.001,
) -> str:
    """Determine trend bias from daily bars.

    Bull  : last close > SMA20  (by more than neutral_band)
    Bear  : last close < SMA20  (by more than neutral_band)
    Neutral: within neutral_band of SMA (avoid choppy near-pivot entries)
    """
    if daily_bars.empty or len(daily_bars) < sma_period:
        logger.debug("Insufficient daily bars for bias (%d < %d)", len(daily_bars), sma_period)
        return BIAS_NEUTRAL

    closes = daily_bars["close"].values
    sma = float(np.mean(closes[-sma_period:]))
    last_close = float(closes[-1])

    if sma == 0:
        return BIAS_NEUTRAL

    pct_diff = (last_close - sma) / sma

    if pct_diff > neutral_band:
        return BIAS_BULL
    elif pct_diff < -neutral_band:
        return BIAS_BEAR
    return BIAS_NEUTRAL


def bias_allows(bias: str, direction: str) -> bool:
    """True when the HTF bias permits a trade in `direction` (long|short)."""
    if bias == BIAS_NEUTRAL:
        return False
    return (bias == BIAS_BULL and direction == "long") or \
           (bias == BIAS_BEAR and direction == "short")

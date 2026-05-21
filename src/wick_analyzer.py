"""
Wick & price-action analyzer.

Two custom signals built on top of OHLCV data:

  1. Rejection wicks — when N consecutive candles print upper wicks ≥ 2× body
     at roughly the same price, that's real supply (a ceiling).  Symmetric
     case for lower wicks = floor.  Blocks longs into ceilings and shorts
     into floors.

  2. Stop hunts — when a candle wicks > 0.15% beyond a recent 60-bar
     swing high/low and then closes back inside it within 2 candles, that
     is a forced-liquidation flush.  Trading WITH the reversal historically
     prints a high win rate.

Both signals are computed from a pandas DataFrame with columns:
    open, high, low, close, volume   (datetime index, oldest→newest).
"""

from __future__ import annotations

import logging
from typing import Optional

import pandas as pd

logger = logging.getLogger(__name__)

# Rejection thresholds
_WICK_BODY_RATIO   = 2.0    # wick must be ≥ N× the body to count
_REJECT_LOOKBACK   = 5      # check the last N candles for clustered rejections
_REJECT_MIN_COUNT  = 2      # need at least N rejection candles in the window
_PRICE_BAND_PCT    = 0.20   # cluster band: rejections within 0.20% of each other

# Stop-hunt thresholds
_HUNT_SWING_BARS   = 60     # swing high/low lookback
_HUNT_PIERCE_PCT   = 0.15   # wick must pierce swing by ≥ this %
_HUNT_RECLAIM_BARS = 2      # close back inside the swing within N bars


def _safe_body(row) -> float:
    return abs(float(row['close']) - float(row['open']))


def _upper_wick(row) -> float:
    return float(row['high']) - max(float(row['open']), float(row['close']))


def _lower_wick(row) -> float:
    return min(float(row['open']), float(row['close'])) - float(row['low'])


def detect_rejection(df: pd.DataFrame,
                     side: str = 'buy',
                     lookback: int = _REJECT_LOOKBACK) -> Optional[str]:
    """
    Look back N candles. For a long, count upper-wick rejections (supply
    rejecting price). For a short, count lower-wick rejections (demand
    rejecting price).  If ≥ _REJECT_MIN_COUNT cluster within _PRICE_BAND_PCT
    of each other, return a reason string; else None.
    """
    if df is None or len(df) < lookback:
        return None

    side = side.lower()
    window = df.iloc[-lookback:]
    rejection_prices = []

    for _, row in window.iterrows():
        body = _safe_body(row) + 1e-9
        if side in ('buy', 'long'):
            wick = _upper_wick(row)
            if wick / body >= _WICK_BODY_RATIO:
                rejection_prices.append(float(row['high']))
        else:  # sell / short
            wick = _lower_wick(row)
            if wick / body >= _WICK_BODY_RATIO:
                rejection_prices.append(float(row['low']))

    if len(rejection_prices) < _REJECT_MIN_COUNT:
        return None

    # Cluster check: max-min within _PRICE_BAND_PCT
    lo, hi = min(rejection_prices), max(rejection_prices)
    mid = (lo + hi) / 2.0
    if mid <= 0:
        return None
    band_pct = (hi - lo) / mid * 100.0
    if band_pct > _PRICE_BAND_PCT:
        return None

    direction = "ceiling" if side in ('buy', 'long') else "floor"
    return (f"WICK_REJECTION {direction} at ~{mid:.2f} "
            f"({len(rejection_prices)} wicks in {lookback} bars)")


def detect_stop_hunt(df: pd.DataFrame,
                     side: str = 'buy',
                     swing_bars: int = _HUNT_SWING_BARS) -> Optional[dict]:
    """
    Detect a recent stop-hunt reversal that supports the proposed entry.

    For a BUY: look for a wick that pierced the recent _HUNT_SWING_BARS low
    by ≥ _HUNT_PIERCE_PCT% and was reclaimed (close > swing_low) within
    _HUNT_RECLAIM_BARS.  This is bullish — longs entering with the reclaim.

    For a SELL: symmetric — wick above swing high, then close back below.

    Returns a small dict {'price': pierce_price, 'reclaim_bar': i} on a
    confirmed hunt, else None.  Returning a dict (vs string) keeps this as
    a *positive* signal that the entry path can use as a fast-track edge —
    it is not a blocker.
    """
    needed = swing_bars + _HUNT_RECLAIM_BARS + 1
    if df is None or len(df) < needed:
        return None

    side = side.lower()
    recent = df.iloc[-(swing_bars + _HUNT_RECLAIM_BARS + 1):]
    # Reference swing: the bars BEFORE the potential hunt window
    swing_window = recent.iloc[:swing_bars]
    hunt_window  = recent.iloc[swing_bars:]

    if side in ('buy', 'long'):
        swing_low = float(swing_window['low'].min())
        if swing_low <= 0:
            return None
        pierce_thresh = swing_low * (1 - _HUNT_PIERCE_PCT / 100.0)
        # Find first bar in hunt window that pierced
        for i, (_, row) in enumerate(hunt_window.iterrows()):
            low_v = float(row['low'])
            if low_v <= pierce_thresh:
                # Check if any subsequent bar (inclusive) reclaimed by close
                tail = hunt_window.iloc[i:]
                for j, (_, tr) in enumerate(tail.iterrows()):
                    if float(tr['close']) > swing_low:
                        return {
                            'side': 'buy',
                            'pierce_price': low_v,
                            'swing_level':  swing_low,
                            'reclaim_lag':  j,
                        }
                break
        return None

    # short side
    swing_high = float(swing_window['high'].max())
    if swing_high <= 0:
        return None
    pierce_thresh = swing_high * (1 + _HUNT_PIERCE_PCT / 100.0)
    for i, (_, row) in enumerate(hunt_window.iterrows()):
        high_v = float(row['high'])
        if high_v >= pierce_thresh:
            tail = hunt_window.iloc[i:]
            for j, (_, tr) in enumerate(tail.iterrows()):
                if float(tr['close']) < swing_high:
                    return {
                        'side': 'sell',
                        'pierce_price': high_v,
                        'swing_level':  swing_high,
                        'reclaim_lag':  j,
                    }
            break
    return None

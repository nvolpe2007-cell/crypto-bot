"""
Trailing Stop Manager — handles multi-tier trailing stops + max-hold backstops.

Tier-mapped trail behavior (price-based, no extra timeframe data needed):
  scalp / atr_stop  : no trail — fixed SL/TP from signal (legacy behavior)
  swing / ema21_1h  : after +0.5% favorable, trail 1.0% below peak
  position / ema50_4h: after +1.0% favorable, trail 2.0% below peak

All tiers also enforce a max-hold backstop: forced exit when intended_hold
elapsed regardless of price.

The manager is *stateless* — it reads/writes peak_favorable_price and
trail_stop_price directly on the PaperPosition object that paper_trading.py
already maintains.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional, Tuple

logger = logging.getLogger(__name__)


# (trail_style, trigger_pct, trail_pct)
#   trigger_pct: price must move this far in favor before trailing arms
#   trail_pct:   distance below (long) / above (short) the peak that triggers exit
TRAIL_PARAMS = {
    "atr_stop":  (None, None),     # no trailing
    "ema21_1h":  (0.005, 0.010),   # arm at +0.5%, trail by 1.0%
    "ema50_4h":  (0.010, 0.020),   # arm at +1.0%, trail by 2.0%
}

# Breakeven trail — applies to ALL trail styles regardless of tier.
# Round-trip Kraken spot fees ≈ 0.52% (0.26% per side). BE stop must clear fees
# or "breakeven" exits net-lose. Arm at +0.85% MFE; BE stop at entry +0.55%
# (gross +0.55% → net ~+0.03% after fees). Live-data fix: losers had avg MFE
# 0.23% then bled out, so this only triggers on the ~30% of trades that reach
# 0.85% MFE — but lets them lock in a small profit instead of round-tripping.
BE_ARM_PCT     = 0.0085  # 0.85% favorable to arm
BE_STOP_BUFFER = 0.0055  # exit if price retraces to entry + 0.55%


def _is_long(pos) -> bool:
    return getattr(pos, "side", "buy") == "buy"


def update_trailing_stop(pos, current_price: float) -> Optional[str]:
    """
    Update trail state on the position and return an exit reason if triggered.

    Returns one of:
      None         — no exit, just state update
      'TRAIL_STOP' — price crossed the trailing stop
      'MAX_HOLD'   — intended hold duration elapsed
    """
    trail_style = getattr(pos, "trail_style", "atr_stop")
    hold_min    = getattr(pos, "intended_hold_min", 0)

    # 1. Max-hold backstop — fires regardless of trail style (when set)
    if hold_min and pos.entry_time:
        elapsed_min = (datetime.now(timezone.utc) - pos.entry_time).total_seconds() / 60.0
        if elapsed_min >= hold_min:
            return "MAX_HOLD"

    entry   = pos.entry_price
    is_long = _is_long(pos)

    # Favorable move %
    if is_long:
        fav_pct = (current_price - entry) / entry
    else:
        fav_pct = (entry - current_price) / entry

    # Update peak favorable price (used by both BE trail and tier trail below)
    if is_long:
        if not pos.peak_favorable_price or current_price > pos.peak_favorable_price:
            pos.peak_favorable_price = current_price
    else:
        if not pos.peak_favorable_price or current_price < pos.peak_favorable_price:
            pos.peak_favorable_price = current_price

    # Peak favorable as a fraction (uses tracked peak, not just current tick)
    if is_long:
        peak_fav_pct = (pos.peak_favorable_price - entry) / entry
    else:
        peak_fav_pct = (entry - pos.peak_favorable_price) / entry

    # 2. Breakeven trail — applies to ALL trail styles. Once MFE clears fees,
    #    set a virtual stop at entry ± small buffer. Live-data fix: losers had
    #    avg MFE 0.23% then bled to small losses; this locks in net profit.
    if peak_fav_pct >= BE_ARM_PCT:
        if is_long:
            be_stop = entry * (1 + BE_STOP_BUFFER)
            if current_price <= be_stop:
                return "BREAKEVEN"
        else:
            be_stop = entry * (1 - BE_STOP_BUFFER)
            if current_price >= be_stop:
                return "BREAKEVEN"

    # 3. Tier-specific trailing stop
    if trail_style not in TRAIL_PARAMS:
        return None
    trigger_pct, trail_pct = TRAIL_PARAMS[trail_style]
    if trigger_pct is None:
        return None  # scalp / atr_stop — no tier trail (BE trail above still applies)

    # Arm / update the trailing stop once price has moved trigger_pct in our
    # favour.  If the trail was already armed on a prior tick, skip re-arming
    # but still fall through to the crossing check below — a retracement that
    # pushes fav_pct back below trigger_pct must not prevent the stop from
    # firing when the price crosses it.
    if fav_pct >= trigger_pct:
        if is_long:
            new_stop = pos.peak_favorable_price * (1 - trail_pct)
            if not pos.trail_stop_price or new_stop > pos.trail_stop_price:
                pos.trail_stop_price = new_stop
        else:
            new_stop = pos.peak_favorable_price * (1 + trail_pct)
            if not pos.trail_stop_price or new_stop < pos.trail_stop_price:
                pos.trail_stop_price = new_stop
    elif not pos.trail_stop_price:
        return None  # trail not yet armed and trigger not reached

    # Check whether price has crossed the (possibly pre-existing) stop level
    if is_long:
        if current_price <= pos.trail_stop_price:
            return "TRAIL_STOP"
    else:
        if current_price >= pos.trail_stop_price:
            return "TRAIL_STOP"

    return None

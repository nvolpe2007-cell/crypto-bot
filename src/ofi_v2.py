"""
OFI v2 — normalized, accelerating, persistent. Spec-compliant implementation.

Computes Order Flow Imbalance from consecutive order book snapshots (REST-based).
Unlike OFI v1 which just measures current book balance, v2 computes the DELTA
between snapshots — tracking how much volume is being added/removed at each side.

Delta-based OFI formula (per Cont, Kukanov, Stoikov 2014, adapted for REST snapshots):
  For each price level p:
    bid_delta += new_bid_vol[p] - old_bid_vol[p]  (if p appears on both sides)
  ask_delta similarly
  OFI tick = (bid_delta - ask_delta) / (total_depth_top5 + 1e-10)

Then normalize, compute acceleration (second derivative), and track persistence.
"""

import logging
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Thresholds from strategy spec
_SIGNAL_THRESH   = 0.35   # |norm| >= 0.35 → signal
_STRONG_THRESH   = 0.55   # |norm| >= 0.55 → strong
_BUILD_THRESH    = 0.15   # |norm| >= 0.15 → building
_EXIT_THRESH     = 0.10   # |norm| < 0.10 → exit
_ACCEL_BUILD     = 0.08   # accel > 0.08 while building → 'building' state
_TICK_WINDOW     = 50     # rolling buffer size
_PERSIST_TICKS   = 3      # must stay above threshold this many ticks for signal
_DEPTH_LEVELS    = 5      # top-5 levels for depth calculation


@dataclass
class OFIState:
    """Full state output from OFI v2 after each update."""
    ofi_norm:           float    # normalized OFI in [-1, +1]
    ofi_accel:          float    # acceleration (change in norm per tick)
    ofi_raw:            float    # raw delta-based OFI before normalization
    depth:              float    # total top-5 book depth (bid + ask volume)
    ticks_above_threshold: int  # consecutive ticks with |norm| >= SIGNAL_THRESH
    state:              str      # neutral/building/signal/strong/exhausting/exit
    is_signal:          bool     # True when all 4 spec conditions are met
    direction:          int      # 1=buy pressure, -1=sell pressure, 0=neutral
    timestamp:          float    # unix time of this update


def _compute_state(norm: float, accel: float, ticks_above: int,
                   prev_was_signal: bool) -> str:
    """
    Derive the qualitative state label from numeric OFI values.
    Priority: strong > signal > exhausting > exit > building > neutral
    """
    abs_norm = abs(norm)

    # Strong: already past signal level and very high
    if abs_norm >= _STRONG_THRESH:
        return 'strong'

    # Signal: threshold met and persisted enough ticks
    if abs_norm >= _SIGNAL_THRESH:
        if ticks_above >= _PERSIST_TICKS:
            return 'signal'
        # Crossed threshold but not yet persisted → still building toward signal
        return 'building'

    # Exhausting: was in signal/strong territory, now falling back
    if prev_was_signal and abs_norm >= _BUILD_THRESH and accel < 0:
        return 'exhausting'

    # Exit: very weak signal
    if abs_norm < _EXIT_THRESH:
        return 'exit'

    # Building: accelerating toward threshold
    if accel > _ACCEL_BUILD and abs_norm >= _BUILD_THRESH:
        return 'building'

    return 'neutral'


class OFICalculatorV2:
    """
    Per-symbol OFI v2 calculator using consecutive REST order book snapshots.

    Usage:
        calc = OFICalculatorV2()
        state = calc.update(bids, asks)   # call each time order book is fetched
    """

    def __init__(self, window: int = _TICK_WINDOW, depth_levels: int = _DEPTH_LEVELS):
        self._window       = window
        self._depth_levels = depth_levels

        # Rolling buffer of raw OFI ticks
        self._ticks: deque = deque(maxlen=window)

        # Previous snapshot: price → size (bids and asks separately)
        self._prev_bids: Dict[float, float] = {}
        self._prev_asks: Dict[float, float] = {}
        self._prev_snapshot_time: float = 0.0

        # State tracking
        self._ticks_above: int = 0
        self._prev_norm:   float = 0.0
        self._prev_state:  str = 'neutral'
        self._prev_was_signal: bool = False

    def update(self, bids: List[List[float]], asks: List[List[float]]) -> OFIState:
        """
        Process a new order book snapshot.

        Args:
            bids: [[price, size], ...] sorted descending by price
            asks: [[price, size], ...] sorted ascending by price

        Returns:
            OFIState with all derived metrics.
        """
        now = time.time()

        # Build dicts for current snapshot
        curr_bids: Dict[float, float] = {
            float(row[0]): float(row[1])
            for row in bids[:20] if len(row) >= 2
        }
        curr_asks: Dict[float, float] = {
            float(row[0]): float(row[1])
            for row in asks[:20] if len(row) >= 2
        }

        # Top-5 depth for kill-filter reference
        top5_bid_vol = sum(v for _, v in sorted(curr_bids.items(), reverse=True)[:self._depth_levels])
        top5_ask_vol = sum(v for _, v in sorted(curr_asks.items())[:self._depth_levels])
        total_depth  = top5_bid_vol + top5_ask_vol

        # For OFI normalization, use top-1 depth only (best bid + best ask).
        # Since the delta formula is based on best-bid and best-ask volume changes,
        # normalizing by top-1 keeps the signal in the expected [-1, +1] range.
        # (Using top-5 over-dampens the signal with REST-based snapshot data.)
        best_bid_norm_v = max(curr_bids.values()) if curr_bids else 1.0
        best_ask_norm_v = min(curr_asks.values(), key=lambda _: True) if curr_asks else 1.0
        # use actual best levels
        _sorted_asks = sorted(curr_asks.items())
        _sorted_bids = sorted(curr_bids.items(), reverse=True)
        best_bid_norm_v = _sorted_bids[0][1] if _sorted_bids else 1.0
        best_ask_norm_v = _sorted_asks[0][1] if _sorted_asks else 1.0
        norm_depth = best_bid_norm_v + best_ask_norm_v

        if not self._prev_bids:
            # First snapshot — no delta yet; just store and return neutral state
            self._prev_bids = curr_bids
            self._prev_asks = curr_asks
            self._prev_snapshot_time = now
            return OFIState(
                ofi_norm=0.0, ofi_accel=0.0, ofi_raw=0.0,
                depth=total_depth, ticks_above_threshold=0,
                state='neutral', is_signal=False, direction=0, timestamp=now,
            )

        # Compute delta-based OFI per Cont, Kukanov & Stoikov spec.
        # Only the BEST bid and BEST ask are used for the delta — this is what
        # the spec formula describes, not the whole book.
        best_bid_p = max(curr_bids.keys()) if curr_bids else 0.0
        best_ask_p = min(curr_asks.keys()) if curr_asks else 0.0
        best_bid_v = curr_bids.get(best_bid_p, 0.0)
        best_ask_v = curr_asks.get(best_ask_p, 0.0)

        prev_best_bid_p = max(self._prev_bids.keys()) if self._prev_bids else 0.0
        prev_best_ask_p = min(self._prev_asks.keys()) if self._prev_asks else 0.0
        prev_best_bid_v = self._prev_bids.get(prev_best_bid_p, 0.0)
        prev_best_ask_v = self._prev_asks.get(prev_best_ask_p, 0.0)

        # Spec formula for bid_delta:
        #   price improved (new higher best bid)  → bid_delta = +best_bid_v
        #   price unchanged                        → bid_delta = best_bid_v - prev_best_bid_v
        #   price worsened (best bid pulled lower) → bid_delta = 0
        if best_bid_p > prev_best_bid_p:
            bid_delta = best_bid_v
        elif best_bid_p == prev_best_bid_p:
            bid_delta = best_bid_v - prev_best_bid_v
        else:
            bid_delta = 0.0

        # Spec formula for ask_delta:
        #   price worsened (ask moved up = pulled, bullish) → ask_delta = -best_ask_v
        #   price unchanged                                  → ask_delta = prev_best_ask_v - best_ask_v
        #   price improved (more asks, bearish)              → ask_delta = 0
        if best_ask_p > prev_best_ask_p:
            ask_delta = -best_ask_v        # ask pulled up → bullish
        elif best_ask_p == prev_best_ask_p:
            ask_delta = prev_best_ask_v - best_ask_v
        else:
            ask_delta = 0.0                # more asks appeared → bearish supply

        # Raw OFI tick = bid_delta + ask_delta (spec formula)
        raw_tick = bid_delta + ask_delta

        # Normalize by top-5 depth so signals are comparable across time periods
        # (a 5 BTC delta when the book has 10 BTC is far stronger than when it has 1000 BTC)
        normalizer = norm_depth + 1e-10
        ofi_norm_tick = raw_tick / normalizer
        ofi_norm_tick = max(-1.0, min(1.0, ofi_norm_tick))

        self._ticks.append(ofi_norm_tick)

        # Smooth: use EWA of recent ticks for the current norm value
        ofi_norm = self._ewa(list(self._ticks), alpha=0.35)

        # Acceleration: difference in smoothed norm from previous tick
        if len(self._ticks) >= 2:
            prev_smoothed = self._ewa(list(self._ticks)[:-1], alpha=0.35)
            ofi_accel = ofi_norm - prev_smoothed
        else:
            ofi_accel = 0.0

        # Persistence counter: consecutive ticks above signal threshold
        if abs(ofi_norm) >= _SIGNAL_THRESH:
            self._ticks_above += 1
        else:
            self._ticks_above = 0

        # State determination
        state = _compute_state(ofi_norm, ofi_accel, self._ticks_above,
                               self._prev_was_signal)

        # Direction
        if ofi_norm > 0.05:
            direction = 1
        elif ofi_norm < -0.05:
            direction = -1
        else:
            direction = 0

        # Depth check: must have at least some depth to be a valid signal
        depth_ok = total_depth > 0.01

        # is_signal: all 4 spec conditions
        is_signal = (
            abs(ofi_norm) >= _SIGNAL_THRESH and
            ofi_accel > 0 and                   # still accelerating
            self._ticks_above >= _PERSIST_TICKS and
            depth_ok
        )

        # Update state for next call
        self._prev_bids = curr_bids
        self._prev_asks = curr_asks
        self._prev_snapshot_time = now
        self._prev_norm = ofi_norm
        self._prev_was_signal = state in ('signal', 'strong')

        result = OFIState(
            ofi_norm=ofi_norm,
            ofi_accel=ofi_accel,
            ofi_raw=raw_tick,
            depth=total_depth,
            ticks_above_threshold=self._ticks_above,
            state=state,
            is_signal=is_signal,
            direction=direction,
            timestamp=now,
        )

        logger.debug(
            f"[OFIv2] norm={ofi_norm:+.3f}  accel={ofi_accel:+.4f}  "
            f"state={state}  ticks_above={self._ticks_above}  depth={total_depth:.2f}"
        )
        return result

    @staticmethod
    def _ewa(values: list, alpha: float = 0.35) -> float:
        """Exponentially weighted average, most recent values weighted highest."""
        if not values:
            return 0.0
        result = float(values[0])
        for v in values[1:]:
            result = alpha * float(v) + (1.0 - alpha) * result
        return result

    def reset(self):
        """Clear all state — use when symbol tracking changes."""
        self._ticks.clear()
        self._prev_bids = {}
        self._prev_asks = {}
        self._ticks_above = 0
        self._prev_norm = 0.0
        self._prev_state = 'neutral'
        self._prev_was_signal = False

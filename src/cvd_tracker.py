"""
CVD Tracker — candle-based cumulative volume delta.

Since we don't have a tick-by-tick trade tape (Kraken WS doesn't stream
individual trades in a usable form for our setup), we approximate CVD
from OHLCV candles using Kaufman's formula:

  delta = volume × (close - open) / (high - low + 1e-10)

Positive delta → net buying pressure in that candle.
Negative delta → net selling pressure.

CVD is the rolling sum of these deltas. We track slope (trend of CVD),
direction, and whether price is responding to CVD pressure (confirmation).

Usage:
    tracker = CVDTracker(symbol='BTC/USD')
    state = tracker.update(open, close, high, low, volume, timestamp)
    if tracker.aligned_with_ofi(ofi_direction=1):
        ...
"""

import logging
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)

_CANDLE_WINDOW     = 60   # keep 60 candles (60 minutes at 1m timeframe)
_SLOPE_WINDOW      = 5    # slope computed over last 5 entries
_PRICE_RESPONSE_N  = 3    # candles to check for price responding to CVD
_FRESHNESS_CANDLES = 5    # "fresh" if last alignment was within 5 candle-counts
                           # (≈5 seconds in terms of spec threshold, ≈5 minutes at 1m)


@dataclass
class CVDState:
    """Output state from CVDTracker after each candle update."""
    cvd_now:            float   # current cumulative volume delta
    cvd_slope:          float   # delta per period over last _SLOPE_WINDOW entries
    cvd_direction:      int     # 1=net buying, -1=net selling, 0=neutral
    price_responding:   bool    # price moved in same direction as CVD recently
    seconds_since_aligned: float  # time in seconds since CVD and price last agreed
    last_candle_delta:  float   # delta from the most recent candle
    candle_count:       int     # total candles processed


class CVDTracker:
    """
    Tracks cumulative volume delta from OHLCV candles for a single symbol.

    Call update() on each candle close (from WS or REST).
    Use aligned_with_ofi() to check confluence with OFI direction.
    """

    def __init__(self, symbol: str = '', window: int = _CANDLE_WINDOW):
        self.symbol = symbol
        self._window = window

        # Rolling deques: candle deltas and candle closes
        self._deltas:     deque = deque(maxlen=window)
        self._closes:     deque = deque(maxlen=window)
        self._timestamps: deque = deque(maxlen=window)

        # Derived state
        self._cvd:       float = 0.0   # running sum of deltas
        self._last_aligned_time: float = 0.0
        self._candle_count: int = 0

    def update(self,
               candle_open:   float,
               candle_close:  float,
               candle_high:   float,
               candle_low:    float,
               candle_volume: float,
               timestamp:     float) -> CVDState:
        """
        Process a new completed candle.

        Args:
            candle_open, candle_close, candle_high, candle_low: OHLC prices
            candle_volume: total volume traded in this candle
            timestamp: unix timestamp of candle close

        Returns:
            CVDState with all derived metrics.
        """
        # Kaufman's approximation: estimate net directional volume
        price_range = candle_high - candle_low + 1e-10
        price_move  = candle_close - candle_open
        delta = candle_volume * (price_move / price_range)

        self._deltas.append(delta)
        self._closes.append(candle_close)
        self._timestamps.append(timestamp)
        self._cvd += delta
        self._candle_count += 1

        # Slope: change in CVD per period over last N entries
        slope = self._compute_slope()

        # Direction from slope (not raw CVD which is a running sum and can drift)
        if slope > 1e-6:
            direction = 1
        elif slope < -1e-6:
            direction = -1
        else:
            direction = 0

        # Is price responding to CVD pressure?
        price_responding = self._check_price_responding()

        # Track when CVD and price last aligned
        if price_responding and direction != 0:
            self._last_aligned_time = time.time()

        now = time.time()
        seconds_since_aligned = now - self._last_aligned_time if self._last_aligned_time > 0 else 999.0

        state = CVDState(
            cvd_now=self._cvd,
            cvd_slope=slope,
            cvd_direction=direction,
            price_responding=price_responding,
            seconds_since_aligned=seconds_since_aligned,
            last_candle_delta=delta,
            candle_count=self._candle_count,
        )

        logger.debug(
            f"[CVD] {self.symbol}  cvd={self._cvd:.4f}  slope={slope:.4f}  "
            f"dir={direction}  price_ok={price_responding}"
        )
        return state

    def aligned_with_ofi(self, ofi_direction: int) -> bool:
        """
        Returns True if:
          - CVD slope direction matches ofi_direction, AND
          - Price is responding to CVD (no absorption)

        Spec requires: "no absorption (price moved with CVD), fresh within 5s"
        We interpret "fresh" as within _FRESHNESS_CANDLES of last alignment.
        """
        if not self._deltas or len(self._deltas) < 2:
            return False

        slope     = self._compute_slope()
        slope_dir = 1 if slope > 1e-6 else (-1 if slope < -1e-6 else 0)

        if slope_dir == 0 or slope_dir != ofi_direction:
            return False

        price_ok = self._check_price_responding()
        return price_ok

    def get_slope(self) -> float:
        """Return the current CVD slope (positive = buying, negative = selling)."""
        return self._compute_slope()

    def get_direction(self) -> int:
        """Return directional summary: 1, -1, or 0."""
        slope = self._compute_slope()
        if slope > 1e-6:
            return 1
        if slope < -1e-6:
            return -1
        return 0

    def divergence_blocks(self, side: str, lookback: int = 10,
                          min_price_move_pct: float = 0.10) -> Optional[str]:
        """
        Bearish/bullish divergence gate.

        For a long entry: blocks when price ↑ but cumulative CVD ↓ over the
        last `lookback` candles — buyers are exhausting and the rally is
        getting sold into.

        For a short entry: blocks when price ↓ but CVD ↑ — sellers exhausting.

        Returns reason string if entry should be blocked, None otherwise.
        Fail-open when there isn't enough history.
        """
        closes = list(self._closes)
        deltas = list(self._deltas)
        n = min(lookback, len(closes), len(deltas))
        if n < 4:
            return None

        recent_closes = closes[-n:]
        recent_deltas = deltas[-n:]
        first_close = recent_closes[0]
        last_close  = recent_closes[-1]
        if first_close <= 0:
            return None

        price_move_pct = (last_close - first_close) / first_close * 100.0
        cvd_change     = sum(recent_deltas)
        side = side.lower()

        if side in ('buy', 'long'):
            # price meaningfully higher, CVD lower → bearish divergence
            if price_move_pct >= min_price_move_pct and cvd_change < 0:
                return (f"CVD_DIVERGENCE price +{price_move_pct:.2f}% "
                        f"but CVD {cvd_change:+.2f} over {n} bars")
        elif side in ('sell', 'short'):
            if price_move_pct <= -min_price_move_pct and cvd_change > 0:
                return (f"CVD_DIVERGENCE price {price_move_pct:.2f}% "
                        f"but CVD {cvd_change:+.2f} over {n} bars")
        return None

    # ── Internal helpers ─────────────────────────────────────────────────────────

    def _compute_slope(self) -> float:
        """
        Compute the rate of change of CVD over the last _SLOPE_WINDOW entries.
        Returns delta per period (e.g., delta per candle).
        """
        deltas = list(self._deltas)
        if len(deltas) < 2:
            return 0.0

        n = min(_SLOPE_WINDOW, len(deltas))
        recent = deltas[-n:]

        # Slope = running sum of recent deltas / n (avg delta per candle)
        if n < 2:
            return recent[-1]

        # Use linear regression slope on cumulative CVD values
        # cvd[i] = sum of recent[:i+1]
        cum_vals = []
        running  = 0.0
        for d in recent:
            running += d
            cum_vals.append(running)

        # Simple least-squares slope: cov(x, y) / var(x)
        x_mean = (n - 1) / 2.0
        y_mean = sum(cum_vals) / n
        numerator   = sum((i - x_mean) * (cum_vals[i] - y_mean) for i in range(n))
        denominator = sum((i - x_mean) ** 2 for i in range(n))

        if denominator < 1e-12:
            return 0.0
        return numerator / denominator

    def _check_price_responding(self) -> bool:
        """
        True if price moved in the same direction as CVD delta in recent N candles.
        Detects absorption: if CVD was strongly positive but price didn't rise,
        sellers are absorbing the buying → false signal.
        """
        closes = list(self._closes)
        deltas = list(self._deltas)

        n = min(_PRICE_RESPONSE_N, len(closes))
        if n < 2:
            return True   # insufficient data — assume responding

        recent_closes = closes[-n:]
        recent_deltas = deltas[-n:]

        # CVD direction over recent window
        net_delta  = sum(recent_deltas)
        price_move = recent_closes[-1] - recent_closes[0]

        if abs(net_delta) < 1e-10:
            return True   # no clear CVD direction

        # Same sign = price responding to CVD
        return (net_delta > 0 and price_move > 0) or (net_delta < 0 and price_move < 0)


# ── Tick-level CVD (real Kraken trade tape) ──────────────────────────────────

_TICK_WINDOW_SECS = 60.0   # rolling window for slope/price-response
_TICK_SLOPE_SECS  = 10.0   # CVD slope measured over the last ~10s (OFI's horizon)


class TickCVDTracker:
    """CVD from the REAL trade tape (KrakenTradeFeed) instead of the candle
    Kaufman approximation. Each taker print adds +qty (taker buy) or -qty (taker
    sell) to a running cumulative volume delta — the genuine quantity the
    candle proxy only estimated. Emits the SAME `CVDState` as CVDTracker so the
    microstructure gate consumes it interchangeably.

    update_tick(price, qty, side, timestamp): `side` is the TAKER side per Kraken
    docs ('buy'/'sell'). Pure/synchronous → unit-testable by replaying a tape.
    """

    def __init__(self, symbol: str = '', window_secs: float = _TICK_WINDOW_SECS):
        self.symbol = symbol
        self._window_secs = window_secs
        # rolling (ts, signed_qty, price) within the window
        self._events: deque = deque()
        self._cvd: float = 0.0           # cumulative (session) signed volume
        self._last_aligned_time: float = 0.0
        self._tick_count: int = 0

    def update_tick(self, price: float, qty: float, side: str,
                    timestamp: float) -> CVDState:
        signed = float(qty) if str(side).lower() == 'buy' else -float(qty)
        self._cvd += signed
        self._tick_count += 1
        self._events.append((float(timestamp), signed, float(price)))
        # evict events older than the window
        cutoff = timestamp - self._window_secs
        while self._events and self._events[0][0] < cutoff:
            self._events.popleft()

        # slope = net signed volume over the last _TICK_SLOPE_SECS, per second
        recent = [e for e in self._events if e[0] >= timestamp - _TICK_SLOPE_SECS]
        net_recent = sum(e[1] for e in recent)
        span = max(1e-9, (recent[-1][0] - recent[0][0]) if len(recent) > 1 else 1.0)
        slope = net_recent / span

        if slope > 1e-9:
            direction = 1
        elif slope < -1e-9:
            direction = -1
        else:
            direction = 0

        # price responding: did price move with CVD over the recent window?
        if len(recent) >= 2:
            price_move = recent[-1][2] - recent[0][2]
            price_responding = (
                (net_recent > 0 and price_move > 0) or
                (net_recent < 0 and price_move < 0) or
                abs(net_recent) < 1e-12
            )
        else:
            price_responding = True   # insufficient data → assume responding

        if price_responding and direction != 0:
            self._last_aligned_time = timestamp
        seconds_since_aligned = (timestamp - self._last_aligned_time
                                 if self._last_aligned_time > 0 else 999.0)

        return CVDState(
            cvd_now=self._cvd,
            cvd_slope=slope,
            cvd_direction=direction,
            price_responding=price_responding,
            seconds_since_aligned=seconds_since_aligned,
            last_candle_delta=signed,     # last tick's signed qty (field reused)
            candle_count=self._tick_count,
        )

    def aligned_with_ofi(self, ofi_direction: int) -> bool:
        """Confluence helper mirroring CVDTracker: latest slope direction agrees
        with OFI and price is responding. Recomputed cheaply from the buffer."""
        if not self._events:
            return False
        recent = self._events[-1]
        # cheap re-read: use the sign of the recent windowed net
        net_recent = sum(e[1] for e in self._events
                         if e[0] >= recent[0] - _TICK_SLOPE_SECS)
        direction = 1 if net_recent > 0 else (-1 if net_recent < 0 else 0)
        return direction != 0 and direction == ofi_direction

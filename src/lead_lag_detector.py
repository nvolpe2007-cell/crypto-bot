"""
BTC Lead-Lag Detector
Academically documented: BTC price discovery leads ETH/SOL by 30s–5min.

When BTC moves > 0.30% within a rolling window, a directional signal is
emitted for all tracked altcoins. The signal decays linearly over 3 minutes
and is cancelled if the BTC move reverses before alts catch up.

Usage in paper_trading.py:
  detector.update_price('BTC/USD', price)
  signal = detector.get_signal('ETH/USD')  # 'BUY', 'SELL', or None
"""

import logging
import time
from collections import deque
from typing import Dict, Optional, Tuple

logger = logging.getLogger(__name__)

_MOVE_THRESHOLD  = 0.0020   # 0.20% BTC move triggers signal (0.30% was too rare on 1m)
_SIGNAL_DECAY_S  = 180       # signal valid for 3 minutes
_WINDOW_S        = 60        # measure BTC move over last 60 seconds
_REVERSAL_THRESH = 0.0015    # 0.15% reversal cancels the signal


class LeadLagDetector:
    """
    Tracks BTC price ticks and emits timed directional signals for alt symbols.
    Call update_price() on every WebSocket or poll tick for ALL symbols.
    """

    def __init__(self,
                 lead_symbol: str   = 'BTC/USD',
                 move_threshold: float = _MOVE_THRESHOLD,
                 decay_seconds:  float = _SIGNAL_DECAY_S):
        self.lead_symbol = lead_symbol
        self.threshold   = move_threshold
        self.decay       = decay_seconds

        # price history: symbol → deque of (timestamp, price)
        self._prices: Dict[str, deque] = {}

        # active signals: symbol → (direction, emitted_at, magnitude_pct)
        self._signals: Dict[str, Tuple[str, float, float]] = {}

        self._baseline_price: Optional[float] = None
        self._baseline_time:  float = 0.0

    # ── Public API ─────────────────────────────────────────────────────────────

    def update_price(self, symbol: str, price: float) -> None:
        """Record a new price tick for any symbol."""
        now = time.time()
        if symbol not in self._prices:
            self._prices[symbol] = deque(maxlen=120)
        self._prices[symbol].append((now, price))

        if symbol == self.lead_symbol:
            self._evaluate_btc(price, now)

    def get_signal(self, symbol: str) -> Optional[str]:
        """
        Returns 'BUY', 'SELL', or None.
        Expired signals are removed before returning.
        """
        sig = self._signals.get(symbol)
        if not sig:
            return None
        direction, emitted, _ = sig
        if time.time() - emitted > self.decay:
            del self._signals[symbol]
            return None
        return direction

    def get_strength(self, symbol: str) -> float:
        """Signal strength 0-1, decaying linearly to 0 at expiry."""
        sig = self._signals.get(symbol)
        if not sig:
            return 0.0
        direction, emitted, magnitude = sig
        age = time.time() - emitted
        if age >= self.decay:
            return 0.0
        time_factor = 1.0 - age / self.decay
        size_factor = min(1.0, magnitude / self.threshold)
        return time_factor * size_factor

    def confirms_buy(self, symbol: str) -> bool:
        """True unless BTC just led a significant move DOWN."""
        sig = self.get_signal(symbol)
        return sig != 'SELL'

    def confirms_sell(self, symbol: str) -> bool:
        """True unless BTC just led a significant move UP."""
        sig = self.get_signal(symbol)
        return sig != 'BUY'

    def summary(self, symbol: str) -> str:
        """Human-readable summary for Telegram analysis."""
        sig = self.get_signal(symbol)
        if not sig:
            return 'no BTC lead signal'
        strength = self.get_strength(symbol)
        return f"BTC led {sig} (strength {strength:.0%})"

    # ── Internal ───────────────────────────────────────────────────────────────

    def _evaluate_btc(self, current_price: float, now: float) -> None:
        """Check if BTC made a significant move and emit alt signals."""
        # Initialise or refresh baseline every _WINDOW_S seconds
        if not self._baseline_price or (now - self._baseline_time) >= _WINDOW_S:
            self._baseline_price = current_price
            self._baseline_time  = now
            return

        move = (current_price - self._baseline_price) / self._baseline_price

        if abs(move) < self.threshold:
            return  # not yet significant

        direction  = 'BUY' if move > 0 else 'SELL'
        magnitude  = abs(move)

        # Check if any existing signal would be reversed (cancels it)
        for sym in list(self._signals):
            existing_dir = self._signals[sym][0]
            if existing_dir != direction:
                # BTC reversed — cancel stale signal
                del self._signals[sym]

        # Emit new signals for all tracked alt symbols
        for sym in self._prices:
            if sym != self.lead_symbol:
                existing = self._signals.get(sym)
                # Only update if no signal, or new magnitude is larger
                if not existing or magnitude > existing[2]:
                    self._signals[sym] = (direction, now, magnitude)

        logger.info(
            f"[LEAD-LAG] BTC moved {move*100:+.2f}% → {direction} signal on alts "
            f"(strength {magnitude/self.threshold:.0%})"
        )

        # Reset baseline after emitting signal
        self._baseline_price = current_price
        self._baseline_time  = now

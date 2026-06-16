"""
Statistical arbitrage — pairs trading for BTC/ETH/SOL catch-up signals.

When BTC pumps but ETH hasn't followed, ETH is "lagging" and statistically
likely to catch up.  We trade the lagger in the direction of the leader.

Signal conditions (all must hold):
  • Leader 5-min return ≥ 0.8%   (meaningful move, not noise)
  • Lagger 5-min return ≤ 0.3%   (hasn't caught up yet)
  • Spread z-score ≥ 2.0          (statistically significant divergence)
  • Divergence is fresh < 10 min  (stale divergences don't work)

Exit: spread reverts to within 0.2 std devs of mean (mean-reversion complete).
"""

import logging
import time
from collections import deque
from dataclasses import dataclass
from itertools import combinations
from typing import Dict, List, Optional, Tuple

import numpy as np

logger = logging.getLogger(__name__)

MIN_LEADER_MOVE    = 0.008   # 0.8% in 5 minutes
MAX_LAGGER_MOVE    = 0.003   # 0.3% — lagger hasn't caught up yet
ZSCORE_THRESHOLD   = 2.0     # spread must be this many σ from mean
MAX_DIVERGENCE_AGE = 600     # 10 minutes — stale divergences don't work
EXIT_ZSCORE        = 0.2     # exit when spread returns to within 0.2σ
RETURN_WINDOW      = 300     # 5-minute return window (seconds)


@dataclass
class PairsSignal:
    leader:     str    # asset that already moved (e.g. "BTC/USD")
    lagger:     str    # asset that should catch up (e.g. "ETH/USD")
    direction:  str    # "long" or "short" (trade the lagger)
    z_score:    float  # how many σ the spread has diverged
    confidence: float  # 0–100


class PairsStrategy:
    """
    Monitors rolling price history for all symbol pairs and fires a
    PairsSignal when one asset significantly leads another.
    """

    def __init__(self, symbols: List[str], lookback_minutes: int = 30):
        self._lookback = lookback_minutes * 60
        # price_history: symbol → deque of (timestamp, price)
        self._history: Dict[str, deque] = {s: deque() for s in symbols}
        # All pairs from the symbol list
        self._pairs: List[Tuple[str, str]] = list(combinations(symbols, 2))
        # spread_history: (A, B) → deque of (timestamp, spread)
        self._spread_history: Dict[Tuple[str, str], deque] = {
            p: deque() for p in self._pairs
        }
        # divergence_start: when |z| first exceeded threshold for this pair
        self._divergence_start: Dict[Tuple[str, str], Optional[float]] = {
            p: None for p in self._pairs
        }

    # ── Public API ─────────────────────────────────────────────────────────────

    def update_price(self, symbol: str, price: float,
                     timestamp: Optional[float] = None):
        if symbol not in self._history:
            return
        ts = timestamp or time.time()
        self._history[symbol].append((ts, price))
        # Prune entries older than lookback + return window
        cutoff = ts - self._lookback - RETURN_WINDOW
        hist = self._history[symbol]
        while hist and hist[0][0] < cutoff:
            hist.popleft()
        # Update spread for every pair that includes this symbol
        for pair in self._pairs:
            if symbol in pair:
                self._update_spread(pair, ts)

    def evaluate(self) -> Optional[PairsSignal]:
        """Check all pairs and return the highest-confidence signal, or None."""
        now = time.time()
        best: Optional[PairsSignal] = None
        for pair in self._pairs:
            sig = self._evaluate_pair(pair, now)
            if sig and (best is None or sig.confidence > best.confidence):
                best = sig
        return best

    def should_exit(self, lagger: str, leader: str) -> bool:
        """True when the spread has mean-reverted to within EXIT_ZSCORE σ."""
        pair_key = (leader, lagger) if (leader, lagger) in self._spread_history \
                   else (lagger, leader)
        if pair_key not in self._spread_history:
            return False
        z = self._zscore(pair_key)
        return z is not None and abs(z) <= EXIT_ZSCORE

    # ── Internals ──────────────────────────────────────────────────────────────

    def _update_spread(self, pair: Tuple[str, str], now: float):
        a, b = pair
        ret_a = self._five_min_return(a, now)
        ret_b = self._five_min_return(b, now)
        if ret_a is None or ret_b is None:
            return
        spread = ret_a - ret_b
        self._spread_history[pair].append((now, spread))
        cutoff = now - self._lookback
        hist = self._spread_history[pair]
        while hist and hist[0][0] < cutoff:
            hist.popleft()

    def _five_min_return(self, symbol: str, now: float) -> Optional[float]:
        hist = self._history.get(symbol)
        if not hist:
            return None
        current = hist[-1][1]
        past = self._price_at(symbol, now - RETURN_WINDOW)
        if past is None or past == 0:
            return None
        return (current - past) / past

    def _price_at(self, symbol: str, target_ts: float) -> Optional[float]:
        """Nearest price to target_ts within a 90 s tolerance."""
        hist = self._history.get(symbol)
        if not hist:
            return None
        best_price = None
        best_diff  = 90.0
        prev_diff  = float('inf')
        for ts, price in hist:
            diff = abs(ts - target_ts)
            if diff < best_diff:
                best_diff  = diff
                best_price = price
            # Once past the closest point and moving away, stop early
            if diff > prev_diff and ts > target_ts:
                break
            prev_diff = diff
        return best_price

    def _zscore(self, pair: Tuple[str, str]) -> Optional[float]:
        hist = self._spread_history.get(pair)
        if not hist or len(hist) < 10:
            return None
        spreads = np.array([s for _, s in hist])
        mean = spreads.mean()
        std  = spreads.std()
        if std < 1e-9:
            return None
        return float((spreads[-1] - mean) / std)

    def _evaluate_pair(self, pair: Tuple[str, str],
                       now: float) -> Optional[PairsSignal]:
        a, b = pair
        z = self._zscore(pair)
        if z is None or abs(z) < ZSCORE_THRESHOLD:
            self._divergence_start[pair] = None
            return None

        ret_a = self._five_min_return(a, now)
        ret_b = self._five_min_return(b, now)
        if ret_a is None or ret_b is None:
            return None

        # z > 0: A outperformed B → A led, B lagged
        # z < 0: B outperformed A → B led, A lagged
        if z > 0:
            leader, lagger = a, b
            leader_ret, lagger_ret = ret_a, ret_b
        else:
            leader, lagger = b, a
            leader_ret, lagger_ret = ret_b, ret_a

        if abs(leader_ret) < MIN_LEADER_MOVE:
            self._divergence_start[pair] = None
            return None
        if abs(lagger_ret) > MAX_LAGGER_MOVE:
            self._divergence_start[pair] = None
            return None

        # Track how long this divergence has been active
        if self._divergence_start[pair] is None:
            self._divergence_start[pair] = now
        age = now - self._divergence_start[pair]
        if age > MAX_DIVERGENCE_AGE:
            self._divergence_start[pair] = None
            return None

        direction = 'long' if leader_ret > 0 else 'short'

        # Confidence scales with |z| (60 at threshold, up to 100) plus freshness bonus
        conf = min(100.0, 60.0 + (abs(z) - ZSCORE_THRESHOLD) * 20.0)
        freshness = max(0.0, (MAX_DIVERGENCE_AGE - age) / MAX_DIVERGENCE_AGE * 10.0)
        conf = min(100.0, conf + freshness)

        logger.info(
            f"[PAIRS] {leader.split('/')[0]} led {leader_ret*100:+.2f}%  "
            f"{lagger.split('/')[0]} lagged {lagger_ret*100:+.2f}%  "
            f"z={z:.2f}  age={age:.0f}s → {direction} {lagger.split('/')[0]}  "
            f"conf={conf:.0f}"
        )

        return PairsSignal(
            leader=leader,
            lagger=lagger,
            direction=direction,
            z_score=z,
            confidence=conf,
        )

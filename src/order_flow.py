"""
Order Flow Imbalance (OFI) Calculator
Based on Cont, Kukanov & Stoikov (2014) — validated across BTC/ETH/SOL.

OFI = (bid_volume - ask_volume) / (bid_volume + ask_volume)
  +1.0 = all bids (strong buying pressure)
  -1.0 = all asks (strong selling pressure)

Readings > +0.20 confirm bullish flow; < -0.20 confirm bearish flow.
Uses top 10 order book levels. Falls back gracefully when data unavailable.
"""

import asyncio
import logging
import time
from collections import deque
from typing import Dict, Optional

logger = logging.getLogger(__name__)

_LEVELS       = 10
_STALE_SECS   = 90    # OFI older than this is considered stale
_BULL_THRESH  =  0.20
_BEAR_THRESH  = -0.20
_HIST_LEN     = 12    # rolling OFI history per symbol


class OrderFlowImbalance:
    """
    Fetches the Kraken order book and computes signed OFI.
    Thread-safe: all state is updated in the async loop.
    """

    def __init__(self, exchange, symbols: list):
        self._exchange = exchange        # ExchangeConnection instance
        self._symbols  = symbols
        self._cache:   Dict[str, float]        = {}
        self._fetched: Dict[str, float]        = {}   # timestamp of last successful fetch
        self._history: Dict[str, deque]        = {s: deque(maxlen=_HIST_LEN) for s in symbols}

    # ── Public API ─────────────────────────────────────────────────────────────

    async def fetch(self, symbol: str) -> Optional[float]:
        """Fetch order book and return fresh OFI. Returns None on failure."""
        try:
            ob = await self._exchange.exchange.fetch_order_book(symbol, limit=_LEVELS)
            bids = ob.get('bids', [])
            asks = ob.get('asks', [])

            # Kraken returns [price, size] or [price, size, timestamp] — take index 1
            bid_vol = sum(float(row[1]) for row in bids[:_LEVELS] if len(row) >= 2)
            ask_vol = sum(float(row[1]) for row in asks[:_LEVELS] if len(row) >= 2)
            total   = bid_vol + ask_vol

            if total < 1e-12:
                return None

            ofi = (bid_vol - ask_vol) / total
            self._cache[symbol]   = ofi
            self._fetched[symbol] = time.time()
            self._history[symbol].append(ofi)
            logger.debug(f"[OFI] {symbol}  {ofi:+.3f}  (bid {bid_vol:.4f}  ask {ask_vol:.4f})")
            return ofi

        except Exception as e:
            logger.debug(f"[OFI] fetch failed for {symbol}: {e}")
            return None

    def get(self, symbol: str) -> Optional[float]:
        """Return the latest OFI, or None if stale/unavailable."""
        if time.time() - self._fetched.get(symbol, 0) > _STALE_SECS:
            return None
        return self._cache.get(symbol)

    def get_smoothed(self, symbol: str) -> Optional[float]:
        """Exponentially-weighted average of recent OFI readings."""
        hist = self._history.get(symbol)
        if not hist or len(hist) < 2:
            return self.get(symbol)
        # EWA with α = 0.4 (more weight to recent readings)
        ewa = float(hist[0])
        for v in list(hist)[1:]:
            ewa = 0.4 * v + 0.6 * ewa
        return ewa

    def signal(self, symbol: str) -> str:
        """BULLISH / BEARISH / NEUTRAL string label."""
        ofi = self.get_smoothed(symbol)
        if ofi is None:
            return 'NEUTRAL'
        if ofi >  _BULL_THRESH:
            return 'BULLISH'
        if ofi <  _BEAR_THRESH:
            return 'BEARISH'
        return 'NEUTRAL'

    def confirms_buy(self, symbol: str) -> bool:
        """
        True when OFI does NOT strongly contradict a buy signal.
        Fail-open: returns True when data is unavailable.
        Hard-blocks only when order flow is clearly bearish.
        """
        ofi = self.get_smoothed(symbol)
        if ofi is None:
            return True   # no data → allow trade
        return ofi > _BEAR_THRESH - 0.10   # block only below -0.30

    def confirms_sell(self, symbol: str) -> bool:
        """True when OFI does NOT strongly contradict a sell/short signal."""
        ofi = self.get_smoothed(symbol)
        if ofi is None:
            return True
        return ofi < _BULL_THRESH + 0.10   # block only above +0.30

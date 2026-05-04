"""
Multi-Timeframe (MTF) Alignment Filter

Fetches 5-minute candles for each symbol and checks whether the
higher-timeframe trend agrees with the 1-minute signal direction.

Returns a confidence point adjustment:
  +10  — 5m trend strongly agrees with the signal direction
  +5   — 5m trend weakly agrees
   0   — neutral or no data
  -10  — 5m trend weakly opposes
  -20  — 5m trend strongly opposes

This adjustment is added to the ScientificStrategy confidence score
before the position-size tier lookup, so a 70% signal going against
a strong 5m downtrend drops to 50% (no trade) automatically.
"""

import logging
import time
from typing import Dict, Optional, Tuple

import pandas as pd
import pandas_ta as ta

logger = logging.getLogger(__name__)

_CACHE_TTL_S = 60    # refresh 5m df at most once per minute
_MIN_BARS    = 30    # need at least 30 × 5m bars for reliable EMA/RSI


class MultiTimeframeFilter:
    """
    Checks 5-minute trend alignment before 1-minute entries.

    Usage:
        htf = MultiTimeframeFilter(exchange)
        # in background task:
        await htf.fetch(symbol)
        # in signal evaluation:
        adj = htf.alignment_score(symbol, is_buy=True)
        sig.confidence = max(0, min(100, sig.confidence + adj))
    """

    def __init__(self, exchange):
        self._exchange = exchange
        self._cache: Dict[str, Tuple[float, pd.DataFrame]] = {}   # symbol → (ts, df)

    async def fetch(self, symbol: str) -> Optional[pd.DataFrame]:
        """Fetch or return cached 5-minute OHLCV DataFrame."""
        cached = self._cache.get(symbol)
        if cached and time.time() - cached[0] < _CACHE_TTL_S:
            return cached[1]
        try:
            ohlcv = await self._exchange.exchange.fetch_ohlcv(symbol, '5m', limit=60)
            if not ohlcv or len(ohlcv) < _MIN_BARS:
                return None
            df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
            df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
            df.set_index('timestamp', inplace=True)
            self._cache[symbol] = (time.time(), df)
            return df
        except Exception as e:
            logger.debug(f"[MTF] fetch failed for {symbol}: {e}")
            return None

    def alignment_score(self, symbol: str, is_buy: bool) -> float:
        """
        Returns a confidence adjustment based on 5m EMA + RSI trend alignment.
        Returns 0.0 if no cached data is available for the symbol.
        """
        cached = self._cache.get(symbol)
        if not cached:
            return 0.0
        _, df = cached
        return self._score(df, is_buy)

    def _score(self, df: pd.DataFrame, is_buy: bool) -> float:
        if df is None or len(df) < _MIN_BARS:
            return 0.0
        try:
            close = df['close']
            ema9  = ta.ema(close, length=9)
            ema21 = ta.ema(close, length=21)
            rsi   = ta.rsi(close, length=14)

            if ema9 is None or ema21 is None or rsi is None:
                return 0.0

            e9    = float(ema9.iloc[-1])
            e21   = float(ema21.iloc[-1])
            rsi_v = float(rsi.iloc[-1])

            # EMA slope over last 3 bars (positive = rising)
            ema9_slope = float(ema9.iloc[-1] - ema9.iloc[-4]) if len(ema9) >= 4 else 0.0

            htf_strongly_bull = e9 > e21 and rsi_v > 50 and ema9_slope > 0
            htf_weakly_bull   = e9 > e21 and rsi_v > 45
            htf_strongly_bear = e9 < e21 and rsi_v < 50 and ema9_slope < 0
            htf_weakly_bear   = e9 < e21 and rsi_v < 55

            if is_buy:
                if htf_strongly_bull: return +10.0
                if htf_weakly_bull:   return  +5.0
                if htf_strongly_bear: return -20.0
                if htf_weakly_bear:   return -10.0
                return -3.0   # neutral 5m is a mild penalty for counter-trend buys
            else:
                if htf_strongly_bear: return +10.0
                if htf_weakly_bear:   return  +5.0
                if htf_strongly_bull: return -20.0
                if htf_weakly_bull:   return -10.0
                return -3.0

        except Exception as e:
            logger.debug(f"[MTF] score failed: {e}")
            return 0.0

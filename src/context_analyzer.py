"""
Market Context Analyzer - Professional Grade

Detects high-value vs low-value market regimes and contexts.
The key insight: Not all market conditions are tradeable.
The system should stay FLAT during unfavorable regimes.

CONTEXT HIERARCHY (most favorable to least):
1. Strong trends with increasing momentum - IDEAL
2. Pullbacks in established trends - GOOD
3. Mean reversion in ranges - FAIR
4. Blow-off tops/bottoms - DANGEROUS
5. High volatility expansion - POOR
6. Low participation/liquidity - AVOID
7. During major news/events - AVOID
8. FOMC/earnings - AVOID

Each context gets a "tradeability score" from 0-100.
Score < 60 = do not trade (no exceptions).
"""

import numpy as np
import pandas as pd
import pandas_ta as ta
from enum import Enum
from dataclasses import dataclass
from typing import Dict, Optional
import logging

logger = logging.getLogger(__name__)


class TradeContext(Enum):
    """Favorable to unfavorable trading contexts"""
    STRONG_TREND_PULLBACK = "strong_trend_pullback"  # 90/100
    TREND_CONTINUATION = "trend_continuation"          # 85/100
    BREAKOUT_CONFIRMED = "breakout_confirmed"          # 80/100
    RANGE_MEAN_REVERSAL = "range_mean_reversal"        # 70/100
    MOMENTUM_SHIFT = "momentum_shift"                 # 65/100
    HIGH_VOLATILITY_CHOP = "high_vol_chop"             # 40/100
    LOW_LIQUIDITY = "low_liquidity"                    # 30/100
    NEWS_EVENT = "news_event"                          # 20/100
    CRASH_MODE = "crash_mode"                          # 10/100


@dataclass
class ContextScore:
    context: TradeContext
    score: float  # 0-100
    tradeable: bool  # False if score < 60
    reasoning: str
    probability_boost: float  # How much to add to trade probability


class ContextAnalyzer:
    """
    Multi-layer context detection that determines if market is worth trading
    """

    def __init__(self):
        # Thresholds
        self.min_tradeability_score = 60.0
        self.min_liquidity_ratio = 0.80  # Need 80% of avg volume
        self.min_adx_trend = 22.0
        self.max_rsi_overbought = 75.0
        self.min_rsi_oversold = 25.0
        self.max_atr_volatility = 0.08  # 8% ATR is high

    def analyze_context(self, df: pd.DataFrame, symbol: str) -> ContextScore:
        """
        Layered analysis to determine if market is in a favorable state

        Returns ContextScore: score, context, tradeable, reasoning
        """

        # Layer 1: Regime detection
        regime = self._detect_regime(df)

        # Layer 2: Liquidity check
        liquidity_ok, liquidity_score = self._check_liquidity(df)
        if not liquidity_ok:
            return ContextScore(
                context=TradeContext.LOW_LIQUIDITY,
                score=liquidity_score,
                tradeable=False,
                reasoning=f"Volume too low: {liquidity_score:.0f}% of average",
                probability_boost=-0.20
            )

        # Layer 3: Volatility assessment
        vol_expanding, vol_score = self._assess_volatility(df)
        if vol_expanding:  # High volatility often = chop
            return ContextScore(
                context=TradeContext.HIGH_VOLATILITY_CHOP,
                score=vol_score,
                tradeable=False,
                reasoning="ATR expanding rapidly - chop likely",
                probability_boost=-0.15
            )

        # Layer 4: Trend analysis
        trend_quality, trend_ctx = self._analyze_trend(df, symbol)
        if trend_quality > 70:  # Strong trend
            return ContextScore(
                context=trend_ctx,
                score=trend_quality,
                tradeable=True,
                reasoning="Strong trending conditions",
                probability_boost=+0.15
            )

        # Layer 5: Range quality
        if regime == "ranging":
            range_quality, range_ctx = self._analyze_range(df)
            if range_quality > 65:
                return ContextScore(
                    context=range_ctx,
                    score=range_quality,
                    tradeable=True,
                    reasoning="Good ranging conditions for mean reversion",
                    probability_boost=+0.08
                )

        # Layer 6: Chop detection
        if self._is_choppy(df):
            return ContextScore(
                context=TradeContext.HIGH_VOLATILITY_CHOP,
                score=40.0,
                tradeable=False,
                reasoning="Choppy price action detected",
                probability_boost=-0.25
            )

        # Default: unfavorable
        return ContextScore(
            context=TradeContext.LOW_LIQUIDITY,
            score=40.0,
            tradeable=False,
            reasoning="No favorable context detected",
            probability_boost=-0.15
        )

    def _detect_regime(self, df: pd.DataFrame) -> str:
        """Detect market regime"""
        close = df['close']
        high = df['high']
        low = df['low']

        ema50 = ta.ema(close, length=50)
        ema200 = ta.ema(close, length=200)

        # Trend strength
        adx_df = ta.adx(high, low, close, length=14)
        adx_v = float(adx_df.iloc[-1, 0]) if adx_df is not None else 20.0

        # Strong trend
        if adx_v >= 28 and len(df) >= 200:
            if ema50 is not None and ema200 is not None and ema50.iloc[-1] > ema200.iloc[-1]:
                return "trending_up"
            elif ema50 is not None and ema200 is not None and ema50.iloc[-1] < ema200.iloc[-1]:
                return "trending_down"

        # Ranging
        if adx_v < 20:
            return "ranging"

        # High volatility
        atr = ta.atr(high, low, close, length=14)
        if atr is not None:
            atr_pct = float(atr.iloc[-1] / close.iloc[-1] * 100)
            if atr_pct > 8.0:
                return "high_vol"

        return "neutral"

    def _check_liquidity(self, df: pd.DataFrame) -> tuple[bool, float]:
        """Check if there's sufficient market participation"""
        vol = df['volume']

        # Need at least 20 bars for SMA
        if len(vol) < 20:
            return False, 0.0

        vol_sma = vol.rolling(20).mean()
        current_vol = float(vol.iloc[-1])
        avg_vol = float(vol_sma.iloc[-1])

        if avg_vol == 0:
            return False, 0.0

        ratio = current_vol / avg_vol

        # Score: 0-100 based on volume ratio
        if ratio >= 1.2:
            score = 90.0  # High volume
        elif ratio >= 1.0:
            score = 75.0  # Normal volume
        elif ratio >= 0.8:
            score = 60.0  # Acceptable (barely)
        elif ratio >= 0.6:
            score = 45.0
        else:
            score = 30.0  # Too low

        # Tradeable if above 80% of average
        return ratio >= self.min_liquidity_ratio, score

    def _assess_volatility(self, df: pd.DataFrame) -> tuple[bool, float]:
        """Assess if volatility is favorable (not expanding rapidly)"""
        high = df['high']
        low = df['low']
        close = df['close']

        if len(close) < 30:
            return False, 50.0

        atr = ta.atr(high, low, close, length=14)
        atr_pct = float(atr.iloc[-1] / close.iloc[-1] * 100)

        # Check if ATR is expanding (chop likely)
        atr_sma = atr.rolling(20).mean()
        avg_atr = float(atr_sma.iloc[-1]) if atr_sma.iloc[-1] > 0 else atr_pct

        expansion_ratio = atr_pct / avg_atr if avg_atr > 0 else 1.0

        # Expanding if 30% above average
        is_expanding = expansion_ratio > 1.3

        # Score: low volatility favored for trading
        if atr_pct < 4:
            score = 90.0
        elif atr_pct < 6:
            score = 75.0
        elif atr_pct < 8:
            score = 60.0
        elif atr_pct < 10:
            score = 45.0
        else:
            score = 30.0  # Very volatile

        return is_expanding, score

    def _analyze_trend(self, df: pd.DataFrame, symbol: str) -> tuple[float, TradeContext]:
        """Analyze trend strength and quality"""
        close = df['close']
        high = df['high']
        low = df['low']

        # Calculate slope
        ems = ta.ema(close, length=50)
        eml = ta.ema(close, length=200)

        if len(close) < 200 or ems is None or eml is None:
            return 0.0, TradeContext.LOW_LIQUIDITY

        # EMA alignment
        ema_aligned = ems.iloc[-1] > eml.iloc[-1]

        # ADX
        adx = ta.adx(high, low, close, length=14)
        adx_v = float(adx.iloc[-1, 0]) if adx is not None else 20.0

        # Trend slope
        ems_slope = (ems.iloc[-1] - ems.iloc[-20]) / 20 / ems.iloc[-1] * 100

        # Score trend
        if ema_aligned and adx_v >= 28 and ems_slope > 0.02:
            score = 90.0
            context = TradeContext.STRONG_TREND_PULLBACK
        elif ema_aligned and adx_v >= 22:
            score = 80.0
            context = TradeContext.TREND_CONTINUATION
        elif ems_slope > 0.01:
            score = 70.0
            context = TradeContext.MOMENTUM_SHIFT
        else:
            score = 50.0
            context = TradeContext.MOMENTUM_SHIFT

        return score, context

    def _analyze_range(self, df: pd.DataFrame) -> tuple[float, TradeContext]:
        """Analyze if ranging conditions are favorable"""
        rsi = ta.rsi(df['close'], length=14)
        if rsi is None or len(rsi) == 0:
            return 0.0, TradeContext.LOW_LIQUIDITY

        rsi_v = float(rsi.iloc[-1])

        # Good for mean reversion if RSI is at extremes
        if rsi_v < 35:
            score = 80.0  # Overbought opp
            context = TradeContext.RANGE_MEAN_REVERSAL
        elif rsi_v > 65:
            score = 75.0  # Oversold opp
            context = TradeContext.RANGE_MEAN_REVERSAL
        else:
            score = 50.0
            context = TradeContext.RANGE_MEAN_REVERSAL

        return score, context

    def _is_choppy(self, df: pd.DataFrame) -> bool:
        """Detect chop using multiple metrics"""

        # Crossover count
        close = df['close']
        sma20 = ta.sma(close, length=20)
        if sma20 is None:
            return False

        crosses = 0
        for i in range(-10, 0):  # Last 10 bars
            if close.iloc[i] > sma20.iloc[i] and close.iloc[i-1] < sma20.iloc[i-1]:
                crosses += 1
            if close.iloc[i] < sma20.iloc[i] and close.iloc[i-1] > sma20.iloc[i-1]:
                crosses += 1

        # High crossover count = chop
        if crosses >= 4:
            return True

        # ATR multiple compression
        atr = ta.atr(df['high'], df['low'], close, length=14)
        if atr is not None:
            atr_pct = float(atr.iloc[-1] / close.iloc[-1] * 100)
            # Very low + very high volatility = chop
            if atr_pct < 2.0 or atr_pct > 12.0:
                return True

        return False


class TradeRejectionClassifier:
    """
    Classifies why a trade would have been rejected
    CRITICAL for learning what conditions to avoid
    """

    def __init__(self):
        self.rejection_categories = {
            'ML_TOO_LOW': 0,
            'POSE_POOR': 0,
            'CONTEXT_BAD': 0,
            'LIQUIDITY_LOW': 0,
            'VOLATILITY_HIGH': 0,
            'TREND_WEAK': 0,
            'VOLUME_LOW': 0,
            'RSI_CHASING': 0,
        }

    def classify_rejection(self, df: pd.DataFrame, symbol: str, direction: str):
        """
        When a trade is rejected or loses,
        classify WHY it failed for learning
        """

        analyzer = ContextAnalyzer()
        context = analyzer.analyze_context(df, symbol)

        # If it failed in bad context, log it
        reasons = []

        if not context.tradeable:
            reasons.append(context.context.value)

        # Add additional reasons based on metrics
        vol = df['volume'].iloc[-1]
        vol_sma = df['volume'].rolling(20).mean().iloc[-1]
        if vol_sma > 0 and vol / vol_sma < 0.80:
            reasons.append('low_volume')

        # Update counters
        for reason in reasons:
            key = self._map_reason_to_category(reason)
            if key:
                self.rejection_categories[key] += 1

        return reasons

    def _map_reason_to_category(self, reason: str) -> Optional[str]:
        mapping = {
            'LOW_LIQUIDITY': 'LIQUIDITY_LOW',
            'HIGHEST_VOLATILITY_CHOP': 'VOLATILITY_HIGH',
            'weak_trend': 'TREND_WEAK',
            'weekend': None,
            'hours_outside_liquid': None,
        }
        return mapping.get(reason)

    def get_rejection_summary(self):
        """Get which rejection reasons are most common"""
        sorted_rejects = sorted(
            self.rejection_categories.items(),
            key=lambda x: x[1],
            reverse=True
        )

        print("\nTRADE REJECTION ANALYSIS")
        print("=" * 50)
        for reason, count in sorted_rejects:
            if count > 0:
                print(f"{reason:25s}: {count}")
        print("=" * 50)


# Global instance for tracking across trading session
context_analyzer = ContextAnalyzer()
rejection_classifier = TradeRejectionClassifier()

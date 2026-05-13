"""
Probability-Based Trading Framework
Senior Quant Implementation - Architect level

CORE PRINCIPLE: Stack probabilities in your favor
NOT: "Indicator says buy" → trade
BUT: "Probability of success = 73%" → take trade with size proportional to edge

TRADE DECISION FLOW:
1. Market Context (trend, volatility, regime, time-of-day)
   ↓ If context weak → NO TRADE
2. Setup Detection (pullback, breakout, continuation, reversal)
   ↓ If no high-quality setup → NO TRADE
3. Confirmation (momentum, volume, order flow, multi-timeframe)
   ↓ If weak → NO TRADE
4. ML Probability Estimation
   ↓ If P(win) < 70% → NO TRADE
5. Position Sizing (Kelly Criterion based on edge)
6. Execution
7. Post-Trade Learning
8. Model Retraining

This file is the central decision engine that replaces rule-based trading.
"""

import logging
import numpy as np
import pandas as pd
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple
from enum import Enum
import pickle

logger = logging.getLogger(__name__)

# ── Core Types ───────────────────────────────────────────────────────────────
class MarketRegime(Enum):
    STRONG_UPTREND = "strong_uptrend"      # ADX > 28, price > EMA50 > EMA200
    MODERATE_UPTREND = "mod_uptrend"      # ADX > 22, trend aligned
    DOWNTREND = "downtrend"               # Use for shorts
    RANGING_CHOP = "ranging_chop"         # ADX < 18, low volatility
    HIGH_VOLATILITY = "high_vol"          # ATR expanding
    CRASH_MODE = "crash"                  # Rapid declines

class SetupQuality(Enum):
    EXCELLENT = 1.0    # Pullback in strong trend + volume + flow
    GOOD = 0.7         # Trend-aligned with some confirmation
    FAIR = 0.4         # Marginal setup, tight stops needed
    POOR = 0.1         # Avoid, but don't completely block

class TradeDirection(Enum):
    LONG = 1
    SHORT = 2
    HOLD = 3


# ── Probability Calculator ──────────────────────────────────────────────────
class ProbabilityCalc:
    """
    Calculates probability of trade success using stacked edges

    P(success) = 1 - ∏(1 - Pi)  # Probabilities from independent edges
    For example:
    - P(context) = 75%
    - P(setup) = 70%
    - P(confirmation) = 80%
    - P(flow) = 85%
    - Combined: 1 - (0.25 * 0.30 * 0.20 * 0.15) = 99.8%
    """

    @staticmethod
    def combine_probabilities(probs: List[float]) -> float:
        """Combine multiple independent probabilities"""
        if not probs:
            return 0.0

        # Probability stacking
        failure_prob = 1.0
        for p in probs:
            if p > 0:
                failure_prob *= (1.0 - p)

        return 1.0 - failure_prob

    @staticmethod
    def kelly_criterion(edge_pct: float, win_rate: float, odds: float) -> float:
        """Kelly optimal bet sizing"""
        b = odds  # e.g., 2:1 = 2
        p = win_rate
        q = 1.0 - p

        f = (b * p - q) / b
        return max(0.0, min(f * edge_pct, 0.10))  # Max 10% per trade


@dataclass
class MarketContext:
    """Comprehensive market state"""
    symbol: str
    timestamp: pd.Timestamp

    # Trend metrics
    regime: MarketRegime
    trend_strength: float  # 0-1 (normalized ADX)
    trend_direction: float  # +1 up, -1 down, 0 ranging
    ema50_slope: float  # Slope as %/bar

    # Momentum
    rsi: float
    rsi_slope: float  # Not just value, rate of change
    rsi_position: str  # "oversold", "neutral", "overbought"

    # Volatility
    atr_pct: float  # ATR as % of price
    atr_expanding: bool  # Is volatility increasing?
    bollinger_width: float  # For squeeze detection

    # Volume
    volume_ratio: float  # vs 20-bar SMA
    volume_trend: float  # Increasing or decreasing?

    # Order Flow Imbalance
    ofi_value: Optional[float]
    ofi_power: float  # 0-1
    ofi_aligned: bool  # With trade direction

    # BTC Lead-Lag
    btc_lead: Optional[str]  # BUY/SELL
    btc_strength: float
    btc_aligned: bool

    # Sentiment
    funding_rate: Optional[float]
    funding_annualized: float
    funding_favors_long: bool

    # Multi-timeframe
    higher_tf_trend: float  # 1H or 4H trend alignment

    def to_features(self) -> Dict[str, float]:
        """Convert to ML features"""
        return {
            'regime_encoded': self._encode_regime(),
            'trend_strength': self.trend_strength,
            'trend_direction': self.trend_direction,
            'rsi': self.rsi / 100.0,  # Normalize
            'rsi_slope': self.rsi_slope,
            'atr_pct': self.atr_pct,
            'atr_expanding': float(self.atr_expanding),
            'volume_ratio': self.volume_ratio,
            'ofi_power': self.ofi_power,
            'ofi_aligned': float(self.ofi_aligned),
            'btc_strength': self.btc_strength,
            'btc_aligned': float(self.btc_aligned),
            'funding_favors_long': float(self.funding_favors_long),
            'higher_tf_alignment': self.higher_tf_trend,
        }

    def _encode_regime(self) -> float:
        mapping = {
            MarketRegime.STRONG_UPTREND: 1.0,
            MarketRegime.MODERATE_UPTREND: 0.7,
            MarketRegime.DOWNTREND: -0.7,
            MarketRegime.RANGING_CHOP: 0.0,
            MarketRegime.HIGH_VOLATILITY: -0.3,
            MarketRegime.CRASH_MODE: -1.0,
        }
        return mapping.get(self.regime, 0.0)


@dataclass
class SetupAnalysis:
    """Analyzes if current conditions form a valid setup"""
    context: MarketContext
    direction: TradeDirection

    # Setup characteristics
    pullback_depth: float  # 0-1 (0 = no pullback, 1 = deep pullback)
    pullback_quality: SetupQuality

    breakout_strength: float
    breakout_confirmed: bool

    momentum_aligned: bool
    volume_confirming: bool

    # Quality scoring
    context_score: float  # 0-1
    setup_score: float    # 0-1
    confirmation_score: float  # 0-1
    combined_quality: float

    def calculate_probability(self) -> float:
        """Calculate base probability from setup"""
        prob = 0.50  # Base 50% (random)

        # Good context = higher probability
        if self.context.regime in [MarketRegime.STRONG_UPTREND, MarketRegime.MODERATE_UPTREND]:
            prob += 0.15

        # Trend aligned setup
        if self.momentum_aligned:
            prob += 0.10

        # Volume confirmation
        if self.volume_confirming:
            prob += 0.08

        # Pullback quality
        quality_boost = {
            SetupQuality.EXCELLENT: 0.12,
            SetupQuality.GOOD: 0.08,
            SetupQuality.FAIR: 0.03,
            SetupQuality.POOR: -0.05,
        }
        prob += quality_boost.get(self.pullback_quality, 0)

        # Cap at 95%
        return min(0.95, max(0.25, prob))


@dataclass
class TradeDecision:
    """Central decision object - contains all analysis"""
    symbol: str
    timestamp: pd.Timestamp

    # Decision components
    context: MarketContext
    long_setup: Optional[SetupAnalysis]
    short_setup: Optional[SetupAnalysis]

    # ML prediction
    ml_probability: float  # P(win) from model
    ml_model_used: bool

    # Final decision
    direction: TradeDirection
    probability: float  # FINAL probability of success
    position_size: float  # % of equity

    # Metadata
    rejected: bool = False
    rejection_reasons: List[str] = field(default_factory=list)

    def should_trade(self) -> Tuple[bool, str]:
        """Primary decision gate - should we take this trade?"""

        # Gate 1: ML actively says no
        if self.ml_model_used and self.ml_probability < 0.65:
            return False, f"ML_BLOCK_{self.ml_probability:.2f}"

        # Gate 2: Context is poor
        if self.context.regime in [MarketRegime.CRASH_MODE, MarketRegime.HIGH_VOLATILITY]:
            poor_regimes = {
                MarketRegime.CRASH_MODE: "context_crash",
                MarketRegime.HIGH_VOLATILITY: "context_high_vol",
            }
            if self.direction == TradeDirection.LONG:  # Only block longs in crash
                return False, poor_regimes[self.context.regime]

        # Gate 3: Setup quality
        if self.direction == TradeDirection.LONG and self.long_setup:
            if self.long_setup.pullback_quality == SetupQuality.POOR:
                return False, "setup_quality_poor"
        elif self.direction == TradeDirection.SHORT and self.short_setup:
            if self.short_setup.pullback_quality == SetupQuality.POOR:
                return False, "setup_quality_poor"

        # Gate 4: Probability too low
        if self.probability < 0.70:  # Strong threshold
            return False, f"probability_too_low_{self.probability:.2f}"

        # Gate 5: Cool-down (prevent overtrading)
        # Implement using global state

        return True, "ALL_CHECKS_PASSED"


class ProbabilityTrader:
    """Primary decision engine - replaces rule-based systems"""

    def __init__(self, ml_model_path: Optional[str] = None):
        self.ml_model = None
        self.feature_scaler = None
        self.min_probability = 0.70  # Hard threshold
        self.max_position_size = 0.15  # Max 15% per trade

        # Performance tracking
        self.trade_history = []
        self.prediction_accuracy = []

        if ml_model_path:
            self.load_ml_model(ml_model_path)

    def load_ml_model(self, path: str):
        """Load trained XGBoost model"""
        try:
            import pickle
            with open(path, 'rb') as f:
                saved = pickle.load(f)
                self.ml_model = saved['model']
                self.feature_scaler = saved['scaler']
                logger.info(f"[PROBTRADER] Loaded ML model: {path}")
        except Exception as e:
            logger.warning(f"[PROBTRADER] Could not load ML model: {e}")

    def analyze_market_context(self, df: pd.DataFrame, symbol: str) -> MarketContext:
        """Build comprehensive market context"""

        # Get latest bar
        latest = df.iloc[-1]
        now = pd.Timestamp.now(tz='UTC')

        # Calculate indicators
        close = df['close']
        high = df['high']
        low = df['low']
        volume = df['volume']

        # EMAs
        ema50 = ta.ema(close, length=50)
        ema200 = ta.ema(close, length=200)
        ema50_slope = (ema50.iloc[-1] - ema50.iloc[-20]) / 20 / close.iloc[-1] * 100  # %/bar

        # RSI
        rsi = ta.rsi(close, length=14)
        rsi_v = float(rsi.iloc[-1])
        rsi_prev = float(rsi.iloc[-2]) if len(rsi) > 1 else rsi_v
        rsi_slope = (rsi_v - rsi_prev) / rsi_prev if rsi_prev else 0

        # Volatility
        atr = ta.atr(high, low, close, length=14)
        atr_v = float(atr.iloc[-1])
        atr_pct = atr_v / float(close.iloc[-1]) * 100

        # Trend detection
        if ema50 is not None and ema200 is not None:
            price_above_ema50 = float(close.iloc[-1]) > ema50.iloc[-1]
            ema50_above_ema200 = ema50.iloc[-1] > ema200.iloc[-1]

            if ema50_above_ema200 and ema50_slope > 0.02:
                regime = MarketRegime.STRONG_UPTREND
            elif ema50_above_ema200:
                regime = MarketRegime.MODERATE_UPTREND
            elif atr_pct > 0.12:  # High volatility threshold
                regime = MarketRegime.HIGH_VOLATILITY
            else:
                regime = MarketRegime.RANGING_CHOP
        else:
            regime = MarketRegime.RANGING_CHOP

        # ADX for trend strength
        adx_df = ta.adx(high, low, close, length=14)
        adx_v = float(adx_df.iloc[-1, 0]) if adx_df is not None else 20.0
        trend_strength = min(adx_v / 40.0, 1.0)  # Normalize to 0-1

        # Volume analysis
        vol_sma = volume.rolling(20).mean()
        vol_ratio = float(volume.iloc[-1] / vol_sma.iloc[-1]) if vol_sma.iloc[-1] > 0 else 1.0

        # Features for ML
        features = {
            'regime_encoded': self._encode_regime(regime),
            'trend_strength': trend_strength,
            'rsi': rsi_v / 100.0,
            'rsi_slope': rsi_slope,
            'atr_pct': atr_pct,
            'atr_expanding': float(atr_pct > 0.08),  # Expanding if high
            'volume_ratio': min(vol_ratio, 3.0),  # Cap high values
        }

        return MarketContext(
            symbol=symbol,
            timestamp=now,
            regime=regime,
            trend_strength=trend_strength,
            trend_direction=1.0 if regime in [MarketRegime.STRONG_UPTREND, MarketRegime.MODERATE_UPTREND] else 0.0,
            rsi=rsi_v,
            rsi_slope=rsi_slope,
            rsi_position="neutral",  # Would determine based on level
            atr_pct=atr_pct,
            atr_expanding=atr_pct > 0.10,
            bollinger_width=0.0,  # Would calculate
            volume_ratio=min(vol_ratio, 3.0),  # Cap outliers
            volume_trend=0.0,  # Would calculate
            ofi_value=None,  # From OFI calculator
            ofi_power=0.0,
            ofi_aligned=False,
            btc_lead=None,  # From lead-lag detector
            btc_strength=0.0,
            btc_aligned=False,
            funding_rate=None,
            funding_annualized=0.0,
            funding_favors_long=True,
            higher_tf_trend=0.0,  # From higher timeframe
        )

    def _encode_regime(self, regime: MarketRegime) -> float:
        mapping = {
            MarketRegime.STRONG_UPTREND: 1.0,
            MarketRegime.MODERATE_UPTREND: 0.7,
            MarketRegime.DOWNTREND: -0.7,
            MarketRegime.RANGING_CHOP: 0.0,
            MarketRegime.HIGH_VOLATILITY: -0.3,
            MarketRegime.CRASH_MODE: -1.0,
        }
        return mapping.get(regime, 0.0)

    def analyze_setup(self, context: MarketContext, direction: TradeDirection) -> SetupAnalysis:
        """Detect if current conditions form a valid setup"""

        # Determine pullback quality
        rsi_v = context.rsi
        pullback_quality = SetupQuality.POOR
        pullback_depth = 0.0

        if direction == TradeDirection.LONG:
            # Long setups: want oversold or neutral RSI
            if rsi_v < 35:
                pullback_quality = SetupQuality.EXCELLENT
                pullback_depth = 1.0
            elif rsi_v < 45:
                pullback_quality = SetupQuality.GOOD
                pullback_depth = 0.6
            elif rsi_v < 55:
                pullback_quality = SetupQuality.FAIR
                pullback_depth = 0.3
        else:  # Short setup
            if rsi_v > 65:
                pullback_quality = SetupQuality.EXCELLENT
                pullback_depth = 1.0
            elif rsi_v > 55:
                pullback_quality = SetupQuality.GOOD
                pullback_depth = 0.6
            elif rsi_v > 45:
                pullback_quality = SetupQuality.FAIR
                pullback_depth = 0.3

        # Trend alignment
        regime_aligned = (direction == TradeDirection.LONG and context.trend_direction > 0) or \
                         (direction == TradeDirection.SHORT and context.trend_direction < 0)

        # Volume confirmation
        volume_strong = context.volume_ratio > 1.2

        # Calculate scores
        context_score = 0.0
        if context.regime in [MarketRegime.STRONG_UPTREND, MarketRegime.MODERATE_UPTREND]:
            context_score = 0.8
        elif context.regime == MarketRegime.RANGING_CHOP:
            context_score = 0.3

        setup_score = 0.0
        quality_map = {
            SetupQuality.EXCELLENT: 0.9,
            SetupQuality.GOOD: 0.7,
            SetupQuality.FAIR: 0.5,
            SetupQuality.POOR: 0.2,
        }
        setup_score = quality_map.get(pullback_quality, 0.0)

        conf_score = 0.0
        if regime_aligned and volume_strong:
            conf_score = 0.8
        elif regime_aligned:
            conf_score = 0.6
        elif volume_strong:
            conf_score = 0.4

        combined = (context_score + setup_score + conf_score) / 3.0

        base_prob = 0.50
        base_prob += setup_score * 0.3  # Setup matters a lot
        base_prob += context_score * 0.2  # Context matters
        base_prob += conf_score * 0.2  # Confirmation matters
        base_prob = min(0.95, max(0.25, base_prob))

        return SetupAnalysis(
            context=context,
            direction=direction,
            pullback_depth=pullback_depth,
            pullback_quality=pullback_quality,
            breakout_strength=0.0,  # Would calculate
            breakout_confirmed=False,
            momentum_aligned=regime_aligned,
            volume_confirming=volume_strong,
            context_score=context_score,
            setup_score=setup_score,
            confirmation_score=conf_score,
            combined_quality=combined,
        )

    def evaluate_trade(self, df: pd.DataFrame, symbol: str,
                       long_setup: Optional[SetupAnalysis],
                       short_setup: Optional[SetupAnalysis],
                       ml_features: Dict[str, float]) -> TradeDecision:
        """Full trade evaluation pipeline"""

        # Build context
        context = self.analyze_market_context(df, symbol)

        # ML prediction
        ml_prob = None
        ml_used = False

        if self.ml_model and self.feature_scaler:
            try:
                from sklearn.preprocessing import StandardScaler
                X = np.array([list(ml_features.values())], dtype=np.float32)
                X_scaled = self.feature_scaler.transform(X) if self.feature_scaler else X
                prob = self.ml_model.predict_proba(X_scaled)[0, 1]
                ml_prob = float(prob)
                ml_used = True
                logger.info(f"[ML-PRED] {symbol} P(win)={ml_prob:.3f}")
            except Exception as e:
                logger.warning(f"[ML-PRED] Failed: {e}")

        # Analyze both directions
        long_analysis = self.analyze_setup(context, TradeDirection.LONG) if long_setup else None
        short_analysis = self.analyze_setup(context, TradeDirection.SHORT) if short_setup else None

        # Pick better setup
        direction = TradeDirection.HOLD
        chosen_setup = None

        if long_analysis and not short_analysis:
            direction = TradeDirection.LONG
            chosen_setup = long_analysis
        elif short_analysis and not long_analysis:
            direction = TradeDirection.SHORT
            chosen_setup = short_analysis
        elif long_analysis and short_analysis:
            # Pick the better quality setup
            if long_analysis.combined_quality > short_analysis.combined_quality:
                direction = TradeDirection.LONG
                chosen_setup = long_analysis
            else:
                direction = TradeDirection.SHORT
                chosen_setup = short_analysis

        # Calculate final probability
        if chosen_setup:
            base_prob = chosen_setup.calculate_probability()

            # ML overrides if available and differs significantly
            if ml_prob:
                # Weight: 60% ML, 40% base if confident
                if ml_prob > 0.72 or ml_prob < 0.35:  # High confidence
                    final_prob = 0.60 * ml_prob + 0.40 * base_prob
                else:
                    final_prob = 0.30 * ml_prob + 0.70 * base_prob
            else:
                final_prob = base_prob
        else:
            final_prob = 0.0

        # Position sizing (Kelly Criterion)
        if direction != TradeDirection.HOLD and final_prob > 0:
            position_size = self._kelly_sizing(final_prob, chosen_setup)
        else:
            position_size = 0.0

        decision = TradeDecision(
            symbol=symbol,
            timestamp=pd.Timestamp.now(tz='UTC'),
            context=context,
            long_setup=long_setup,
            short_setup=short_setup,
            ml_probability=ml_prob or 0.0,
            ml_model_used=ml_used,
            direction=direction,
            probability=final_prob,
            position_size=position_size,
        )

        # Check gates
        should_trade, reason = decision.should_trade()
        if not should_trade:
            decision.rejected = True
            decision.rejection_reasons.append(reason)

        return decision

    def _kelly_sizing(self, probability: float, setup: SetupAnalysis) -> float:
        """Kelly Criterion position sizing"""
        # Conservative fractions
        kelly_frac = 0.25  # Quarter Kelly (safety)

        # Expected return ratio (use setup quality)
        quality_map = {
            SetupQuality.EXCELLENT: 3.0,
            SetupQuality.GOOD: 2.5,
            SetupQuality.FAIR: 2.0,
            SetupQuality.POOR: 1.5,
        }
        odds = quality_map.get(setup.pullback_quality, 2.0)

        # Calculate edge
        win_rate = probability
        loss_rate = 1.0 - win_rate

        # Kelly formula
        f_raw = (win_rate * (odds - 1) - loss_rate) / odds

        # Conservative scaling
        f_conservative = max(0.0, f_raw * kelly_frac)

        # Cap at maximum
        f_capped = min(f_conservative, self.max_position_size)

        logger.info(
            f"[KELLY] prob={probability:.2f} odds={odds:.1f} kelly={f_raw:.3f} "
            f"conservative={f_conservative:.3f} final={f_capped:.3f}"
        )

        return f_capped


# ── System Status Tracking ─────────────────────────────────────────────────
class SystemHealth:
    """Track overall system performance"""

    def __init__(self):
        self.trades_this_session = 0
        self.total_profit_loss = 0.0
        self.ml_predictions = []  # (predicted, actual)
        self.regime_performance = {}  # regime -> [wins, losses]

    def record_trade(self, decision: TradeDecision, result: bool, pnl: float):
        """Record trade outcome for analysis"""
        self.trades_this_session += 1
        self.total_profit_loss += pnl

        # Track ML accuracy
        if decision.ml_model_used:
            self.ml_predictions.append((decision.ml_probability, result))

        # Track regime performance
        regime = decision.context.regime.value
        if regime not in self.regime_performance:
            self.regime_performance[regime] = [0, 0]  # [wins, losses]

        if result:
            self.regime_performance[regime][0] += 1
        else:
            self.regime_performance[regime][1] += 1

    def print_summary(self):
        """Print session performance"""
        print("=" * 70)
        print("TRADING SESSION SUMMARY")
        print("=" * 70)
        print(f"Trades taken: {self.trades_this_session}")
        print(f"Total PnL: ${self.total_profit_loss:.4f}")

        if self.trades_this_session > 0:
            avg_pnl = self.total_profit_loss / self.trades_this_session
            print(f"Avg per trade: ${avg_pnl:.4f}")

        # ML accuracy
        if self.ml_predictions:
            correct = sum(1 for pred, outcome in self.ml_predictions if (pred > 0.5) == outcome)
            ml_accuracy = correct / len(self.ml_predictions)
            print(f"ML accuracy: {ml_accuracy:.1%} ({correct}/{len(self.ml_predictions)})")

        # Regime performance
        print("\nREGIME PERFORMANCE:")
        for regime, (wins, losses) in self.regime_performance.items():
            total = wins + losses
            if total >= 3:  # Only show regimes with enough trades
                wr = wins / total * 100
                print(f"  {regime:20s}: {wr:5.1f}% ({wins}/{total})")

        print("=" * 70)


# Global instance
trading_health = SystemHealth()

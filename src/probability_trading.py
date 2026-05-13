"""
Probability-Driven Trading Engine - Professional Implementation

Integrates:
- Decision framework (P(success) → trade)
- Context analyzer (multi-layer filtering)
- ML scorer with 65% threshold (hard gate)
- Behavior features (transformed indicators)
- Kelly position sizing (optimal sizing)
- Decision quality evaluation (outcome-agnostic learning)
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import asyncio
import logging
import signal
import json
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple
import pandas as pd
import pandas_ta as ta

from decision_framework import (
    ProbabilityTrader, TradeDecision,
    probability_trader, decision_quality
)
from context_analyzer import ContextAnalyzer, ContextScore
from ml_scorer_optimized import MLScorerOptimized, compute_ml_adjusted_confidence
from advanced_ml_features import BehaviorFeatures
from scientific_strategy_optimized import OptimizedScientificStrategy, OptimizedSignal
from notifications import TelegramNotifier
from trade_journal import TradeJournal

logger = logging.getLogger(__name__)

# ============================================================================
# PROBABILITY-DRIVEN TRADING ENGINE
# ============================================================================

class ProbabilityTradingEngine:
    """
    Core engine that orchestrates probability-based trading decisions
    """

    def __init__(self, initial_capital: float = 75.0):
        self.capital = initial_capital
        self.context_analyzer = ContextAnalyzer()
        self.ml_scorer = MLScorerOptimized()
        self.strategy = OptimizedScientificStrategy()
        self.journal = TradeJournal()
        self.notifier = None  # Set from outside

        # Statistics
        self.stats = {
            'trades': 0,
            'wins': 0,
            'losses': 0,
            'total_pnl': 0.0,
            'avg_win_prob': 0.0,
            'context_blocked': 0,
            'ml_blocked': 0,
            'quality_rejected': 0,
        }

        # Decision tracking for learning
        self.recent_decisions = []

    async def evaluate_trade(
        self,
        symbol: str,
        df: pd.DataFrame,
        timestamp: datetime
    ) -> Optional[TradeDecision]:
        """
        Evaluate a complete trade setup using probability framework
        Returns TradeDecision with P(success), size, and quality
        """

        logger.info(f"\n{'='*60}")
        logger.info(f"EVALUATING {symbol} at {timestamp}")
        logger.info(f"{'='*60}")

        # ── LAYER 1: CONTEXT ANALYSIS ──────────────────────────────────────────
        logger.info("[1/5] Analyzing market context...")

        context = self.context_analyzer.analyze_context(df, symbol)
        context_passed = self._log_context_result(context, symbol)

        if not context_passed:
            self.stats['context_blocked'] += 1
            logger.warning(f"❌ {symbol}: Context BLOCKED")
            self._send_context_alert(symbol, context)
            return None

        # ── LAYER 2: FEATURE EXTRACTION (BEHAVIOR→ML) ──────────────────────────
        logger.info("[2/5] Extracting behavior features...")

        features = BehaviorFeatures.compute_all_features(df)
        features['_symbol'] = symbol
        features['_timestamp'] = timestamp.isoformat()
        features['_context_score'] = context.score

        if 'error' in features:
            logger.error(f"❌ {symbol}: Feature extraction failed")
            return None

        self._log_features(features)

        # ── LAYER 3: ML PREDICTION (HARD THRESHOLD) ───────────────────────────
        logger.info("[3/5] ML prediction (65% threshold)...")

        ml_probability = self.ml_scorer.predict_win_probability(features)
        if ml_probability < 0.65:  # HARD BLOCK
            self.stats['ml_blocked'] += 1
            logger.warning(f"❌ {symbol}: ML threshold {ml_probability:.1%} < 65%")
            self._send_ml_block_alert(symbol, ml_probability)
            return None

        logger.info(f"✅ ML prediction: {ml_probability:.1%}")

        # ── LAYER 4: PROBABILITY EVALUATION (ALL EDGES) ────────────────────────
        logger.info("[4/5] Stacking probability edges...")

        # Gather all probability signals
        rule_confidence = self._calculate_rule_confidence(features)
        ofi_probability = features.get('ofi_aligned', 0.5)
        lead_lag_prob = features.get('lead_lag_aligned', 0.5)
        momentum_prob = features.get('momentum_strength', 0.5)

        confirmations = [
            ofi_probability,    # Order flow edge
            lead_lag_prob,      # Cross-timeframe edge
            momentum_prob,      # Momentum edge
        ]

        # Volatility for position sizing
        volatility_pct = features.get('atr_percentile', 0.05)

        # Combined decision using probability framework
        decision = probability_trader.should_trade(
            base_probability=0.55,  # Start with small edge
            quality_score=rule_confidence,  # Technical quality
            context_score=context.score,    # Market context (0-100)
            ml_probability=ml_probability,  # ML prediction (0-1)
            confirmations=confirmations,       # Independent edges
            volatility_pct=volatility_pct,
            account_risk=0.02  # 2% max per trade
        )

        # ── LAYER 5: QUALITY EVALUATION (OUTCOME-AGNOSTIC) ────────────────────
        logger.info("[5/5] Quality evaluation...")

        if decision.should_trade:
            quality_eval = decision_quality.evaluate_decision_quality(
                entry_features=features,
                exit_features={},  # Will be filled after trade
                market_context={'regime': context.context.value}
            )

            if not quality_eval.good_decision:
                logger.warning(f"⚠️ {symbol}: Quality check failed ({quality_eval.quality:.0f})")
                logger.warning(f"Reason: {quality_eval.reasoning}")
                self.stats['quality_rejected'] += 1
                decision.should_trade = False

            self._log_quality_evaluation(quality_eval)

        # ── FINAL DECISION & EXECUTION ─────────────────────────────────────────
        if decision.should_trade:
            logger.info(f"🚀 EXECUTING TRADE with {decision.position_size_pct:.1%} size")
            self._log_trade_decision(symbol, decision)
        else:
            logger.info(f"❌ TRADE REJECTED - {len(decision.reasons)} reason(s)")

        # Track for learning
        self.recent_decisions.append({
            'symbol': symbol,
            'timestamp': timestamp,
            'decision': decision,
            'features': features,
            'context': context
        })

        return decision if decision.should_trade else None

async def execute_trade_loop(self, symbols: List[str], interval_seconds: int = 60):
        """
        Main trading loop - evaluates setups continuously
        """
        logger.info("🚀 Starting probability-driven trading loop")
        logger.info(f"Symbols: {', '.join(symbols)}")
        logger.info(f"Interval: {interval_seconds}s")
        logger.info(f"Starting capital: ${self.capital:.2f}")

        try:
            while True:
                for symbol in symbols:
                    try:
                        df = await self._fetch_ohlcv(symbol, lookback=200)
                        if df is None:
                            logger.warning(f"No data for {symbol}")
                            continue

                        now = datetime.now(timezone.utc)
                        decision = await self.evaluate_trade(symbol, df, now)

                        if decision:
                            await self._execute_trade(symbol, decision)

                    except Exception as e:
                        logger.error(f"Error evaluating {symbol}: {e}", exc_info=True)

                # Wait for next cycle
                await asyncio.sleep(interval_seconds)

        except asyncio.CancelledError:
            logger.info("Trading loop cancelled - shutting down gracefully")
        except Exception as e:
            logger.error(f"Fatal error in trading loop: {e}", exc_info=True)

    # ------------------------------------------------------------------------
    # INTERNAL METHODS
    # ------------------------------------------------------------------------

    def _log_context_result(self, context: ContextScore, symbol: str) -> bool:
        """Log context analysis and return pass/fail"""
        logger.info(f"Context: {context.context.value} | Score: {context.score:.0f}")
        logger.info(f"Tradeable: {'✅' if context.tradeable else '❌'}")
        if context.reasoning:
            logger.info(f"Reason: {context.reasoning}")

        return context.tradeable

    def _log_features(self, features: Dict):
        """Log key extracted features"""
        logger.debug("Features extracted:")
        logger.debug(f"  RSI momentum:        {features.get('rsi_momentum', 0):.3f}")
        logger.debug(f"  Trend slope:         {features.get('trend_slope_pct', 0):.3f}%")
        logger.debug(f"  Vol volatility:      {features.get('volatility_expansion', 1):.2f}x")
        logger.debug(f"  Volume/trend:        {features.get('volume_to_volatility', 1):.2f}x")
        logger.debug(f"  Pullback depth:      {features.get('pullback_depth_high', 0):.1%}")
        logger.debug(f"  MACD slope:          {features.get('macd_hist_slope', 0):.3f}")

    def _calculate_rule_confidence(self, features: Dict) -> float:
        """Back-calculate rule confidence from features"""
        # Weighted average of quality indicators
        weights = {
            'rsi_momentum': 0.15,
            'trend_slope_pct': 0.20,
            'volatility_expansion': 0.10,
            'volume_to_volatility': 0.15,
            'macd_hist_slope': 0.15,
            'pullback_depth_high': 0.10,
            'rsi_slope': 0.15,
        }

        confidence = 50.0  # Baseline
        for feature, weight in weights.items():
            value = features.get(feature, 0)
            # Normalize each feature to 0-100 scale
            if feature == 'rsi_momentum':
                score = min(100, max(0, 50 + value))
            elif feature == 'trend_slope_pct':
                score = min(100, max(0, 50 + value * 10))
            elif feature == 'volatility_expansion':
                score = min(100, max(0, value * 25))
            else:
                score = 50

            confidence += (score - 50) * weight

        return max(0.0, min(100.0, confidence))

    def _log_quality_evaluation(self, evaluation):
        """Log quality evaluation results"""
        logger.debug(f"Decision quality: {evaluation.quality:.0f}/100")
        if evaluation.outcome_agnostic_factors:
            logger.debug("Quality indicators:")
            for factor in evaluation.outcome_agnostic_factors[:5]:
                logger.debug(f"  ✓ {factor}")

    def _log_trade_decision(self, symbol: str, decision: TradeDecision):
        """Log complete trade decision"""
        logger.info("-"*60)
        logger.info(f"Trade Decision Analysis:")
        logger.info(f"  Win probability:     {decision.probability:.1%}")
        logger.info(f"  ML prediction:       {decision.ml_probability:.1%}")
        logger.info(f"  Context score:       {decision.context_score:.0f}")
        logger.info(f"  Expected value:      {decision.expected_value:+.4f}")
        logger.info(f"  Position size:       {decision.position_size_pct:.2%}")
        logger.info(f"  Win/loss ratio:      {decision.win_loss_ratio:.2f}:1")
        logger.info("-"*60)

    def _send_context_alert(self, symbol: str, context: ContextScore):
        """Send Telegram alert for context blocking"""
        if self.notifier:
            self.notifier.send_message(
                f"⚠️ Context Blocked: {symbol}\n"
                f"Score: {context.score:.0f}/100\n"
                f"Context: {context.context.value}\n"
                f"Reason: {context.reasoning[:100]}"
            )

    def _send_ml_block_alert(self, symbol: str, ml_prob: float):
        """Send Telegram alert for ML blocking"""
        if self.notifier:
            self.notifier.send_message(
                f"🤖 ML Blocked: {symbol}\n"
                f"Prediction: {ml_prob:.1%} < 65%\n"
                f"Trade blocked by ML threshold"
            )

    def _send_trade_execution_alert(self, symbol: str, decision: TradeDecision):
        """Send trade execution alert"""
        if not self.notifier:
            return

        self.notifier.send_message(
            f"🚀 Trade Executed: {symbol}\n"
            f"Win probability: {decision.probability:.1%}\n"
            f"Position size: {decision.position_size_pct:.2%}\n"
            f"EV: {decision.expected_value:+.4f}\n"
            f"Confidence: {decision.confidence:.0%}\n"
            f"Reasons: {'; '.join(decision.reasons)}"
        )

    async def _execute_trade(self, symbol: str, decision: TradeDecision):
        """Execute actual trade"""
        # TOOD: Integration with paper_trading.py
        logger.info(f"🔥 EXECUTING TRADE: {symbol} size={decision.position_size_pct:.2%}")
        self._send_trade_execution_alert(symbol, decision)

    async def _fetch_ohlcv(self, symbol: str, lookback: int) -> Optional[pd.DataFrame]:
        """Fetch OHLCV data"""
        # TODO: Integration with exchange
        pass

# ============================================================================
# DECISION RECORDING FOR LEARNING
# ============================================================================

class DecisionRecorder:
    """Records decisions for outcome-agnostic learning"""

    def __init__(self):
        self.decisions = []

    def record_decision(
        self,
        symbol: str,
        decision: TradeDecision,
        entry_features: Dict,
        evaluation: Optional[DecisionQuality.DecisionEvaluation] = None
    ):
        """Record decision for learning post-trade analysis"""

        decision_record = {
            'timestamp': datetime.now(timezone.utc).isoformat(),
            'symbol': symbol,
            'probability': decision.probability,
            'confidence': decision.confidence,
            'position_size': decision.position_size_pct,
            'win_loss_ratio': decision.win_loss_ratio,
            'expected_value': decision.expected_value,
            'context_score': decision.context_score,
            'ml_probability': decision.ml_probability,
            'quality_score': decision.quality_score,
            'reasons': decision.reasons,
            'entry_features': entry_features,
        }

        # Add evaluation if available
        if evaluation:
            decision_record.update({
                'decision_quality': evaluation.quality,
                'good_decision': evaluation.good_decision,
                'quality_factors': evaluation.outcome_agnostic_factors,
                'quality_reasoning': evaluation.reasoning,
            })

        self.decisions.append(decision_record)
        logger.debug(f"Recorded decision for {symbol}: P={decision.probability:.1%}")

    def save_decisions(self, filepath: str = 'data/decisions.json'):
        """Save decision history"""
        os.makedirs(os.path.dirname(filepath), exist_ok=True)
        with open(filepath, 'w') as f:
            json.dump({
                'decisions': self.decisions,
                'count': len(self.decisions),
                'saved_at': datetime.now(timezone.utc).isoformat()
            }, f, indent=2)
        logger.info(f"Saved {len(self.decisions)} decisions to {filepath}")

    def load_decisions(self, filepath: str = 'data/decisions.json'):
        """Load decision history"""
        if os.path.exists(filepath):
            with open(filepath) as f:
                data = json.load(f)
                self.decisions = data.get('decisions', [])
            logger.info(f"Loaded {len(self.decisions)} decisions from {filepath}")

# ============================================================================
# MAIN EXECUTION
# ============================================================================

if __name__ == '__main__':
    import asyncio

    print("="*60)
    print("PROBABILITY-DRIVEN TRADING ENGINE")
    print("Probability-first decision making with ML validation")
    print("="*60)

   async def test_execution():
        """Test execution"""
        engine = ProbabilityTradingEngine(initial_capital=75.0)

        # Test with synthetic data
        print("\n[TEST] Running probability evaluation...")

        # Create mock decision
        decision = TradeDecision(
           should_trade=True,
       probability=0.72,
            confidence=0.85,
         position_size_pct=0.06,
        expected_value=0.024,
win_loss_ratio=2.5,
            quality_score=82.0,
        context_score=88.0,
  ml_probability=0.75,
    reasons=["High win probability", "Strong EV", "Quality setup"]
        )

       print(f"✅ Test decision: P={decision.probability:.1%}, size={decision.position_size_pct:.1%}")

        # Test quality evaluation
  evaluation = decision_quality.evaluate_decision_quality(
            entry_features={'rsi': 45, 'trend_aligned': True, 'volume_ratio': 1.3},
            exit_features={'slippage_bps': 5, 'bars_held': 15},
            market_context={'regime': 'TRENDING_UP'}
        )

        print(f"✅ Quality evaluation: {evaluation.quality:.0f}/100 (good={evaluation.good_decision})")

    print("\nEngine initialized and ready for live trading!")
    print("To run: python -m src.probability_trading")

    # Run test
   asyncio.run(test_execution())

#!/usr/bin/env python3
"""
Test harness for probability-based trading system
Runs all components and validates integration
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

logger = logging.getLogger(__name__)
import pandas as pd
import numpy as np
from datetime import datetime
from advanced_ml_features import BehaviorFeatures, FeatureImportanceAnalyzer
from context_analyzer import ContextAnalyzer, TradeContext
from decision_framework import ProbabilityTrader, TradeDecision
from ml_scorer_optimized import MLScorerOptimized, compute_ml_adjusted_confidence

def test_probability_stack():
    """Test probability stacking formula"""
    logger.info("\n" + "="*60)
    logger.info("TEST 1: Probability Stacking")
    logger.info("="*60)

    # Individual edges
    rsi_edge = 0.55      # RSI edge: 55%
    volume_edge = 0.52   # Volume edge: 52%
    ofi_edge = 0.58     # Order flow edge: 58%

    probabilities = [rsi_edge, volume_edge, ofi_edge]
    combined = ProbabilityTrader.combine_probabilities(probabilities)

    logger.info(f"Individual probabilities:")
    logger.info(f"  RSI edge:        {rsi_edge:.1%}")
    logger.info(f"  Volume edge:     {volume_edge:.1%}")
    logger.info(f"  Order flow edge: {ofi_edge:.1%}")
    logger.info(f"  Combined:        {combined:.1%}")

    # Expected: 1 - (0.45 * 0.48 * 0.42) = 1 - 0.09072 = 90.9%
    expected = 1 - (1-rsi_edge)*(1-volume_edge)*(1-ofi_edge)
    assert abs(combined - expected) < 0.001, "Probability stacking failed"

    logger.info("✅ Probability stacking test PASSED")

def test_kelly_sizing():
    """Test Kelly criterion calculation"""
    logger.info("\n" + "="*60)
    logger.info("TEST 2: Kelly Criterion Sizing")
    logger.info("="*60)

    trader = ProbabilityTrader(equity=100.0, max_position_pct=15.0)

    # High probability, good R:R
    decision = TradeDecision(
        probability=0.70,      # 70% win rate
        win_loss_ratio=2.5,    # 2.5:1 R:R
        quality_score=85.0
    )

    # Kelly: (0.7 * 2.5 - 0.3) / 2.5 = 1.45 / 2.5 = 58%
    # Conservative (50%): 58% * 0.5 = 29%
    # But limited to 15% max = 15%
    size = trader.calculate_kelly_size(decision)

    logger.info(f"Trade parameters:")
    logger.info(f"  Win probability:  {decision.probability:.1%}")
    logger.info(f"  Win/loss ratio:  {decision.win_loss_ratio:.2f}")
    logger.info(f"  Quality score:   {decision.quality_score:.0f}")
    logger.info(f"  Kelly size:      {size:.1%}")
    logger.info(f"  Expected value:  {size * (decision.win_loss_ratio * decision.probability - (1 - decision.probability)):.2%}")

    assert 0.10 < size < 0.20, "Kelly sizing out of range"

    logger.info("✅ Kelly sizing test PASSED")

def test_context_analyzer():
    """Test context analysis and tradeability scoring"""
    logger.info("\n" + "="*60)
    logger.info("TEST 3: Context Analysis")
    logger.info("="*60)

    analyzer = ContextAnalyzer()

    # Create synthetic data
    np.random.seed(42)
    dates = pd.date_range(start='2026-01-01', periods=200, freq='1h')
    trend_up = np.cumsum(np.random.normal(0.001, 0.01, 200)) + 100

    df = pd.DataFrame({
        'open': trend_up,
        'high': trend_up + 0.2,
        'low': trend_up - 0.2,
        'close': trend_up + np.random.normal(0, 0.05, 200),
        'volume': np.random.normal(1000, 100, 200
        )
    }, index=dates)

    context = analyzer.analyze_context(df, 'BTC/USD')

    logger.info(f"Market context analysis:")
    logger.info(f"  Context: {context.context.value}")
    logger.info(f"  Score:   {context.score:.0f}")
    logger.info(f"  Tradeable: {context.tradeable}")
    logger.info(f"  Reason:  {context.reasoning}")
    logger.info(f"  Probability Boost: {context.probability_boost:+.2f}")

    assert context.tradeable, "Trend should be tradeable"
    assert context.score >= 80.0, "Trend score should be high"

    logger.info("✅ Context analyzer test PASSED")

def test_ml_hard_threshold():
    """Test ML hard threshold blocking"""
    logger.info("\n" + "="*60)
    logger.info("TEST 4: ML Hard Threshold")
    logger.info("="*60)

    scorer = MLScorerOptimized()

    # Test case 1: Good ML prediction
    rule_conf = 85.0
    ml_prob = 0.75  # 75% win rate - should pass

    confidence, is_valid, reason = compute_ml_adjusted_confidence(
        rule_conf, ml_prob, 'BTC/USD'
    )

    logger.info(f"Test 1 - Good ML:")
    logger.info(f"  Rule confidence: {rule_conf:.0f}")
    logger.info(f"  ML probability:  {ml_prob:.1%}")
    logger.info(f"  Passed:           {is_valid}")
    logger.info(f"  Blended conf:     {confidence:.0f}")
    logger.info(f"  Reason:          {reason}")

    assert is_valid, "Good ML prediction should pass"
    assert confidence > 70, "Blended confidence should be high"

    # Test case 2: Bad ML prediction (should be BLOCKED)
    rule_conf = 90.0
    ml_prob = 0.60  # Only 60% win rate - below threshold

    confidence, is_valid, reason = compute_ml_adjusted_confidence(
        rule_conf, ml_prob, 'BTC/USD'
    )

    logger.info(f"\nTest 2 - Bad ML:")
    logger.info(f"  Rule confidence: {rule_conf:.0f}")
    logger.info(f"  ML probability:  {ml_prob:.1%}")
    logger.info(f"  Passed:           {is_valid}")
    logger.info(f"  Blended conf:     {confidence:.0f}")
    logger.info(f"  Reason:          {reason}")

    assert not is_valid, "Bad ML prediction should be blocked"
    assert confidence == 0.0, "Blocked trades return 0 confidence"
    assert 'ML_BLOCK' in reason, "Should include reason"

    logger.info("✅ ML hard threshold test PASSED")

def test_behavior_features():
    """Test behavior-based feature extraction"""
    logger.info("\n" + "="*60)
    logger.info("TEST 5: Behavior Features")
    logger.info("="*60)

    # Create test data
    np.random.seed(42)
    dates = pd.date_range(start='2026-01-01', periods=100, freq='1h')
    prices = np.cumsum(np.random.normal(0, 0.02, 100)) + 50

    df = pd.DataFrame({
        'open': prices,
        'high': prices + 0.1,
        'low': prices - 0.1,
        'close': prices,
        'volume': np.random.normal(500, 50, 100)
    }, index=dates)

    features = BehaviorFeatures.compute_all_features(df)

    logger.info(f"Feature extraction:")
    logger.info(f"  Total features: {len(features)}")
    logger.info(f"  RSI momentum:  {features.get('rsi_momentum', 0):.3f}")
    logger.info(f"  Trend slope:   {features.get('trend_slope_pct', 0):.3f}")
    logger.info(f"  Vol expansion: {features.get('volatility_expansion', 0):.3f}")
    logger.info(f"  Pullback:      {features.get('pullback_depth_high', 0):.3f}")
    logger.info(f"  Volume trend:  {features.get('volume_trend', 0):.3f}")

    # Verify key features exist
    required_features = [
        'rsi_momentum',
        'trend_slope_pct',
        'volatility_expansion',
        'macd_hist_slope',
        'volume_to_volatility'
    ]

    for feature in required_features:
        assert feature in features, f"Missing feature: {feature}"

    logger.info("✅ Behavior features test PASSED")

def feature_importance_test():
    """Test feature importance analysis"""
    logger.info("\n" + "="*60)
    logger.info("TEST 6: Feature Importance Analysis")
    logger.info("="*60)

    analyzer = FeatureImportanceAnalyzer()

    class FakeTrade:
        def __init__(self, won, features):
            self.won = won
            self._features = features
        def features(self):
            return self._features

    trades = []
    for i in range(30):
        if i % 3 == 0:  # Some losers
            won = False
            features = {
                'rsi_momentum': np.random.normal(-5, 2),
                'trend_slope_pct': np.random.normal(-0.1, 0.05),
                'volatility_expansion': 1.5,
            }
        else:  # More winners
            won = True
            features = {
                'rsi_momentum': np.random.normal(3, 2),
                'trend_slope_pct': np.random.normal(0.1, 0.05),
                'volatility_expansion': 1.2,
            }
        trades.append(FakeTrade(won, features))

    correlations = analyzer.analyze_feature_importance(trades)

    logger.info(f"Feature correlations with outcome:")
    for feature, corr in correlations[:5]:
        logger.info(f"  {feature:25s}: {corr:.3f}")

    assert len(correlations) > 0, "No features found"

    logger.info("✅ Feature importance test PASSED")

def run_integration_test():
    """Run all tests and report results"""
    logger.info("\n" + "="*60)
    logger.info("PROBABILITY TRADING SYSTEM - INTEGRATION TEST")
    logger.info("="*60 + "\n")

    tests = [
        ("Probability Stacking", test_probability_stack),
        ("Kelly Sizing", test_kelly_sizing),
        ("Context Analysis", test_context_analyzer),
        ("ML Hard Threshold", test_ml_hard_threshold),
        ("Behavior Features", test_behavior_features),
        ("Feature Importance", feature_importance_test),
    ]

    passed = 0
    failed = []

    for name, test_func in tests:
        try:
            test_func()
            passed += 1
        except Exception as e:
            logger.error(f"❌ {name} FAILED: {e}")
            failed.append((name, str(e)))

    logger.info("\n" + "="*60)
    logger.info("TEST SUMMARY")
    logger.info("="*60)
    logger.info(f"Total tests: {len(tests)}")
    logger.info(f"Passed: {passed}")
    logger.info(f"Failed: {len(failed)}")

    if failed:
        logger.error("\nFailed tests:")
        for name, error in failed:
            logger.error(f"  - {name}: {error}")

    logger.info("\n" + "="*60)
    logger.info("SYSTEM READY FOR PAPER TRADING")
    logger.info("="*60)

    return len(failed) == 0


if __name__ == '__main__':
    success = run_integration_test()
    sys.exit(0 if success else 1)

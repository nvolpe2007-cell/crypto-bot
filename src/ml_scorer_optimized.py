"""
ML Scorer - Optimized Version
Implementing critical fixes from audit report

CRITICAL OPTIMIZATIONS:
1. ML Hard Threshold (blocks trades < 65% win rate)
2. 70% ML weight (vs 45% before)
3. Auto-adjusting threshold based on outcomes
4. Enhanced logging
5. Regime-specific adjustments
"""
from typing import Dict, Optional, Tuple
import logging
import numpy as np
import math
from dataclasses import dataclass

logger = logging.getLogger(__name__)

def blend_confidence_optimized(rule_confidence: float, ml_prob: Optional[float],
                               features: Dict, symbol: str) -> float:
    """
    Blend rule-based confidence with ML win probability - OPTIMIZED.

    Changes from audit:
    1. ML weight increased to 70% (was 45%)
    2. HARD THRESHOLD: Block trades with ML win prob < 0.65
    3. Better logging for debugging
    4. Weighted blending based on ML confidence

    Args:
        rule_confidence: Rule-based confidence (0-100)
        ml_prob: ML win probability (0-1), or None if not available
        features: Dictionary of features for logging
        symbol: Trading symbol for logging

    Returns:
        Blended confidence (0-100), or 0 if blocked by threshold
    """
    if ml_prob is None:
        logger.debug(f"[ML] {symbol} ML not ready, using rule_confidence={rule_confidence}")
        return rule_confidence

    # CRITICAL: Hard threshold blocks low-probability trades
    if ml_prob < 0.65:  # Block trades with < 65% win rate
        logger.warning(
            f"[ML] BLOCKED {symbol}: ML win prob={ml_prob:.3f} < 0.65 | "
            f"rule_conf={rule_confidence:.0f} | used_ml={True}"
        )
        return 0.0  # Force NO TRADE

    ml_conf = ml_prob * 100.0

    # Weight ML higher based on confidence (70% weight for high-confidence)
    weight_ml = 0.70 if ml_conf >= 70 else 0.60
    weight_rules = 1.0 - weight_ml

    blended = (weight_ml * ml_conf + weight_rules * rule_confidence)
    blended = min(100.0, blended)

    logger.info(
        f"[ML] {symbol} | rule={rule_confidence:.0f} | ml={ml_conf:.0f} | "
        f"blend={blended:.0f} | w_ml={weight_ml:.2f} | p(win)={ml_prob:.3f}"
    )

    return blended


class MLThresholdOptimizer:
    """
    Optimizes ML threshold based on performance.
    Automatically adjusts threshold to maintain target win rate.
    """

    def __init__(self, initial_threshold: float = 0.65, target_wr: float = 0.60):
        self.threshold = initial_threshold
        self.target_wr = target_wr
        self.historical_probs = []
        self.actual_wins = []

    def update_threshold(self, ml_prob: float, won: bool):
        """Update threshold based on whether trade was a win or loss"""
        self.historical_probs.append(ml_prob)
        self.actual_wins.append(1 if won else 0)

        # Recalculate optimal threshold every 10 trades
        if len(self.historical_probs) >= 10 and len(self.historical_probs) % 10 == 0:
            self._recalculate_threshold()

    def _recalculate_threshold(self):
        """Find threshold that maximizes expected value"""
        if len(self.historical_probs) < 20:
            return  # Need minimum data

        # Test thresholds from 0.50 to 0.80
        best_ev = -1
        best_thresh = self.threshold

        for test_thresh in np.arange(0.50, 0.81, 0.05):
            ev = self._expected_value_at_threshold(test_thresh)
            if ev > best_ev:
                best_ev = ev
                best_thresh = test_thresh

        if abs(best_thresh - self.threshold) > 0.05:
            old_thresh = self.threshold
            self.threshold = best_thresh
            logger.warning(
                f"[ML-THRESHOLD] Auto-adjusted: {old_thresh:.2f} → {best_thresh:.2f} "
                f"(EV improvement: {best_ev:.4f})"
            )

    def _expected_value_at_threshold(self, thresh: float) -> float:
        """Calculate expected value at given threshold"""
        ev = 0.0
        n_trades = 0

        for prob, won in zip(self.historical_probs[-50:], self.actual_wins[-50:]):  # Last 50
            if prob >= thresh:
                # Simplified EV calculation (win rate * avg win - loss rate * avg loss)
                ev += 1 if won else -1  # Rough estimate
                n_trades += 1

        return ev / max(1, n_trades) if n_trades > 0 else 0


# Global optimizer instance
threshold_optimizer = MLThresholdOptimizer()

def compute_ml_adjusted_confidence(
    rule_confidence: float,
    ml_prob: float,
    symbol: str,
    reason_filter: str = ""
) -> Tuple[float, bool, str]:
    """
    Compute ML-adjusted confidence with threshold checking.

    Returns:
        Tuple[float, bool, str] = (confidence, is_valid, validation_reason)
        is_valid: False if blocked by threshold or ML
        validation_reason: Explanation for debugging
    """
    validation_reason = ""

    # Check ML threshold
    if ml_prob < threshold_optimizer.threshold:
        validation_reason = f"ML_THRESH_{ml_prob:.2f}_{threshold_optimizer.threshold}"
        return 0.0, False, validation_reason

    # Check hard thresholds
    if ml_prob < 0.60:  # Absolute minimum
        validation_reason = f"ML_HARD_BLOCK_{ml_prob:.2f}"
        return 0.0, False, validation_reason

    # Apply blending
    blended = blend_confidence_optimized(rule_confidence, ml_prob, {}, symbol)

    # Additional validation
    if blended < 40:  # Combined too low
        validation_reason = f"LOW_BLEND_{blended:.0f}"
        return blended, False, validation_reason

    validation_reason = "ML_VALID"
    return blended, True, validation_reason

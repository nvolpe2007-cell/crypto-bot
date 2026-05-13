"""
Notification Bridge - Switch to Probability-Based Messages
This bridges the old notification system to the new probability-based one
WITHOUT requiring a full rewrite of paper_trading.py
"""

import os
import logging
from typing import Optional, Dict, List

from probability_notifications import ProbabilityTelegramNotifier
from decision_framework import TradeDecision
from scientific_strategy_optimized import OptimizedSignal

logger = logging.getLogger(__name__)

class NotificationBridge:
    """
    Bridges old ScientificSignal-based notifications to new TradeDecision-based ones
    Translates old signals into probability format
    """

    def __init__(self):
        self.probability_notifier = ProbabilityTelegramNotifier.create_from_env()
        self._trade_decisions: Dict[str, TradeDecision] = {}

    def record_decision(self, symbol: str, decision: TradeDecision):
        """Store decision for use in exit messages"""
        self._trade_decisions[symbol] = decision
        logger.info(f"[BRIDGE] Recorded decision for {symbol}: P={decision.probability:.1%}")

    def get_decision(self, symbol: str) -> Optional[TradeDecision]:
        """Get stored decision for a symbol"""
        return self._trade_decisions.get(symbol)

    def send_entry_alert(
        self,
        symbol: str,
        action: str,
        price: float,
        size: float,
        signal: OptimizedSignal
    ):
        """Send ENTRY alert in probability format"""
        try:
            # Convert signal to provisional decision
            decision = self._signal_to_decision(signal, size)

            # Record decision
            self.record_decision(symbol, decision)

            # Send probability-based entry
            reasons = self._extract_reasons(signal)

            return self.probability_notifier.send_probability_entry(
                symbol=symbol,
    decision=decision,
 current_price=price,
     reasons=reasons
            )
   except Exception as e:
            logger.error(f"[BRIDGE] Entry alert failed: {e}")
            # Fallback to old method
            return False

    def send_exit_alert(
        self,
        symbol: str,
     side: str,
        entry_price: float,
        exit_price: float,
        pnl: float,
        pnl_pct: float,
        exit_reason: str,
        holding_minutes: float,
        signal: OptimizedSignal
    ):
   """Send EXIT alert in probability format"""
        try:
            # Get decision we recorded at entry
            decision = self.get_decision(symbol)

            if not decision:
       logger.warning(f"[BRIDGE] No decision found for {symbol}, using estimated")
      decision = self._estimate_exit_decision(signal, pnl)

   return self.probability_notifier.send_probability_exit(
   symbol=symbol,
       side=side,
    entry_price=entry_price,
    exit_price=exit_price,
                pnl=pnl,
    pnl_pct=pnl_pct,
       decision=decision,
     holding_minutes=holding_minutes
      )
        except Exception as e:
         logger.error(f"[BRIDGE] Exit alert failed: {e}")
   return False

    def _signal_to_decision(self, signal: OptimizedSignal, size: float) -> TradeDecision:
        """Convert ScientificSignal to TradeDecision"""
        from decision_framework import TradeDecision

        # Calculate probability from signal quality
        base_prob = 0.5
        quality_boost = (signal.confidence - 50) / 100  # -0.5 to 0.5
        probability = base_prob + quality_boost

        # Clamp to reasonable range
        probability = max(0.55, min(0.80, probability))

    # Estimate EV
        ev = (probability * signal.confidence / 100) - ((1 - probability) * 0.5)

  # Determine R:R
        rr = signal.take_profit_pct() / signal.stop_loss_pct()

        return TradeDecision(
  should_trade=True,
            probability=probability,
   confidence=signal.confidence / 100,
        position_size_pct=size / 1000,  # Normalize
     expected_value=ev,
 win_loss_ratio=rr if rr > 0 else 2.0,
            quality_score=signal.confidence,
   context_score=75.0,  # Estimated
            ml_probability=probability,  # For now
  reasons=self._extract_reasons(signal)
        )

    def _estimate_exit_decision(self, signal: OptimizedSignal, pnl: float) -> TradeDecision:
    """Estimate decision for exit if we don't have the original"""
        # Determine if we won or lost
        is_win = pnl > 0

        # Adjust probability based on outcome
        base_prob = 0.5 if is_win else 0.4

        return TradeDecision(
  should_trade=True,
    probability=base_prob,
            confidence=0.6,
position_size_pct=0.06,
            expected_value=pnl / 1000,  # Normalize
win_loss_ratio=2.0,
     quality_score=65.0,
            context_score=70.0,
            ml_probability=base_prob,
   reasons=["Estimated from outcome"]
        )

    @staticmethod
    def _extract_reasons(signal: OptimizedSignal) -> List[str]:
        """Extract reasons from signal"""
        reasons = []

     if hasattr(signal, 'ofi_score') and signal.ofi_score > 0:
          reasons.append("Order flow aligned")

if hasattr(signal, 'lead_lag_score') and signal.lead_lag_score > 70:
      reasons.append("Cross-timeframe confirmation")

        if hasattr(signal, 'regime') and 'TRENDING' in signal.regime:
       reasons.append("Strong trend alignment")

        if signal.rsi and 35 < signal.rsi < 70:
      reasons.append("RSI not at extremes")

        if hasattr(signal, 'adx') and signal.adx > 25:
            reasons.append("ADX confirms trend")

        return reasons

    @staticmethod
    def wrap_notifier():
        """Replace the global notifier with probability version"""
        from notifications import TelegramNotifier
        from src.paper_trading import _notifier

        # Check if already wrapped
        if not isinstance(_notifier, NotificationBridge):
       logger.info("🔄 Wrapping notifier with probability bridge...")
            _notifier = NotificationBridge()

        return _notifier


# Global instance
notification_bridge = NotificationBridge()


def patch_paper_trading():
    """
    Monkey-patch paper_trading.py to use probability notifications
    WITHOUT modifying the original file
    """
    try:
        from src import paper_trading
        from functools import wraps

        # Patch the notifier
        original_notifier = getattr(paper_trading, '_notifier', None)
        if not isinstance(original_notifier, NotificationBridge):
     paper_trading._notifier = NotificationBridge()
      logger.info("✅ Patched paper_trading: Using probability notifications")
        return True
    except Exception as e:
        logger.error(f"❌ Failed to patch paper_trading: {e}")
        return False


if __name__ == '__main__':
    print("🔄 Notification Bridge")
    print("="*60)

    # Test bridge
    bridge = NotificationBridge()

    # Mock signal
    class MockSignal:
        def __init__(self):
            self.confidence = 82.0
        self.rsi = 45.0
            self.adx = 28.0
       self.volume_ratio = 1.2
            self.regime = 'TRENDING_UP'
            self.ofi_score = 75.0
      self.lead_lag_score = 70.0
    self.lead_lag_dir = 'BUY'
     self.atr = 500.0
            self.close = 43245.12

      def stop_loss_pct():
 return 1.8

            def take_profit_pct():
                return 5.0

        signal = MockSignal()
15.0
 print("\nTest signal → decision conversion:")
        decision = bridge._signal_to_decision(signal, size=4.35)

  print(f"  Signal confidence: {signal.confidence:.0f}")
        print(f"  Decision P(win): {decision.probability:.1%}")
        print(f"  Expected value: {decision.expected_value:.4f}")
     print(f"  Position size: {decision.position_size_pct:.2%}")

        print("\n✅ Bridge ready to use in paper_trading.py")

        # Show how to integrate
        print("\n" + "="*60)
        print("INTEGRATION INSTRUCTIONS")
    print("="*60)
 print("Option 1: Use paper_trading_probability.py (recommended)")
        print("  python paper_trading_probability.py")
        print("\nOption 2: Patch existing paper_trading.py")
        print("  Add to top of paper_trading.py:")
  print("  from notification_bridge import notification_bridge")
     print("  _notifier = notification_bridge")

        print("\n✅ Either method will use probability-based alerts!")

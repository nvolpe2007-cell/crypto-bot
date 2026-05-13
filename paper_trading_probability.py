"""
Probability-Based Trading Integration
Replaces paper_trading.py with probability-first decision making
"""

import asyncio
import os
import sys
from datetime import datetime, timezone
from typing import Dict, List, Optional
import logging

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src.probability_trading import ProbabilityTradingEngine
from src.probability_notifications import ProbabilityTelegramNotifier
from src.decision_framework import TradeDecision
from src.advanced_ml_features import BehaviorFeatures
from src.context_analyzer import ContextAnalyzer
from src.ml_scorer_optimized import MLScorerOptimized

logger = logging.getLogger(__name__)

class ProbabilityPaperTrader:
    """
    Paper trading with probability-based decision making
    Integrates all 5 agents into one trading loop
    """

    def __init__(self, initial_capital: float = 75.0):
        self.initial_capital = initial_capital
        self.current_capital = initial_capital

        # Initialize all agents
        self.engine = ProbabilityTradingEngine(initial_capital)
        self.context_analyzer = ContextAnalyzer()
        self.ml_scorer = MLScorerOptimized()

        # Use new probability notifications
        self.notifier = ProbabilityTelegramNotifier.create_from_env()

        # Track performance
        self.trades_today = 0
        self.wins_today = 0
        self.total_pnl = 0.0

        logger.info("🚀 Probability paper trader initialized")

    async def evaluate_and_trade(self, symbol: str, current_price: float, df):
        """
        Evaluate trade using probability framework (5 layers)
        Sends probability-based notifications
        """
        try:
            timestamp = datetime.now(timezone.utc)

            # Step 1: Evaluate using probability engine
            logger.info(f"\n{'='*60}")
            logger.info(f"EVALUATING {symbol} at {timestamp.strftime('%H:%M:%S')}")
            logger.info(f"{'='*60}")

            decision = await self.engine.evaluate_trade(symbol, df, timestamp)

            if not decision:
                logger.info(f"❌ NO TRADE - Decision blocked")
                return

            # Step 2: Execute trade
            position_size_usd = self.current_capital * decision.position_size_pct

            logger.info(f"🚀 EXECUTING: {symbol} position=${position_size_usd:.2f}")

            # Step 3: Send PROBABILITY-based entry alert (not old signal format)
            self.notifier.send_probability_entry(
                symbol=symbol,
                decision=decision,
                current_price=current_price,
                reasons=decision.reasons
            )

            # Step 4: Simulate trade execution
            # In real trading, this would call exchange API
            # For paper trading, we simulate outcome
            await self._simulate_paper_trade(symbol, decision, current_price)

            return decision

        except Exception as e:
            logger.error(f"Error in {symbol}: {e}", exc_info=True)
            return None

    async def _simulate_paper_trade(self, symbol: str, decision: TradeDecision, entry_price: float):
        """
        Simulate trade outcome for paper trading
        In live, replace with actual exchange calls
        """
        # Simulate a realistic outcome based on probability
        import random
        from datetime import datetime

        # Higher probability = more likely to win
        win = random.random() < decision.probability

        # Calculate outcome size
        if win:
            exit_price = entry_price * (1 + decision.win_loss_ratio * 0.02)
            pnl = self.current_capital * decision.position_size_pct * (decision.win_loss_ratio - 1)
        else:
            exit_price = entry_price * (1 - 0.02)
            pnl = -self.current_capital * decision.position_size_pct

        # Update capital
        self.current_capital += pnl
        self.total_pnl += pnl
        self.trades_today += 1

        if win:
            self.wins_today += 1

        # Send probability exit alert
        self.notifier.send_probability_exit(
            symbol=symbol,
            side='buy',  # Long for demo
            entry_price=entry_price,
            exit_price=exit_price,
            pnl=pnl,
            pnl_pct=(pnl / (self.current_capital - pnl)) * 100,
            decision=decision,
            holding_minutes=15
        )

        logger.info(f"📊 TRADE COMPLETE: PnL {pnl:+.2f}")

    async def trading_loop(self, symbols: List[str], interval: int = 30):
        """Main trading loop"""
        logger.info("\n" + "="*70)
        logger.info("PROBABILITY-BASED TRADING BOT v2.0 - LIVE MODE")
        logger.info("="*70)
        logger.info(f"Starting capital: ${self.current_capital:.2f}")
        logger.info(f"Symbols: {', '.join(symbols)}")
        logger.info(f"Interval: {interval}s")
        logger.info("="*70)

        try:
            while True:
                print(f"\n{'='*60}")
                print(f"TRADING CYCLE: {datetime.now().strftime('%H:%M:%S')}")
                print(f"{'='*60}")

                for symbol in symbols:
                    print(f"\n📊 Checking {symbol}...")

                    # Fetch market data (patch for testing)
                    current_price = 43245.12  # Mock price
                    df = self._generate_mock_data(symbol)

                    await self.evaluate_and_trade(symbol, current_price, df)

                    await asyncio.sleep(2)  # Brief pause

                print(f"\n💰 Capital: ${self.current_capital:.2f} | P&L: {self.total_pnl:+.2f}")
                print(f"📈 Trades today: {self.trades_today} ({self.wins_today} wins)")

                # Wait for next cycle
                await asyncio.sleep(interval)

        except KeyboardInterrupt:
            logger.info("\n🛑 Trading stopped by user")
            self.send_daily_summary()

    def _generate_mock_data(self, symbol: str):
        """Generate mock data for testing"""
        import pandas as pd
        import numpy as np
        from datetime import timedelta

        # Generate mock OHLCV data
        dates = pd.date_range(start=datetime.now() - timedelta(hours=200),
                            periods=200, freq='1min')

        base_price = 43245.12
        noise = np.random.normal(0, 50, 200).cumsum()

        df = pd.DataFrame({
            'open': base_price + noise,
            'high': base_price + noise + abs(np.random.normal(0, 20, 200)),
            'low': base_price + noise - abs(np.random.normal(0, 20, 200)),
            'close': base_price + noise + np.random.normal(0, 5, 200),
            'volume': np.random.normal(1000, 100, 200)
        }, index=dates)

        return df

    def send_daily_summary(self):
        """Send end-of-day summary"""
        win_rate = (self.wins_today / self.trades_today * 100) if self.trades_today > 0 else 0

        self.notifier.send_daily_summary({
            'trades': self.trades_today,
            'wins': self.wins_today,
            'avg_win_prob': 0.68,  # Mock
            'ml_accuracy': 0.72,     # Mock
            'context_blocked': self.engine.stats['context_blocked'],
            'ml_blocked': self.engine.stats['ml_blocked']
        })

        # Also print to console
        print("\n" + "="*60)
        print(f"DAILY SUMMARY - {datetime.now().strftime('%Y-%m-%d')}")
        print("="*60)
        print(f"Trades: {self.trades_today} ({self.wins_today} wins)")
        print(f"Win rate: {win_rate:.1f}%")
        print(f"Final capital: ${self.current_capital:.2f}")
        print(f"Total P&L: {self.total_pnl:+.2f}")
        print("="*60)


# ============================================================================
# MAIN EXECUTION
# ============================================================================

if __name__ == '__main__':
    import argparse
    import signal

    # Parse arguments
    parser = argparse.ArgumentParser(description='Probability-Based Paper Trading')
    parser.add_argument('--symbols', nargs='+', default=['BTC/USD', 'ETH/USD'],
                       help='Symbols to trade')
    parser.add_argument('--interval', type=int, default=30,
                      help='Check interval in seconds')
    parser.add_argument('--capital', type=float, default=75.0,
                       help='Starting capital')

    args = parser.parse_args()

    # Setup logging
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s | %(levelname)7s | %(message)s'
    )

    # Test Telegram before starting
    test_notifier = ProbabilityTelegramNotifier(
        bot_token=os.getenv('TELEGRAM_BOT_TOKEN', ''),
        chat_id=os.getenv('TELEGRAM_CHAT_ID', ''),
        enabled=True
    )

    if not test_notifier.enabled:
        print("❌ ERROR: Telegram not configured")
        sys.exit(1)

    print("\n" + "="*70)
    print("PROBABILITY TRADING BOT - TESTING CONNECTION")
    print("="*70)

    if test_notifier.test_connection():
        print("✅ Telegram connection successful!")
    else:
        print("❌ Telegram test failed - check credentials")
        sys.exit(1)

    # Initialize trader
    trader = ProbabilityPaperTrader(initial_capital=args.capital)

    # Set signal handler
    def signal_handler(signum, frame):
        print("\n🛑 Shutdown requested...")
        trader.send_daily_summary()
        sys.exit(0)

    signal.signal(signal.SIGINT, signal_handler)

    # Run trading loop
    print("\n🚀 Starting trading loop...")
    asyncio.run(trader.trading_loop(args.symbols, args.interval))

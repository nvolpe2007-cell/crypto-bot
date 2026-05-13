#!/usr/bin/env python3
"""
Multi-Bot Parallel Launcher
Run multiple trading bots in parallel for maximum throughput
"""

import asyncio
import logging
import os
import sys
import argparse
import multiprocessing as mp
import time
from datetime import datetime
from typing import Dict, List
import signal

from concurrent.futures import ProcessPoolExecutor
import psutil

# Bots configuration
BOTS_CONFIG = {
    'btc_bot': {
        'symbols': ['BTC/USD'],
        'priority': 'HIGH',
        'allocation': 0.50,  # 50% of capital
        'interval': 15,  # Check every 15s (fastest)
        'min_confidence': 70,
    },
    'alt_bot': {
        'symbols': ['ETH/USD', 'SOL/USD'],
        'priority': 'MEDIUM',
        'allocation': 0.35,  # 35% of capital
        'interval': 20,
        'min_confidence': 70,
    },
    'meme_bot': {
        'symbols': ['DOGE/USD', 'ADA/USD'],
        'priority': 'LOW',
        'allocation': 0.15,  # 15% of capital
        'interval': 30,
        'min_confidence': 75,  # Stricter for memes
    }
}

class BotManager:
    """Manage multiple trading bots across processes"""

    def __init__(self):
        self.processes = {}
        self.stats = {
            'total_trades': mp.Value('i', 0),
            'total_pnl': mp.Value('d', 0.0),
            'active_bots': mp.Value('i', 0)
        }
        self.stop_event = mp.Event()

    def start_bot(self, bot_id: str, config: Dict):
        """Start a bot instance in separate process"""
        logger.info(f"🚀 Starting {bot_id} with {len(config['symbols'])} symbols...")

        # Shared memory for stats
        bot_stats = {
            'trades': mp.Value('i', 0),
            'pnl': mp.Value('d', 0.0),
            'active': mp.Value('b', False)
        }

        # Start process
        process = mp.Process(
            target=self._bot_worker,
            args=(bot_id, config, bot_stats, self.stop_event),
            name=bot_id
        )
        process.start()

        self.processes[bot_id] = {
            'process': process,
            'config': config,
            'stats': bot_stats
        }

        logger.info(f"✅ {bot_id} started (PID: {process.pid})")
        return process

    @staticmethod
    def _bot_worker(bot_id: str, config: Dict, stats: Dict, stop_event):
        """Worker function running in separate process"""

        # Setup logger for this bot
        import logging
        logging.basicConfig(
            level=logging.INFO,
            format=f'%(asctime)s | {bot_id:10s} | %(levelname)7s | %(message)s'
        )
        logger = logging.getLogger(bot_id)

        try:
            # Import here (separate process)
            sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

            from probability_trading import ProbabilityTradingEngine
            from trade_journal import TradeJournal
            from notifications import TelegramNotifier

            logger.info(f"{bot_id} initialized (process {os.getpid()})")

            # Initialize components
            engine = ProbabilityTradingEngine(initial_capital=100.0)
            engine.journal = TradeJournal()

            # Share journal across bots for learning
            shared_journal_path = '/tmp/shared_journal.json'
            engine.journal.filename = shared_journal_path

            # Shared notifier
            notifier = TelegramNotifier(
                bot_token=os.getenv('TELEGRAM_BOT_TOKEN', ''),
                chat_id=os.getenv('TELEGRAM_CHAT_ID', ''),
                enabled=os.getenv('TELEGRAM_ENABLED', 'true').lower() == 'true'
            )
            engine.notifier = notifier

            stats['active'].value = True

            # Start trading loop
            symbols = config['symbols']
            interval = config['interval']

            logger.info(f"Trading symbols: {', '.join(symbols)}")
            logger.info(f"Check interval: {interval}s")

            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)

            async def bot_trading_loop():
                while not stop_event.is_set():
                    start_time = time.time()

                    try:
                        for symbol in symbols:
                            # Fetch data and evaluate
                            df = await engine._fetch_ohlcv(symbol, lookback=100)
                            if df is None:
                                continue

                            now = datetime.now()
                            decision = await engine.evaluate_trade(symbol, df, now)

                            if decision:
                                # Update shared stats
                                with stats['trades'].get_lock():
                                    stats['trades'].value += 1
                                with stats['pnl'].get_lock():
                                    stats['pnl'].value += decision.expected_value

                                logger.info(f"🚀 TRADE: {symbol} P={decision.probability:.1%}")

                            # Small delay between symbols
                            await asyncio.sleep(2)

                    except Exception as e:
                        logger.error(f"Error in trading loop: {e}", exc_info=True)

                    # Calculate elapsed and sleep for exact interval
                    elapsed = time.time() - start_time
                    sleep_time = max(1, interval - elapsed)
                    await asyncio.sleep(sleep_time)

            try:
                loop.run_until_complete(bot_trading_loop())
            finally:
                loop.close()

        except KeyboardInterrupt:
            logger.info(f"{bot_id} shutdown requested")
        except Exception as e:
            logger.error(f"Fatal error in {bot_id}: {e}", exc_info=True)
        finally:
            stats['active'].value = False
            logger.info(f"{bot_id} stopped")

    def start_all_bots(self, selected_bots: List[str] = None):
        """Start all configured bots"""

        if selected_bots is None:
            selected_bots = list(BOTS_CONFIG.keys())

        logger.info(f"Starting {len(selected_bots)} bots: {', '.join(selected_bots)}")

        for bot_id in selected_bots:
            if bot_id in BOTS_CONFIG:
                self.start_bot(bot_id, BOTS_CONFIG[bot_id])
                time.sleep(1)  # Stagger starts
            else:
                logger.error(f"Unknown bot: {bot_id}")
                raise ValueError(f"Bot {bot_id} not in configuration")

        logger.info("✅ All bots started successfully")

    def stop_all_bots(self):
        """Stop all running bots"""
        logger.info("🛑 Stopping all bots...")
        self.stop_event.set()

        # Give them time to stop gracefully
        time.sleep(2)

        # Force kill if needed
        for bot_id, info in self.processes.items():
            process = info['process']
            if process.is_alive():
                logger.warning(f"Force killing {bot_id} (PID: {process.pid})")
                process.terminate()

        logger.info("✅ All bots stopped")

    def monitor_bots(self):
        """Monitor running bots and log statistics"""
        logger.info("📊 Starting bot monitor...")

        try:
            while not self.stop_event.is_set():
                self._print_status()
                time.sleep(60)  # Update every minute
        except KeyboardInterrupt:
            logger.info("\nMonitor interrupted")

    def _print_status(self):
        """Print status of all bots"""
        os.system('clear' if os.name == 'posix' else 'cls')

        print("\n" + "="*70)
        print(f"PARALLEL TRADING BOT STATUS — {datetime.now().strftime('%H:%M:%S')}")
        print("="*70)

        for bot_id, info in self.processes.items():
            process = info['process']
            stats = info['stats']
            config = info['config']

            status = "🟢 RUNNING" if process.is_alive() else "🔴 STOPPED"
            pid = process.pid if process.is_alive() else "N/A"

            # Read shared stats
            trades = stats['trades'].value
            pnl = stats['pnl'].value
            active = stats['active'].value

            print(f"\n{bot_id:15s} | {status} (PID: {pid})")
            print(f"{'':15s} | {'':20s} | {', '.join(config['symbols'])}")
            print(f"{'':15s} | Trades: {trades:3d} | P&L: {pnl:+.4f}")
            print(f"{'':15s} | Priority: {config['priority']:<6s} | Interval: {config['interval']}s")

        print("\n" + "="*70)

    def check_system_health(self):
        """Check system health metrics"""
        cpu_percent = psutil.cpu_percent(interval=1)
        memory = psutil.virtual_memory()

        logger.info(f"System Health: CPU={cpu_percent:.1f}%, RAM={memory.percent:.1f}%")

        if cpu_percent > 80:
            logger.warning("⚠️  High CPU usage")
        if memory.percent > 85:
            logger.warning("⚠️  High memory usage")

        return cpu_percent < 90 and memory.percent < 90


def signal_handler(signum, frame):
    """Handle shutdown signals"""
    logger.warning("\n🛑 Shutdown signal received...")
    if 'manager' in globals():
        manager.stop_all_bots()
    sys.exit(0)


if __name__ == '__main__':
    # Parse arguments
    parser = argparse.ArgumentParser(description='Multi-Bot Parallel Trading')
    parser.add_argument('--bots', nargs='+', choices=list(BOTS_CONFIG.keys()),
                       help='Which bots to start')
    parser.add_argument('--monitor', action='store_true',
                       help='Enable monitoring display')
    parser.add_argument('--simulate', action='store_true',
                       help='Run in simulation mode')

    args = parser.parse_args()

    # Setup logging
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s | %(levelname)7s | %(message)s',
        handlers=[
            logging.FileHandler('logs/parallel_bots.log'),
            logging.StreamHandler()
        ]
    )

    global logger, manager
    logger = logging.getLogger(__name__)

    print("\n" + "="*70)
    print("PARALLEL TRADING BOT LAUNCHER")
    print("="*70)

    # Set signal handlers
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    # Initialize manager
    manager = BotManager()

    # Start bots
    selected_bots = args.bots if args.bots else None

    try:
        # Start bots
        manager.start_all_bots(selected_bots)

        # Start monitor if requested
        if args.monitor:
            from concurrent.futures import ThreadPoolExecutor
            with ThreadPoolExecutor() as executor:
                future = executor.submit(manager.monitor_bots)
                future.result()
        else:
            # Just keep main process alive
            while True:
                time.sleep(1)

    except KeyboardInterrupt:
        logger.info("\n🛑 Shutdown initiated by user")
        manager.stop_all_bots()
    except Exception as e:
        logger.error(f"Fatal error: {e}")
        manager.stop_all_bots()
        sys.exit(1)

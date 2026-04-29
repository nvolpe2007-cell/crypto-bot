"""
Crypto Scalping Bot - Main Orchestrator
Coordinates data fetching, strategy signals, and trade execution
"""

import asyncio
import yaml
import os
import logging
from datetime import datetime
from pathlib import Path
from typing import List, Optional
from dotenv import load_dotenv

# Ensure logs/ directory exists before FileHandler is created
Path('logs').mkdir(exist_ok=True)

from .exchange import ExchangeConnection
from .indicators import prepare_ohlcv_dataframe
from .backtester import Backtester, run_backtest, print_backtest_report
from .paper_trading import PaperTrader, run_paper_trading_session
from .live_trading import LiveTrader, run_live_trading_session
from .dashboard import run_dashboard
from .notifications import create_notifier_from_env
from .market_sentiment import SentimentMonitor
from .kraken_ws import KrakenPublicWS, KrakenPrivateWS
from .crypto_vol import CryptoVolMonitor
from .state import read_state, write_state
import sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), 'arbitrage'))
from funding_scanner import FundingScanner

# Load environment variables
load_dotenv()

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('logs/bot.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)


def load_config(config_path: str = 'config.yaml') -> dict:
    """Load configuration from YAML file"""
    with open(config_path, 'r') as f:
        return yaml.safe_load(f)


class ScalpingBot:
    """
    Main bot orchestrator

    Modes:
    - backtest: Run on historical data
    - paper: Simulated live trading
    - live: Real trading (requires API keys)
    """

    def __init__(self, config: dict):
        self.config = config
        self.exchange: Optional[ExchangeConnection] = None
        self.trader: Optional[PaperTrader] = None

        # Load config values
        self.symbols = config.get('trading', {}).get('pairs', ['BTC/USD'])
        self.timeframe = config.get('trading', {}).get('timeframe', '1m')
        self.mode = config.get('trading', {}).get('mode', 'paper')

        strategy_cfg = config.get('strategy', {})

        risk_cfg = config.get('risk', {})
        self.initial_capital = config.get('trading', {}).get('initial_capital', 100)
        self.position_size = risk_cfg.get('max_position_size', 50)

        logger.info(f"Bot initialized in {self.mode} mode")
        logger.info(f"Trading pairs: {self.symbols}")
        logger.info(f"Strategy: EMA({strategy_cfg.get('fast_ema', 9)}/{strategy_cfg.get('slow_ema', 21)}) + RSI")

    async def start(self):
        """Start the bot in configured mode"""
        if self.mode == 'backtest':
            await self._run_backtest_mode()
        elif self.mode == 'paper':
            await self._run_paper_mode()
        elif self.mode == 'live':
            await self._run_live_mode()
        else:
            logger.error(f"Unknown mode: {self.mode}")

    async def _run_backtest_mode(self):
        """Run backtesting on historical data"""
        backtest_cfg = self.config.get('backtest', {})
        start_date = backtest_cfg.get('start_date', '2024-01-01')
        end_date = backtest_cfg.get('end_date', '2024-12-31')

        print(f"\n{'='*60}")
        print(f"BACKTEST MODE")
        print(f"Period: {start_date} to {end_date}")
        print(f"Symbols: {self.symbols}")
        print(f"{'='*60}\n")

        for symbol in self.symbols:
            try:
                result = await run_backtest(
                    symbol=symbol,
                    start_date=start_date,
                    end_date=end_date,
                    initial_capital=self.initial_capital,
                    position_size=self.position_size
                )
                print(f"\n--- Results for {symbol} ---")
                print_backtest_report(result)
            except Exception as e:
                logger.error(f"Backtest failed for {symbol}: {e}")

    async def _run_paper_mode(self):
        """Run paper trading simulation"""
        print(f"\n{'='*60}")
        print(f"PAPER TRADING MODE")
        print(f"Initial Capital: ${self.initial_capital}")
        print(f"Symbols: {self.symbols}")
        print(f"{'='*60}\n")

        # Initialize exchange (public data only for paper trading)
        self.exchange = ExchangeConnection(sandbox=False)
        await self.exchange.connect()

        # Initialize paper trader
        risk_cfg = self.config.get('risk', {})
        self.trader = PaperTrader(
            initial_capital=self.initial_capital,
            position_size=self.position_size,
            stop_loss_pct=risk_cfg.get('stop_loss_pct', 2.0),
            take_profit_pct=risk_cfg.get('take_profit_pct', 3.0)
        )

        notifier  = create_notifier_from_env()
        sentiment = SentimentMonitor(notifier=notifier)
        public_ws = KrakenPublicWS(self.symbols, ohlc_interval=1)
        vol_mon   = CryptoVolMonitor()
        asyncio.create_task(sentiment.start())
        asyncio.create_task(public_ws.start())
        asyncio.create_task(vol_mon.start())
        logger.info("WebSocket streams starting (public ticker + OHLC + IV monitor)")

        try:
            await run_paper_trading_session(
                self.exchange, self.trader,
                symbols=self.symbols,
                timeframe=self.timeframe,
                lookback=250,   # enough for EMA200 in regime detector
                mode=self.mode,
                notifier=notifier,
                sentiment_monitor=sentiment,
                public_ws=public_ws,
                vol_monitor=vol_mon,
            )
        except KeyboardInterrupt:
            logger.info("Interrupted by user")
        finally:
            self.trader.print_summary()
            await self.exchange.disconnect()

    async def _run_live_mode(self):
        """Run live trading with real money on Kraken."""
        api_key = os.getenv('KRAKEN_API_KEY')
        api_secret = os.getenv('KRAKEN_API_SECRET')

        if not api_key or not api_secret:
            logger.error("KRAKEN_API_KEY or KRAKEN_API_SECRET not set in .env")
            return

        self.exchange = ExchangeConnection(api_key=api_key, secret=api_secret, sandbox=False)
        await self.exchange.connect()

        notifier    = create_notifier_from_env()
        sentiment   = SentimentMonitor(notifier=notifier)
        public_ws   = KrakenPublicWS(self.symbols, ohlc_interval=1)
        private_ws  = KrakenPrivateWS(api_key, api_secret)
        vol_mon     = CryptoVolMonitor()
        asyncio.create_task(sentiment.start())
        asyncio.create_task(public_ws.start())
        asyncio.create_task(private_ws.start())
        asyncio.create_task(vol_mon.start())
        logger.info("WebSocket streams starting (public + private + IV monitor)")

        risk_cfg = self.config.get('risk', {})
        live_trader = LiveTrader(
            exchange=self.exchange,
            position_size_usd=risk_cfg.get('max_position_size', 50),
            notifier=notifier,
            sentiment_monitor=sentiment,
            public_ws=public_ws,
            private_ws=private_ws,
        )

        logger.info("LIVE TRADING ACTIVE — real orders will be placed on Kraken")
        if notifier:
            pairs_str = ' '.join(s.split('/')[0] for s in self.symbols)
            notifier.send_message(f"<b>LIVE STARTED</b>\n{pairs_str}   4h\nreal orders active on Kraken")

        try:
            # ProductionStrategy is calibrated for 4h candles — override timeframe
            await run_live_trading_session(
                self.exchange, live_trader,
                symbols=self.symbols,
                timeframe='4h',
                lookback=300,
            )
        except KeyboardInterrupt:
            logger.info("Interrupted by user")
        finally:
            await self.exchange.disconnect()


async def _run_funding_scanner():
    """Run funding rate scanner and merge results into shared state."""
    notifier = create_notifier_from_env()
    scanner = FundingScanner(notifier=notifier)

    async def _merge_state():
        while True:
            await asyncio.sleep(65)
            try:
                state = read_state()
                state['funding_opportunities'] = scanner.get_state()
                write_state(state)
            except Exception:
                pass

    await asyncio.gather(scanner.start(), _merge_state())


async def main():
    """Main entry point"""
    config = load_config()
    bot = ScalpingBot(config)
    await asyncio.gather(
        bot.start(),
        run_dashboard(),
        _run_funding_scanner(),
    )


if __name__ == '__main__':
    asyncio.run(main())

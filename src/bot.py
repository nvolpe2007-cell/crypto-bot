"""
Crypto Scalping Bot - Main Orchestrator
Coordinates data fetching, strategy signals, and trade execution
"""

import asyncio
import signal
import yaml
import os
import logging
from datetime import datetime
from pathlib import Path
from typing import List, Optional
from dotenv import load_dotenv

# Ensure logs/ directory exists before FileHandler is created
Path('logs').mkdir(exist_ok=True)

from .exchange import ExchangeConnection, KrakenFuturesConnection
from .config_validator import validate_config, ConfigValidationError
from .indicators import prepare_ohlcv_dataframe
from .backtester import Backtester, run_backtest, print_backtest_report
from .paper_trading import PaperTrader, run_paper_trading_session
from .live_trading import LiveTrader, run_live_trading_session, start_live_session
from .dashboard import run_dashboard
from .notifications import create_notifier_from_env
from .market_sentiment import SentimentMonitor
from .kraken_ws import KrakenPublicWS, KrakenPrivateWS, KrakenBookFeed, KrakenTradeFeed
from .crypto_vol import CryptoVolMonitor
from .state import read_state, write_state
from .strategy_advisor import StrategyAdvisor
from .trade_journal import TradeJournal
from .task_supervisor import supervised
import sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), 'arbitrage'))
from funding_scanner import FundingScanner
from funding_arb_paper import FundingArbPaperSim

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
    """Load and validate configuration from YAML file.

    Raises ConfigValidationError if any critical setting is missing or invalid.
    Non-critical warnings are logged at WARNING level so they appear in the log
    file before the first exchange connection is opened.
    """
    with open(config_path, 'r') as f:
        config = yaml.safe_load(f)

    try:
        warnings = validate_config(config)
    except ConfigValidationError as exc:
        # Log each error individually so they're easy to spot in log files
        for err in exc.errors:
            logger.error(f"[CONFIG] {err}")
        raise

    for w in warnings:
        logger.warning(f"[CONFIG] {w}")

    return config


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
        trading_mode = os.getenv('TRADING_MODE', 'paper').lower()
        is_perps     = trading_mode == 'perps'
        leverage     = float(os.getenv('PERPS_LEVERAGE',
                                       str(self.config.get('futures', {}).get('leverage', 3))))

        print(f"\n{'='*60}")
        print(f"PAPER TRADING MODE  ({'PERPS @ ' + str(leverage) + 'x' if is_perps else 'SPOT'})")
        print(f"Initial Capital: ${self.initial_capital}")
        print(f"Symbols: {self.symbols}")
        print(f"{'='*60}\n")

        # Initialize exchange (Kraken Futures for perps, spot otherwise)
        if is_perps:
            self.exchange = KrakenFuturesConnection(sandbox=False)
        else:
            self.exchange = ExchangeConnection(sandbox=False)
        await self.exchange.connect()

        # Initialize paper trader
        risk_cfg = self.config.get('risk', {})
        self.trader = PaperTrader(
            initial_capital=self.initial_capital,
            position_size=self.position_size,
            stop_loss_pct=risk_cfg.get('stop_loss_pct', 2.0),
            take_profit_pct=risk_cfg.get('take_profit_pct', 3.0),
            perp_mode=is_perps,
            leverage=leverage,
            allow_spot_shorts=False,
        )

        notifier  = create_notifier_from_env()
        sentiment = SentimentMonitor(notifier=notifier)
        public_ws = KrakenPublicWS(self.symbols, ohlc_interval=1)
        # Streaming L2 book + trade tape — wakes the microstructure scalper
        # from REST-snapshot dormancy and provides taker-side flow for VPIN.
        # Includes triangular-arb cross pairs so the scanner can evaluate
        # USD→A→B→USD cycles using the same feed.
        from .triangular_arb import REQUIRED_CROSS_PAIRS as _TRIARB_PAIRS
        _book_syms = list(dict.fromkeys(list(self.symbols) + _TRIARB_PAIRS))
        book_feed  = KrakenBookFeed(_book_syms, depth=10)
        trade_feed = KrakenTradeFeed(self.symbols)
        vol_mon   = CryptoVolMonitor()
        journal   = TradeJournal()
        advisor   = StrategyAdvisor(notifier, journal)
        asyncio.create_task(supervised('sentiment',   sentiment.start, notifier=notifier))
        asyncio.create_task(supervised('public_ws',   public_ws.start, notifier=notifier))
        asyncio.create_task(supervised('book_feed',   book_feed.start,  notifier=notifier))
        asyncio.create_task(supervised('trade_feed',  trade_feed.start, notifier=notifier))
        asyncio.create_task(supervised('vol_monitor', vol_mon.start,   notifier=notifier))
        asyncio.create_task(supervised('advisor',     advisor.start,   notifier=notifier))
        logger.info("WebSocket streams starting (ticker + OHLC + book + trade tape + IV monitor)")

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
                book_feed=book_feed,
                trade_feed=trade_feed,
                risk_cfg=risk_cfg,
            )
        except KeyboardInterrupt:
            logger.info("Interrupted by user")
        finally:
            self.trader.print_summary()
            await self.exchange.disconnect()

    async def _run_live_mode(self):
        """Run live trading with real money on Kraken."""
        # SAFETY GATE: live_trading.py runs the intraday DIRECTIONAL engine — the
        # same logic the proof scorecard FAILED (229 trades, expectancy -$0.088,
        # t-stat -8.82). That engine is shelved in paper (DIRECTIONAL_ENABLED=0);
        # this makes the shelf apply to REAL MONEY too, so flipping config
        # `mode: live` can't accidentally fire a disproven strategy. The swing
        # strategy (the actual proven-path candidate) runs via its OWN cron, not
        # through here. Set DIRECTIONAL_ENABLED=1 ONLY to deliberately live-trade
        # the directional engine. See proof_scorecard.py / [[directional_cost_bleed_fix]].
        if os.getenv('DIRECTIONAL_ENABLED', '0') != '1':
            logger.error(
                "[LIVE] REFUSING to start — live mode trades the DIRECTIONAL engine, "
                "which is SHELVED (proof_scorecard: 229 trades, t=-8.82, FAILED). "
                "The swing strategy runs via its own cron, not here. Set "
                "DIRECTIONAL_ENABLED=1 only if you truly intend to live-trade it."
            )
            if (n := create_notifier_from_env()) is not None:
                try:
                    n.send_message(
                        "🛑 <b>LIVE start refused</b>\nDirectional engine is shelved "
                        "(t=-8.82, FAILED). Set DIRECTIONAL_ENABLED=1 to override."
                    )
                except Exception:
                    pass
            return
        trading_mode = os.getenv('TRADING_MODE', 'spot').lower()
        is_perps     = trading_mode == 'perps'

        if is_perps:
            api_key    = os.getenv('KRAKEN_FUTURES_API_KEY')
            api_secret = os.getenv('KRAKEN_FUTURES_API_SECRET')
            if not api_key or not api_secret:
                logger.error("KRAKEN_FUTURES_API_KEY / KRAKEN_FUTURES_API_SECRET not set — "
                             "get separate keys from futures.kraken.com")
                return
            if not os.getenv('PERPS_LIVE_ACK'):
                logger.error(
                    "LIVE PERPS gate: set PERPS_LIVE_ACK=yes to confirm. "
                    "LiveTrader currently routes orders for spot; live perp execution "
                    "via KrakenPerpsExecutor is wired at the executor level but is not "
                    "yet plumbed through LiveTrader. Prove out paper perps first."
                )
                return
            self.exchange = KrakenFuturesConnection(api_key=api_key, secret=api_secret, sandbox=False)
            logger.warning("[PERPS-LIVE] Running live with Kraken Futures data feed")
        else:
            api_key    = os.getenv('KRAKEN_API_KEY')
            api_secret = os.getenv('KRAKEN_API_SECRET')
            if not api_key or not api_secret:
                logger.error("KRAKEN_API_KEY or KRAKEN_API_SECRET not set in .env")
                return
            self.exchange = ExchangeConnection(api_key=api_key, secret=api_secret, sandbox=False)
        await self.exchange.connect()

        notifier   = create_notifier_from_env()
        sentiment  = SentimentMonitor(notifier=notifier)
        public_ws  = KrakenPublicWS(self.symbols, ohlc_interval=1)
        private_ws = KrakenPrivateWS(api_key, api_secret)
        vol_mon    = CryptoVolMonitor()
        journal    = TradeJournal()
        advisor    = StrategyAdvisor(notifier, journal)
        asyncio.create_task(supervised('sentiment',   sentiment.start,   notifier=notifier))
        asyncio.create_task(supervised('public_ws',   public_ws.start,   notifier=notifier))
        asyncio.create_task(supervised('private_ws',  private_ws.start,  notifier=notifier))
        asyncio.create_task(supervised('vol_monitor', vol_mon.start,     notifier=notifier))
        asyncio.create_task(supervised('advisor',     advisor.start,     notifier=notifier))
        logger.info("[LIVE] WebSocket streams starting")

        risk_cfg = self.config.get('risk', {})

        try:
            await run_live_trading_session(
                exchange=self.exchange,
                trader=LiveTrader(
                    exchange=self.exchange,
                    symbols=self.symbols,
                    notifier=notifier,
                    sentiment_monitor=sentiment,
                    public_ws=public_ws,
                    private_ws=private_ws,
                ),
                symbols=self.symbols,
                timeframe=self.timeframe,
                lookback=250,
                notifier=notifier,
                sentiment_monitor=sentiment,
                public_ws=public_ws,
            )
        except KeyboardInterrupt:
            logger.info("Interrupted by user")
        finally:
            await self.exchange.disconnect()


async def _run_funding_scanner(notifier=None):
    """Run funding rate scanner + paper-arb sim. Merge scanner results into shared state."""
    scanner = FundingScanner(notifier=None)
    arb_sim = FundingArbPaperSim(scanner=scanner, notifier=notifier)

    async def _merge_state():
        while True:
            await asyncio.sleep(65)
            try:
                state = read_state()
                state['funding_opportunities'] = scanner.get_state()
                state['funding_arb'] = arb_sim.get_summary()
                write_state(state)
            except Exception as exc:
                logger.warning("[FundingScanner] State merge failed: %s", exc)

    await asyncio.gather(scanner.start(), arb_sim.start(), _merge_state())


async def main():
    """Main entry point"""
    config = load_config()
    bot = ScalpingBot(config)

    loop = asyncio.get_running_loop()
    notifier = create_notifier_from_env()
    gather_task = asyncio.gather(
        bot.start(),
        run_dashboard(),
        _run_funding_scanner(notifier=notifier),
    )

    def _handle_shutdown():
        logger.info("Shutdown signal received — cancelling bot tasks")
        gather_task.cancel()

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, _handle_shutdown)
        except (NotImplementedError, AttributeError):
            # Windows does not support add_signal_handler; KeyboardInterrupt
            # from SIGINT is caught by asyncio.run() instead.
            pass

    try:
        await gather_task
    except asyncio.CancelledError:
        logger.info("Bot shutdown complete")


if __name__ == '__main__':
    asyncio.run(main())

#!/usr/bin/env python3
"""
Master Bot Runner
Launches all bots in parallel:
- Scalping bot (advanced EMA/RSI)
- DEX arbitrage
- Stablecoin triangular arb
- Funding rate arb
"""

import asyncio
import signal
import logging
import sys
from pathlib import Path

# Add src to path
sys.path.insert(0, str(Path(__file__).parent / "src"))
sys.path.insert(0, str(Path(__file__).parent / "arbitrage"))

from src.bot import ScalpingBot
from arbitrage.dex_arb import DEXArbitrageBot, Chain
from arbitrage.stablecoin_arb import StablecoinArbBot
from arbitrage.funding_rate_arb import FundingRateArbBot

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


class MasterBotRunner:
    """Runs all trading bots together"""

    def __init__(self, config: dict):
        self.config = config
        self.running = False

        # Initialize bots
        self.scalping_bot = None
        self.dex_arb_bot = None
        self.stablecoin_arb_bot = None
        self.funding_arb_bot = None

    async def start(self):
        """Start all bots"""
        self.running = True
        logger.info("🚀 Starting Master Bot Runner...")

        # Setup signal handlers (add_signal_handler is Unix-only)
        loop = asyncio.get_event_loop()
        try:
            for sig in (signal.SIGTERM, signal.SIGINT):
                loop.add_signal_handler(sig, lambda: asyncio.create_task(self.shutdown()))
        except NotImplementedError:
            pass  # Windows: handled by KeyboardInterrupt in main()

        # Start scalping bot (paper trading mode)
        if self.config.get("scalping", {}).get("enabled", True):
            logger.info("Starting scalping bot...")
            self.scalping_bot = ScalpingBot(self.config)
            asyncio.create_task(self.scalping_bot._run_paper_mode())

        # Start DEX arb
        if self.config.get("dex_arb", {}).get("enabled", True):
            logger.info("Starting DEX arb bot...")
            self.dex_arb_bot = DEXArbitrageBot(
                chain=Chain.SOLANA,
                min_spread_pct=self.config.get("dex_arb", {}).get("min_spread", 0.5),
                trade_size_usd=self.config.get("dex_arb", {}).get("trade_size", 100)
            )
            asyncio.create_task(self.dex_arb_bot.start())

        # Start stablecoin arb
        if self.config.get("stablecoin_arb", {}).get("enabled", True):
            logger.info("Starting stablecoin arb bot...")
            self.stablecoin_arb_bot = StablecoinArbBot(
                exchanges=self.config.get("stablecoin_arb", {}).get("exchanges", ["kraken"]),
                min_profit_pct=self.config.get("stablecoin_arb", {}).get("min_profit", 0.1),
                trade_size_usd=self.config.get("stablecoin_arb", {}).get("trade_size", 500)
            )
            asyncio.create_task(self.stablecoin_arb_bot.start())

        # Start funding rate arb
        if self.config.get("funding_arb", {}).get("enabled", True):
            logger.info("Starting funding rate arb bot...")
            self.funding_arb_bot = FundingRateArbBot(
                exchanges=self.config.get("funding_arb", {}).get("exchanges", ["bybit"]),
                min_annual_rate=self.config.get("funding_arb", {}).get("min_apy", 15.0),
                max_position_usd=self.config.get("funding_arb", {}).get("max_position", 500)
            )
            asyncio.create_task(self.funding_arb_bot.start())

        logger.info("✅ All bots started. Press Ctrl+C to stop.")

        # Keep running
        while self.running:
            await asyncio.sleep(1)

    async def shutdown(self):
        """Gracefully shutdown all bots"""
        logger.info("🛑 Shutting down...")
        self.running = False

        if self.dex_arb_bot:
            await self.dex_arb_bot.stop()
        if self.stablecoin_arb_bot:
            await self.stablecoin_arb_bot.stop()
        if self.funding_arb_bot:
            await self.funding_arb_bot.stop()

        logger.info("All bots stopped.")


def main():
    # Load config
    import yaml
    config_path = Path(__file__).parent / "config.yaml"

    if config_path.exists():
        with open(config_path) as f:
            config = yaml.safe_load(f)
    else:
        config = {}

    # Add arb bot configs
    config.setdefault("dex_arb", {})["enabled"] = True
    config.setdefault("stablecoin_arb", {})["enabled"] = True
    config.setdefault("funding_arb", {})["enabled"] = True

    runner = MasterBotRunner(config)

    try:
        asyncio.run(runner.start())
    except KeyboardInterrupt:
        pass


if __name__ == '__main__':
    main()

"""
Stablecoin Triangular Arbitrage Bot

Exploits depegs between USDC, USDT, DAI:
USDC → USDT → DAI → USDC

Works on CEX (Kraken, Coinbase) and DEX (Curve)
"""

import asyncio
import aiohttp
import logging
from collections import deque
from datetime import datetime
from typing import Dict, List, Optional
from dataclasses import dataclass

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


@dataclass
class TriangleOpportunity:
    path: List[str]  # e.g., ["USDC", "USDT", "DAI", "USDC"]
    start_amount: float
    end_amount: float
    profit_pct: float
    profit_usd: float
    exchange: str
    timestamp: datetime


class StablecoinArbBot:
    """
    Triangular arbitrage for stablecoins

    Monitors:
    - USDC/USDT pair
    - USDT/DAI pair
    - DAI/USDC pair

    When the triangle yields > starting amount, execute
    """

    def __init__(
        self,
        exchanges: List[str] = None,
        min_profit_pct: float = 0.1,  # 0.1% minimum after fees
        trade_size_usd: float = 500,
        fee_pct: float = 0.0026,      # per-leg taker fee (0.26% = Kraken default)
    ):
        self.exchanges = exchanges or ["kraken", "coinbase", "binance"]
        self.min_profit_pct = min_profit_pct
        self.trade_size_usd = trade_size_usd
        self.fee_pct = fee_pct

        self.session: Optional[aiohttp.ClientSession] = None
        # Bounded deque prevents unbounded memory growth (bot scans every second)
        self.opportunities: deque = deque(maxlen=500)
        self.running = False

        # Stablecoin pairs to monitor
        self.triangle_path = ["USDC", "USDT", "DAI", "USDC"]

    async def start(self):
        """Start scanning"""
        self.session = aiohttp.ClientSession()
        self.running = True
        logger.info(f"Starting stablecoin arb scanner: {self.exchanges}")

        while self.running:
            try:
                for exchange in self.exchanges:
                    await self.scan_exchange(exchange)
                await asyncio.sleep(1)  # Scan every second
            except Exception as e:
                logger.error(f"Scan error: {e}")
                await asyncio.sleep(3)

    async def stop(self):
        """Stop scanner"""
        self.running = False
        if self.session:
            await self.session.close()

    async def scan_exchange(self, exchange: str):
        """Scan one exchange for triangular arb"""
        try:
            # Get prices for all three pairs
            prices = await self.get_triangle_prices(exchange)

            if not prices:
                return

            # Calculate triangle return
            # Start with 1 USDC
            # USDC → USDT: get USDT amount
            # USDT → DAI: get DAI amount
            # DAI → USDC: get final USDC amount

            start_amount = self.trade_size_usd
            amount = start_amount
            fee_mul = 1.0 - self.fee_pct  # multiplier applied after each leg

            # Step 1: USDC → USDT (deduct taker fee)
            usdc_usdt = prices.get("USDC_USDT", 1.0)
            amount = amount * usdc_usdt * fee_mul

            # Step 2: USDT → DAI (deduct taker fee)
            usdt_dai = prices.get("USDT_DAI", 1.0)
            amount = amount * usdt_dai * fee_mul

            # Step 3: DAI → USDC (deduct taker fee)
            dai_usdc = prices.get("DAI_USDC", 1.0)
            amount = amount * dai_usdc * fee_mul

            end_amount = amount
            profit_pct = (end_amount - start_amount) / start_amount * 100
            profit_usd = end_amount - start_amount

            if profit_pct >= self.min_profit_pct:
                opp = TriangleOpportunity(
                    path=self.triangle_path.copy(),
                    start_amount=start_amount,
                    end_amount=end_amount,
                    profit_pct=profit_pct,
                    profit_usd=profit_usd,
                    exchange=exchange,
                    timestamp=datetime.now()
                )
                self.opportunities.append(opp)

                logger.info(
                    f"💰 Triangle Arb on {exchange}: "
                    f"{profit_pct:.3f}% (${profit_usd:.2f})"
                )

        except Exception as e:
            logger.debug(f"Error scanning {exchange}: {e}")

    async def get_triangle_prices(self, exchange: str) -> Optional[Dict[str, float]]:
        """Get prices for all three pairs on an exchange"""
        try:
            if exchange == "kraken":
                return await self._get_kraken_prices()
            elif exchange == "coinbase":
                return await self._get_coinbase_prices()
            elif exchange == "binance":
                return await self._get_binance_prices()
        except Exception:
            pass
        return None

    async def _get_kraken_prices(self) -> Dict[str, float]:
        """Fetch stablecoin prices from Kraken API"""
        pairs = {
            "USDC_USDT": "USDCUSDT",
            "USDT_DAI": "USDTDAI",
            "DAI_USDC": "DAIUSDC"
        }

        prices = {}
        for key, pair in pairs.items():
            try:
                async with self.session.get(
                    f"https://api.kraken.com/0/public/Ticker?pair={pair}",
                    timeout=aiohttp.ClientTimeout(total=3)
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        result = data.get("result", {})
                        ticker = result.get(pair, result.get(list(result.keys())[0], {}))
                        last_price = float(ticker.get("c", [1.0])[0])
                        prices[key] = last_price
            except Exception:
                prices[key] = 1.0  # Default to peg

        return prices

    async def _get_coinbase_prices(self) -> Dict[str, float]:
        """Fetch stablecoin prices from Coinbase API"""
        products = {
            "USDC_USDT": "USDC-USDT",
            "USDT_DAI": "USDT-DAI",
            "DAI_USDC": "DAI-USDC"
        }

        prices = {}
        for key, product in products.items():
            try:
                async with self.session.get(
                    f"https://api.coinbase.com/api/v3/brokerage/products/{product}/ticker",
                    timeout=aiohttp.ClientTimeout(total=3)
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        price = float(data.get("ticker", {}).get("price", "1.0"))
                        prices[key] = price
            except Exception:
                prices[key] = 1.0

        return prices

    async def _get_binance_prices(self) -> Dict[str, float]:
        """Fetch stablecoin prices from Binance API"""
        symbols = {
            "USDC_USDT": "USDCUSDT",
            "USDT_DAI": "USDTDAI",
            "DAI_USDC": "DAIUSDC"
        }

        prices = {}
        for key, symbol in symbols.items():
            try:
                async with self.session.get(
                    f"https://api.binance.com/api/v3/ticker/price?symbol={symbol}",
                    timeout=aiohttp.ClientTimeout(total=3)
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        price = float(data.get("price", "1.0"))
                        prices[key] = price
            except Exception:
                prices[key] = 1.0

        return prices

    def get_best_opportunity(self, max_age_seconds: float = 10.0) -> Optional[TriangleOpportunity]:
        """Return the most profitable opportunity seen within max_age_seconds.

        Stale entries are ignored so callers always get a live signal.
        """
        if not self.opportunities:
            return None
        now = datetime.now()
        recent = [o for o in self.opportunities
                  if (now - o.timestamp).total_seconds() <= max_age_seconds]
        if not recent:
            return None
        return max(recent, key=lambda x: x.profit_usd)


async def main():
    bot = StablecoinArbBot(
        exchanges=["kraken", "coinbase"],
        min_profit_pct=0.1,
        trade_size_usd=500
    )

    try:
        await bot.start()
    except KeyboardInterrupt:
        await bot.stop()


if __name__ == '__main__':
    asyncio.run(main())

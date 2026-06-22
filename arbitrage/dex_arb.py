"""
DEX Arbitrage Bot
Scans Uniswap, Curve, Balancer for price divergences

For Solana: Uses Jupiter aggregator
For Ethereum: Uses Uniswap/Curve direct
"""

import asyncio
import aiohttp
import logging
from datetime import datetime
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass
from enum import Enum

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class Chain(Enum):
    SOLANA = "solana"
    ETHEREUM = "ethereum"
    POLYGON = "polygon"
    ARBITRUM = "arbitrum"


@dataclass
class ArbOpportunity:
    token_in: str
    token_out: str
    buy_dex: str
    sell_dex: str
    buy_price: float
    sell_price: float
    spread_pct: float
    profit_usd: float
    gas_cost_usd: float
    net_profit_usd: float
    timestamp: datetime


class DEXArbitrageBot:
    """
    Scans DEXes for arbitrage opportunities

    Strategy:
    1. Monitor same token across multiple DEXes
    2. When price diverges > threshold, execute buy+sell
    3. Profit from spread minus gas
    """

    def __init__(
        self,
        chain: Chain = Chain.SOLANA,
        min_spread_pct: float = 0.5,  # Minimum spread to consider
        max_slippage_pct: float = 1.0,
        trade_size_usd: float = 100,
    ):
        self.chain = chain
        self.min_spread_pct = min_spread_pct
        self.max_slippage_pct = max_slippage_pct
        self.trade_size_usd = trade_size_usd

        self.session: Optional[aiohttp.ClientSession] = None
        self.opportunities: List[ArbOpportunity] = []
        self.running = False

        # DEX endpoints
        if chain == Chain.SOLANA:
            self.jupiter_url = "https://quote-api.jup.ag/v6"
            self.raydium_url = "https://api.raydium.io"
        elif chain == Chain.ETHEREUM:
            self.uniswap_url = "https://api.uniswap.org/v1"
            self.curve_url = "https://curve.fi/api"

    async def start(self):
        """Start the arbitrage scanner"""
        self.session = aiohttp.ClientSession()
        self.running = True
        logger.info(f"Starting DEX arb scanner on {self.chain.value}")

        while self.running:
            try:
                await self.scan_opportunities()
                await asyncio.sleep(2)  # Scan every 2 seconds
            except Exception as e:
                logger.error(f"Scan error: {e}")
                await asyncio.sleep(5)

    async def stop(self):
        """Stop the scanner"""
        self.running = False
        if self.session:
            await self.session.close()

    async def scan_opportunities(self):
        """Scan for arbitrage opportunities"""
        if self.chain == Chain.SOLANA:
            await self.scan_solana_dexes()
        elif self.chain == Chain.ETHEREUM:
            await self.scan_ethereum_dexes()

    async def scan_solana_dexes(self):
        """Scan Solana DEXes via Jupiter aggregator"""
        # Top Solana tokens to monitor
        tokens = [
            "SOL", "USDC", "USDT", "BONK", "JUP",
            "RAY", "ORCA", "WIF", "PYTH"
        ]

        tasks = []
        for token in tokens:
            tasks.append(self.check_solana_token(token))

        results = await asyncio.gather(*tasks, return_exceptions=True)

        for result in results:
            if isinstance(result, ArbOpportunity):
                self.opportunities.append(result)
                logger.info(
                    f"� Arb: {result.token_in}→{result.token_out} | "
                    f"Spread: {result.spread_pct:.2f}% | "
                    f"Profit: ${result.net_profit_usd:.2f}"
                )

    async def check_solana_token(self, token_symbol: str) -> Optional[ArbOpportunity]:
        """Check a single token for arb opportunities"""
        # Get Jupiter quote for buying token
        # Note: In production, you'd query multiple DEXes directly

        # For demo: simulate price checking
        # In reality, you'd call Jupiter API for route quotes
        buy_price, sell_price = await self.get_token_prices(token_symbol)

        if buy_price and sell_price and buy_price < sell_price:
            spread = (sell_price - buy_price) / buy_price * 100

            if spread >= self.min_spread_pct:
                gas_cost = self.estimate_gas_cost()
                gross_profit = (spread / 100) * self.trade_size_usd
                net_profit = gross_profit - gas_cost

                if net_profit > 0:
                    return ArbOpportunity(
                        token_in=token_symbol,
                        token_out="USDC",
                        buy_dex="Raydium",
                        sell_dex="Orca",
                        buy_price=buy_price,
                        sell_price=sell_price,
                        spread_pct=spread,
                        profit_usd=gross_profit,
                        gas_cost_usd=gas_cost,
                        net_profit_usd=net_profit,
                        timestamp=datetime.now()
                    )

        return None

    async def get_token_prices(self, token_symbol: str) -> Tuple[Optional[float], Optional[float]]:
        """
        Get buy and sell prices for a token

        Returns:
            (buy_price, sell_price) or (None, None) on error
        """
        try:
            if self.chain == Chain.SOLANA:
                # Jupiter quote API
                # In production: call actual API with real token addresses
                async with self.session.get(
                    f"{self.jupiter_url}/quote",
                    params={
                        "inputMint": "So11111111111111111111111111111111111111112",  # SOL
                        "outputMint": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",  # USDC
                        "amount": int(self.trade_size_usd * 1_000_000),  # 6 decimals
                        "slippageBps": int(self.max_slippage_pct * 100)
                    },
                    timeout=aiohttp.ClientTimeout(total=5)
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        price = data.get('outAmount', 0) / 1_000_000
                        return price, price  # Same for demo

            return None, None
        except Exception as e:
            logger.debug(f"Price fetch error for {token_symbol}: {e}")
            return None, None

    def estimate_gas_cost(self) -> float:
        """Estimate transaction gas cost in USD"""
        if self.chain == Chain.SOLANA:
            # Solana: ~$0.001 per tx, arb needs 2 tx (buy + sell)
            return 0.002
        elif self.chain == Chain.ETHEREUM:
            # Ethereum: varies wildly, ~$5-50 per tx
            return 20.0  # Conservative estimate
        elif self.chain == Chain.POLYGON:
            return 0.01
        elif self.chain == Chain.ARBITRUM:
            return 0.1
        return 1.0

    async def execute_arb(self, opportunity: ArbOpportunity) -> bool:
        """
        Execute the arbitrage trade

        In production, this would:
        1. Build buy transaction on DEX1
        2. Build sell transaction on DEX2
        3. Submit both atomically (or near-atomically)
        4. Monitor for confirmation
        """
        logger.info(f"Executing arb: {opportunity.token_in} | Buy @{opportunity.buy_dex} → Sell @{opportunity.sell_dex}")

        # In production: actual swap execution via Jupiter/Uniswap API
        # For now, just log the opportunity

        return True

    def get_top_opportunities(self, limit: int = 5) -> List[ArbOpportunity]:
        """Get best opportunities by net profit"""
        sorted_opps = sorted(
            self.opportunities,
            key=lambda x: x.net_profit_usd,
            reverse=True
        )
        return sorted_opps[:limit]


async def main():
    bot = DEXArbitrageBot(
        chain=Chain.SOLANA,
        min_spread_pct=0.5,
        trade_size_usd=100
    )

    try:
        await bot.start()
    except KeyboardInterrupt:
        await bot.stop()


if __name__ == '__main__':
    asyncio.run(main())

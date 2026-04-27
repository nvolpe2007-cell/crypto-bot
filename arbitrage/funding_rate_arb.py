"""
Funding Rate Arbitrage Bot

Market-neutral strategy:
1. Long spot BTC/ETH
2. Short perpetual futures (same amount)
3. Collect funding rate when positive

Profit = Funding Rate × Position Size × Time
"""

import asyncio
import aiohttp
import logging
from datetime import datetime, timedelta
from typing import Dict, List, Optional
from dataclasses import dataclass

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


@dataclass
class FundingRateOpportunity:
    exchange: str
    symbol: str
    funding_rate_8h: float  # 8-hour funding rate
    annual_rate: float  # Annualized rate
    spot_price: float
    futures_price: float
    basis_pct: float  # Futures premium/discount
    recommended_size_usd: float
    timestamp: datetime


@dataclass
class Position:
    symbol: str
    entry_time: datetime
    spot_entry_price: float
    futures_entry_price: float
    size_usd: float
    total_funding_collected: float = 0.0
    unrealized_pnl: float = 0.0
    is_closed: bool = False


class FundingRateArbBot:
    """
    Cash-and-carry funding rate arbitrage

    When perp futures trade at premium to spot:
    - Funding rate goes positive (longs pay shorts)
    - Short the perp, long the spot
    - Collect funding every 8 hours
    - Exit when basis converges or funding turns negative
    """

    def __init__(
        self,
        exchanges: List[str] = None,
        min_annual_rate: float = 10.0,  # Min 10% APY
        max_position_usd: float = 1000,
    ):
        self.exchanges = exchanges or ["bybit", "binance", "okx"]
        self.min_annual_rate = min_annual_rate
        self.max_position_usd = max_position_usd

        self.session: Optional[aiohttp.ClientSession] = None
        self.opportunities: List[FundingRateOpportunity] = []
        self.positions: List[Position] = []
        self.running = False

    async def start(self):
        """Start the funding rate scanner"""
        self.session = aiohttp.ClientSession()
        self.running = True
        logger.info(f"Starting funding rate arb: {self.exchanges}")

        while self.running:
            try:
                await self.scan_opportunities()
                await self.update_positions()
                await asyncio.sleep(60)  # Check every minute
            except Exception as e:
                logger.error(f"Scan error: {e}")
                await asyncio.sleep(5)

    async def stop(self):
        """Stop scanner"""
        self.running = False
        if self.session:
            await self.session.close()

    async def scan_opportunities(self):
        """Scan exchanges for funding rate opportunities"""
        for exchange in self.exchanges:
            try:
                opps = await self.get_funding_rates(exchange)
                for opp in opps:
                    if opp.annual_rate >= self.min_annual_rate:
                        self.opportunities.append(opp)
                        logger.info(
                            f"📈 Funding Arb {exchange} {opp.symbol}: "
                            f"{opp.annual_rate:.1f}% APY | "
                            f"Basis: {opp.basis_pct:.2f}%"
                        )
            except Exception as e:
                logger.debug(f"Error scanning {exchange}: {e}")

    async def get_funding_rates(self, exchange: str) -> List[FundingRateOpportunity]:
        """Fetch funding rates from exchange"""
        if exchange == "bybit":
            return await self._get_bybit_rates()
        elif exchange == "binance":
            return await self._get_binance_rates()
        elif exchange == "okx":
            return await self._get_okx_rates()
        return []

    async def _get_bybit_rates(self) -> List[FundingRateOpportunity]:
        """Get Bybit funding rates"""
        opportunities = []

        async with self.session.get(
            "https://api.bybit.com/v5/market/tickers?category=linear",
            timeout=aiohttp.ClientTimeout(total=5)
        ) as resp:
            if resp.status == 200:
                data = await resp.json()
                tickers = data.get("result", {}).get("list", [])

                for ticker in tickers[:20]:  # Top 20 by volume
                    symbol = ticker.get("symbol", "")
                    if "USDT" not in symbol:
                        continue

                    funding_rate = float(ticker.get("fundingRate", "0"))
                    spot_price = float(ticker.get("lastPrice", "0"))
                    index_price = float(ticker.get("indexPrice", spot_price))

                    # Annualize: 8-hour rate × 3 × 365
                    annual_rate = funding_rate * 3 * 365 * 100

                    basis = ((spot_price - index_price) / index_price) * 100 if index_price else 0

                    if annual_rate >= self.min_annual_rate:
                        opportunities.append(FundingRateOpportunity(
                            exchange="bybit",
                            symbol=symbol,
                            funding_rate_8h=funding_rate,
                            annual_rate=annual_rate,
                            spot_price=spot_price,
                            futures_price=index_price,
                            basis_pct=basis,
                            recommended_size_usd=min(self.max_position_usd, 500),
                            timestamp=datetime.now()
                        ))

        return opportunities

    async def _get_binance_rates(self) -> List[FundingRateOpportunity]:
        """Get Binance funding rates"""
        opportunities = []

        async with self.session.get(
            "https://fapi.binance.com/fapi/v1/premiumIndex",
            timeout=aiohttp.ClientTimeout(total=5)
        ) as resp:
            if resp.status == 200:
                data = await resp.json()
                if isinstance(data, list):
                    for item in data[:20]:
                        symbol = item.get("symbol", "")
                        if "USDT" not in symbol:
                            continue

                        funding_rate = float(item.get("lastFundingRate", "0"))
                        mark_price = float(item.get("markPrice", "0"))
                        index_price = float(item.get("indexPrice", mark_price))

                        annual_rate = funding_rate * 3 * 365 * 100
                        basis = ((mark_price - index_price) / index_price) * 100 if index_price else 0

                        if annual_rate >= self.min_annual_rate:
                            opportunities.append(FundingRateOpportunity(
                                exchange="binance",
                                symbol=symbol,
                                funding_rate_8h=funding_rate,
                                annual_rate=annual_rate,
                                spot_price=mark_price,
                                futures_price=index_price,
                                basis_pct=basis,
                                recommended_size_usd=min(self.max_position_usd, 500),
                                timestamp=datetime.now()
                            ))

        return opportunities

    async def _get_okx_rates(self) -> List[FundingRateOpportunity]:
        """Get OKX funding rates"""
        opportunities = []

        async with self.session.get(
            "https://www.okx.com/api/v5/public/funding-rate?instType=SWAP",
            timeout=aiohttp.ClientTimeout(total=5)
        ) as resp:
            if resp.status == 200:
                data = await resp.json()
                items = data.get("data", [])

                for item in items[:20]:
                    symbol = item.get("instId", "")
                    funding_rate = float(item.get("fundingRate", "0"))

                    annual_rate = funding_rate * 3 * 365 * 100

                    if annual_rate >= self.min_annual_rate:
                        opportunities.append(FundingRateOpportunity(
                            exchange="okx",
                            symbol=symbol,
                            funding_rate_8h=funding_rate,
                            annual_rate=annual_rate,
                            spot_price=0,  # Would need additional API call
                            futures_price=0,
                            basis_pct=0,
                            recommended_size_usd=min(self.max_position_usd, 500),
                            timestamp=datetime.now()
                        ))

        return opportunities

    async def update_positions(self):
        """Update PnL and funding collected for open positions"""
        for pos in self.positions:
            if not pos.is_closed:
                # In production: fetch current prices and calculate
                # For now, just track time-based funding accrual
                hours_open = (datetime.now() - pos.entry_time).total_seconds() / 3600
                funding_periods = int(hours_open / 8)

                # Estimate funding collected (simplified)
                pos.total_funding_collected = funding_periods * (pos.size_usd * 0.0001)  # Example rate

    async def open_position(self, opportunity: FundingRateOpportunity) -> Optional[Position]:
        """
        Open a funding rate arb position:
        1. Long spot
        2. Short perp futures

        In production, this would execute actual trades
        """
        logger.info(
            f"Opening arb position: Long spot {opportunity.symbol} @ ${opportunity.spot_price:.2f} | "
            f"Short futures @ ${opportunity.futures_price:.2f}"
        )

        position = Position(
            symbol=opportunity.symbol,
            entry_time=datetime.now(),
            spot_entry_price=opportunity.spot_price,
            futures_entry_price=opportunity.futures_price,
            size_usd=opportunity.recommended_size_usd
        )

        self.positions.append(position)
        return position

    async def close_position(self, position: Position) -> bool:
        """Close the position (sell spot, buy back futures)"""
        logger.info(f"Closing position: {position.symbol}")
        position.is_closed = True
        return True

    def get_summary(self) -> Dict:
        """Get portfolio summary"""
        open_positions = [p for p in self.positions if not p.is_closed]
        total_funding = sum(p.total_funding_collected for p in self.positions)

        return {
            "open_positions": len(open_positions),
            "total_funding_collected": total_funding,
            "opportunities_found": len(self.opportunities)
        }


async def main():
    bot = FundingRateArbBot(
        exchanges=["bybit", "binance"],
        min_annual_rate=15.0,  # Look for 15%+ APY
        max_position_usd=500
    )

    try:
        await bot.start()
    except KeyboardInterrupt:
        print(f"\nSummary: {bot.get_summary()}")
        await bot.stop()


if __name__ == '__main__':
    asyncio.run(main())

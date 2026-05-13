"""
Funding Rate Arbitrage Scanner
Scans Bybit and Binance every minute for high funding rates.
Writes opportunities to state.json and sends Telegram alerts.
"""

import asyncio
import aiohttp
import logging
from datetime import datetime, timezone
from typing import List, Dict, Optional
from dataclasses import dataclass, asdict

logger = logging.getLogger(__name__)

MIN_ALERT_APY = 20.0   # Telegram alert threshold
MIN_SHOW_APY  = 10.0   # Show on dashboard threshold


@dataclass
class FundingOpportunity:
    exchange: str
    symbol: str
    rate_8h: float      # raw 8-hour rate
    apy: float          # annualised %
    action: str         # "SHORT PERP + LONG SPOT" or "LONG PERP + SHORT SPOT"
    timestamp: str

    def to_dict(self):
        return asdict(self)


class FundingScanner:
    def __init__(self, notifier=None):
        self.notifier = notifier
        self.session: Optional[aiohttp.ClientSession] = None
        self.opportunities: List[FundingOpportunity] = []
        self._alerted: Dict[str, float] = {}   # symbol → last alerted apy
        self.running = False

    async def start(self):
        self.session = aiohttp.ClientSession()
        self.running = True
        logger.info("Funding rate scanner started")

        while self.running:
            try:
                await self._scan()
            except Exception as e:
                logger.error(f"Funding scan error: {e}")
            await asyncio.sleep(60)

    async def stop(self):
        self.running = False
        if self.session:
            await self.session.close()

    async def _scan(self):
        results = []
        tasks = [self._scan_binance(), self._scan_bybit()]
        for coro in asyncio.as_completed(tasks):
            try:
                batch = await coro
                results.extend(batch)
            except Exception as e:
                logger.debug(f"Scan batch error: {e}")

        # Sort by APY descending, keep top 20
        results.sort(key=lambda x: abs(x.apy), reverse=True)
        self.opportunities = results[:20]

        # Funding alerts disabled — data still written to state for strategy use

        logger.info(f"Funding scan: {len(self.opportunities)} opportunities found")

    async def _scan_binance(self) -> List[FundingOpportunity]:
        results = []
        try:
            async with self.session.get(
                "https://fapi.binance.com/fapi/v1/premiumIndex",
                timeout=aiohttp.ClientTimeout(total=8)
            ) as resp:
                if resp.status != 200:
                    return results
                data = await resp.json()

            for item in data:
                symbol = item.get("symbol", "")
                if not symbol.endswith("USDT"):
                    continue
                rate = float(item.get("lastFundingRate", 0))
                if rate == 0:
                    continue
                apy = rate * 3 * 365 * 100
                if abs(apy) < MIN_SHOW_APY:
                    continue
                action = "SHORT PERP + LONG SPOT" if rate > 0 else "LONG PERP + SHORT SPOT"
                results.append(FundingOpportunity(
                    exchange="Binance",
                    symbol=symbol,
                    rate_8h=round(rate * 100, 4),
                    apy=round(apy, 2),
                    action=action,
                    timestamp=datetime.now(timezone.utc).isoformat()
                ))
        except Exception as e:
            logger.debug(f"Binance funding error: {e}")
        return results

    async def _scan_bybit(self) -> List[FundingOpportunity]:
        results = []
        try:
            async with self.session.get(
                "https://api.bybit.com/v5/market/tickers?category=linear",
                timeout=aiohttp.ClientTimeout(total=8)
            ) as resp:
                if resp.status != 200:
                    return results
                data = await resp.json()

            tickers = data.get("result", {}).get("list", [])
            for t in tickers:
                symbol = t.get("symbol", "")
                if not symbol.endswith("USDT"):
                    continue
                rate_str = t.get("fundingRate", "0")
                try:
                    rate = float(rate_str)
                except ValueError:
                    continue
                if rate == 0:
                    continue
                apy = rate * 3 * 365 * 100
                if abs(apy) < MIN_SHOW_APY:
                    continue
                action = "SHORT PERP + LONG SPOT" if rate > 0 else "LONG PERP + SHORT SPOT"
                results.append(FundingOpportunity(
                    exchange="Bybit",
                    symbol=symbol,
                    rate_8h=round(rate * 100, 4),
                    apy=round(apy, 2),
                    action=action,
                    timestamp=datetime.now(timezone.utc).isoformat()
                ))
        except Exception as e:
            logger.debug(f"Bybit funding error: {e}")
        return results

    async def _send_alert(self, opp: FundingOpportunity):
        pass  # alerts disabled — data written to state.json for strategy use only

    def get_state(self) -> List[dict]:
        return [o.to_dict() for o in self.opportunities]

"""
Funding Rate Arbitrage Scanner
Scans Binance, Bybit, AND Kraken Futures every minute for high funding rates.
Writes opportunities to state.json and sends Telegram alerts.

Note: Binance/Bybit rates are 8-hourly; Kraken Futures funding is hourly. Kraken
opportunities are normalised to an "8h-equivalent" rate at scan time so the
downstream cash-and-carry sim can treat all sources uniformly.

For a US-restricted account, Kraken Futures is the only one actually executable
— Binance/Bybit serve as research baselines for comparison.
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
        tasks = [self._scan_binance(), self._scan_bybit(), self._scan_kraken()]
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

    async def _scan_kraken(self) -> List[FundingOpportunity]:
        """Kraken Futures multi-collateral perps (PF_*).

        Funding is settled hourly on Kraken Futures, not 8-hourly. The ticker's
        `fundingRate` field is the absolute USD-per-contract-per-hour amount
        (NOT a fractional rate). To get a percentage the same way Binance/Bybit
        report it, we divide by `markPrice` — that yields `relativeFundingRate`
        per the historical-rates endpoint. We then expose an 8h-equivalent rate
        so the downstream sim, which assumes FUNDING_CYCLE_HOURS=8, computes
        the correct dollar accrual via linear summation across 8 hourly fundings.
        """
        results = []
        try:
            async with self.session.get(
                "https://futures.kraken.com/derivatives/api/v3/tickers",
                timeout=aiohttp.ClientTimeout(total=8)
            ) as resp:
                if resp.status != 200:
                    return results
                data = await resp.json()

            for t in data.get("tickers", []):
                symbol = t.get("symbol", "")
                # Multi-collateral perps only (PF_*USD). PI_/FI_ are inverse/dated.
                if not symbol.startswith("PF_"):
                    continue
                if t.get("suspended"):
                    continue
                try:
                    funding_usd_per_hour = float(t.get("fundingRate", 0))
                    mark = float(t.get("markPrice", 0))
                except (TypeError, ValueError):
                    continue
                if funding_usd_per_hour == 0 or mark <= 0:
                    continue
                # Convert absolute USD-per-contract-per-hour → fractional rate/hr.
                rate_per_hour_frac = funding_usd_per_hour / mark
                # Plausibility guard: anything above ~50%/yr per hour (5.7e-5) is
                # almost certainly a stale/illiquid micro-cap data glitch — skip.
                if abs(rate_per_hour_frac) > 0.001:   # >0.1%/hr = >876% APY
                    continue
                apy = rate_per_hour_frac * 24 * 365 * 100
                if abs(apy) < MIN_SHOW_APY:
                    continue
                # 8h-equivalent fractional rate, then ×100 for percent (storage
                # convention matches _scan_binance / _scan_bybit above).
                rate_8h_pct = rate_per_hour_frac * 8 * 100
                action = ("SHORT PERP + LONG SPOT" if rate_per_hour_frac > 0
                          else "LONG PERP + SHORT SPOT")
                results.append(FundingOpportunity(
                    exchange="Kraken Futures",
                    symbol=symbol,
                    rate_8h=round(rate_8h_pct, 4),
                    apy=round(apy, 2),
                    action=action,
                    timestamp=datetime.now(timezone.utc).isoformat()
                ))
        except Exception as e:
            logger.debug(f"Kraken funding error: {e}")
        return results

    async def _send_alert(self, opp: FundingOpportunity):
        pass  # alerts disabled — data written to state.json for strategy use only

    def get_state(self) -> List[dict]:
        return [o.to_dict() for o in self.opportunities]

"""
Bybit V5 public-data client (no auth needed). Source of all strategy signals.

Endpoints used (all public market data):
  funding/history, tickers, open-interest, recent-trade, orderbook, kline.

Rate-limited to stay under Bybit's market-data cap, with retry/backoff. Async
(aiohttp), matching the rest of the project. Execution happens elsewhere (Kraken
Futures) — this module is read-only market intelligence.
"""

import asyncio
import logging
import time
from typing import Dict, List, Optional

import aiohttp

from . import config

logger = logging.getLogger(__name__)

_BASE = "https://api.bybit.com"


class BybitData:
    def __init__(self, rate_limit_per_sec: float = config.BYBIT_RATE_LIMIT_PER_SEC):
        self._session: Optional[aiohttp.ClientSession] = None
        self._min_interval = 1.0 / max(1.0, rate_limit_per_sec)
        self._last_call = 0.0
        self._lock = asyncio.Lock()

    async def _ensure_session(self):
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

    async def _get(self, path: str, params: Dict, retries: int = 3) -> Optional[Dict]:
        """GET with rate limiting + exponential backoff. Returns parsed `result` or None."""
        await self._ensure_session()
        url = f"{_BASE}{path}"
        for attempt in range(retries):
            # simple global rate limit
            async with self._lock:
                wait = self._min_interval - (time.time() - self._last_call)
                if wait > 0:
                    await asyncio.sleep(wait)
                self._last_call = time.time()
            try:
                async with self._session.get(
                    url, params=params, timeout=aiohttp.ClientTimeout(total=10)
                ) as resp:
                    data = await resp.json()
                    if data.get("retCode") == 0:
                        return data.get("result", {})
                    logger.warning("[BybitData] %s retCode=%s msg=%s",
                                   path, data.get("retCode"), data.get("retMsg"))
            except Exception as e:
                logger.debug("[BybitData] %s attempt %d failed: %s", path, attempt + 1, e)
            await asyncio.sleep(2 ** attempt)
        logger.error("[BybitData] %s failed after %d retries", path, retries)
        return None

    # ── Funding ──────────────────────────────────────────────────────────────
    async def funding_now(self, symbol: str) -> Optional[Dict]:
        """Current/predicted funding + last price from the ticker. Funding is a
        decimal (0.0001 == 0.01%)."""
        res = await self._get("/v5/market/tickers", {"category": "linear", "symbol": symbol})
        if not res:
            return None
        lst = res.get("list", [])
        if not lst:
            return None
        t = lst[0]
        return {
            "symbol": symbol,
            "funding_rate": float(t.get("fundingRate", 0) or 0),
            "price": float(t.get("lastPrice", 0) or 0),
            "volume_24h": float(t.get("volume24h", 0) or 0),
        }

    async def funding_history(self, symbol: str, limit: int = config.FUNDING_HISTORY_LEN) -> List[float]:
        """Last `limit` settled funding rates (most recent first → returned oldest first)."""
        res = await self._get("/v5/market/history-fund-rate",
                              {"category": "linear", "symbol": symbol, "limit": limit})
        if not res:
            return []
        rows = res.get("list", [])
        rates = [float(r.get("fundingRate", 0) or 0) for r in rows]
        return list(reversed(rates))  # oldest → newest

    # ── Open interest ──────────────────────────────────────────────────────────
    async def open_interest(self, symbol: str, interval: str = config.OI_INTERVAL,
                            limit: int = 3) -> List[Dict]:
        """Recent OI points (oldest→newest). Each: {ts, oi} where oi is in base
        contracts; normalize to USD with the current price at the call site."""
        res = await self._get("/v5/market/open-interest",
                              {"category": "linear", "symbol": symbol,
                               "intervalTime": interval, "limit": limit})
        if not res:
            return []
        rows = res.get("list", [])
        pts = [{"ts": int(r.get("timestamp", 0)), "oi": float(r.get("openInterest", 0) or 0)}
               for r in rows]
        return sorted(pts, key=lambda p: p["ts"])  # oldest → newest

    # ── Trades (for CVD) ─────────────────────────────────────────────────────
    async def recent_trades(self, symbol: str, category: str = "linear",
                            limit: int = config.RECENT_TRADE_LIMIT) -> List[Dict]:
        """Recent public trades. category 'linear' = perp, 'spot' = spot.
        Each trade: {side: 'Buy'/'Sell', size: float, ts: int}."""
        res = await self._get("/v5/market/recent-trade",
                              {"category": category, "symbol": symbol, "limit": min(limit, 1000)})
        if not res:
            return []
        out = []
        for r in res.get("list", []):
            out.append({
                "side": r.get("side", ""),
                "size": float(r.get("size", 0) or 0),
                "price": float(r.get("price", 0) or 0),
                "ts": int(r.get("time", 0) or 0),
            })
        return out

    # ── Order book ─────────────────────────────────────────────────────────────
    async def orderbook(self, symbol: str, depth: int = config.LIQ_CLUSTER_DEPTH) -> Optional[Dict]:
        """Top-`depth` book. Returns {bids: [[price, size]...], asks: [...]}."""
        res = await self._get("/v5/market/orderbook",
                              {"category": "linear", "symbol": symbol, "limit": depth})
        if not res:
            return None
        return {
            "bids": [[float(p), float(s)] for p, s in res.get("b", [])],
            "asks": [[float(p), float(s)] for p, s in res.get("a", [])],
        }

    # ── Klines (volume / candles) ──────────────────────────────────────────────
    async def klines(self, symbol: str, interval: str = "240", limit: int = 21) -> List[Dict]:
        """OHLCV candles (oldest→newest). interval in minutes ('240'=4h) or 'D'.
        Each: {ts, open, high, low, close, volume}."""
        res = await self._get("/v5/market/kline",
                              {"category": "linear", "symbol": symbol,
                               "interval": interval, "limit": limit})
        if not res:
            return []
        rows = res.get("list", [])
        out = []
        for r in rows:  # [start, open, high, low, close, volume, turnover]
            out.append({
                "ts": int(r[0]), "open": float(r[1]), "high": float(r[2]),
                "low": float(r[3]), "close": float(r[4]), "volume": float(r[5]),
            })
        return sorted(out, key=lambda c: c["ts"])  # oldest → newest


async def _selftest():
    """Network-tolerant probe. Bybit may be geo-blocked locally (US); this
    verifies the integration when reachable (e.g. on the VPS), else reports why."""
    d = BybitData()
    try:
        f = await d.funding_now("SOLUSDT")
        if f:
            print(f"data probe OK: SOLUSDT funding={f['funding_rate']*100:+.4f}%/8h "
                  f"price=${f['price']:.2f}")
            hist = await d.funding_history("SOLUSDT")
            print(f"  funding history ({len(hist)}): {[round(x*100, 4) for x in hist]}")
        else:
            print("data probe: no data returned (geo-block or API change?) — verify on VPS")
    except Exception as e:
        print(f"data probe could not reach Bybit locally ({e}) — expected if geo-blocked; runs on VPS")
    finally:
        await d.close()


if __name__ == "__main__":
    asyncio.run(_selftest())

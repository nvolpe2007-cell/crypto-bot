"""
Market Sentiment Monitor
Aggregates three free, no-key data sources every bot cycle:
  - Crypto Fear & Greed Index  (alternative.me)
  - BTC dominance + market cap change  (CoinGecko /global)
  - Bitcoin mempool + 24 h tx volume   (blockchain.info)
"""

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

import aiohttp

logger = logging.getLogger(__name__)

_FEAR_GREED_URL = "https://api.alternative.me/fng/?limit=1"
_COINGECKO_URL  = "https://api.coingecko.com/api/v3/global"
_MEMPOOL_URL    = "https://blockchain.info/q/unconfirmedcount"
_TX24H_URL      = "https://blockchain.info/q/24hrtransactioncount"

_REFRESH_FG  = 3600   # Fear & Greed updates once daily; poll hourly
_REFRESH_CG  = 300    # CoinGecko global: every 5 min
_REFRESH_BC  = 60     # Blockchain.info: every minute


@dataclass
class SentimentSnapshot:
    fear_greed_score: int
    fear_greed_label: str
    btc_dominance: float
    market_cap_change_24h: float
    mempool_tx_count: int
    tx_count_24h: int
    fetched_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def allows_long(self) -> bool:
        """Block new longs during Extreme Fear (score < 25)."""
        return self.fear_greed_score >= 25

    @property
    def altcoin_pressure(self) -> bool:
        """BTC dominance > 60 % — capital rotating into BTC."""
        return self.btc_dominance > 60.0

    @property
    def high_mempool(self) -> bool:
        return self.mempool_tx_count > 50_000

    def to_dict(self) -> dict:
        return {
            "fear_greed_score": self.fear_greed_score,
            "fear_greed_label": self.fear_greed_label,
            "btc_dominance": round(self.btc_dominance, 2),
            "market_cap_change_24h": round(self.market_cap_change_24h, 2),
            "mempool_tx_count": self.mempool_tx_count,
            "tx_count_24h": self.tx_count_24h,
            "allows_long": self.allows_long,
            "altcoin_pressure": self.altcoin_pressure,
            "fetched_at": self.fetched_at.isoformat(),
        }

    def telegram_summary(self) -> str:
        block = "  ⛔ no longs right now" if not self.allows_long else ""
        return (
            f"<b>Bot started</b>{block}\n"
            f"Market mood: <b>{self.fear_greed_label}</b> ({self.fear_greed_score}/100)"
        )


class SentimentMonitor:
    """
    Long-running async task that keeps a fresh SentimentSnapshot.
    Call get_snapshot() from the trading loop; allows_long(symbol) as a gate.
    """

    def __init__(self, notifier=None):
        self._notifier = notifier
        self._snapshot: Optional[SentimentSnapshot] = None
        self._running = False

        # raw cached values
        self._fg_score: int   = 50
        self._fg_label: str   = "Neutral"
        self._btc_dom: float  = 50.0
        self._mkt_change: float = 0.0
        self._mempool: int    = 0
        self._tx24h: int      = 0

        # timestamps for rate-limiting
        self._t_fg: float = 0.0
        self._t_cg: float = 0.0
        self._t_bc: float = 0.0

        # previous F&G score for threshold-crossing alerts
        self._fg_prev: Optional[int] = None

    # ── Public API ────────────────────────────────────────────────────────────

    def get_snapshot(self) -> Optional[SentimentSnapshot]:
        return self._snapshot

    def allows_long(self, symbol: str = "") -> bool:
        s = self._snapshot
        if s is None:
            return True   # no data yet — don't block trades
        if not s.allows_long:
            logger.info(f"[SENTIMENT] LONG blocked — F&G={s.fear_greed_score} ({s.fear_greed_label})")
            return False
        if s.altcoin_pressure and symbol and "BTC" not in symbol:
            logger.info(f"[SENTIMENT] Altcoin pressure warning for {symbol} — BTC dom {s.btc_dominance:.1f}%")
        return True

    async def start(self):
        self._running = True
        logger.info("SentimentMonitor: starting initial fetch")
        await self._refresh_all()
        if self._snapshot and self._notifier:
            self._notifier.send_message(self._snapshot.telegram_summary())
        while self._running:
            await asyncio.sleep(10)
            await self._tick()

    def stop(self):
        self._running = False

    # ── Internal ──────────────────────────────────────────────────────────────

    async def _tick(self):
        now = time.monotonic()
        tasks = []
        if now - self._t_fg >= _REFRESH_FG:
            tasks.append(self._fetch_fear_greed())
        if now - self._t_cg >= _REFRESH_CG:
            tasks.append(self._fetch_coingecko())
        if now - self._t_bc >= _REFRESH_BC:
            tasks.append(self._fetch_blockchain())
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
            self._build_snapshot()
            self._check_alerts()

    async def _refresh_all(self):
        await asyncio.gather(
            self._fetch_fear_greed(),
            self._fetch_coingecko(),
            self._fetch_blockchain(),
            return_exceptions=True,
        )
        self._build_snapshot()

    async def _fetch_fear_greed(self):
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as s:
                async with s.get(_FEAR_GREED_URL) as r:
                    data = await r.json(content_type=None)
                    entry = data["data"][0]
                    self._fg_score = int(entry["value"])
                    self._fg_label = entry["value_classification"]
                    self._t_fg = time.monotonic()
                    logger.info(f"[SENTIMENT] Fear & Greed: {self._fg_score} ({self._fg_label})")
        except Exception as e:
            logger.warning(f"[SENTIMENT] Fear & Greed fetch failed: {e}")

    async def _fetch_coingecko(self):
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as s:
                async with s.get(_COINGECKO_URL) as r:
                    data = await r.json(content_type=None)
                    g = data["data"]
                    self._btc_dom    = float(g["market_cap_percentage"].get("btc", 50.0))
                    self._mkt_change = float(g.get("market_cap_change_percentage_24h_usd", 0.0))
                    self._t_cg = time.monotonic()
                    logger.info(f"[SENTIMENT] CoinGecko: BTC dom={self._btc_dom:.1f}% mkt24h={self._mkt_change:+.2f}%")
        except Exception as e:
            logger.warning(f"[SENTIMENT] CoinGecko fetch failed: {e}")

    async def _fetch_blockchain(self):
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as s:
                async with s.get(_MEMPOOL_URL) as r:
                    self._mempool = int((await r.text()).strip())
                async with s.get(_TX24H_URL) as r:
                    self._tx24h = int((await r.text()).strip())
                self._t_bc = time.monotonic()
                logger.info(f"[SENTIMENT] Blockchain: mempool={self._mempool:,} tx24h={self._tx24h:,}")
        except Exception as e:
            logger.warning(f"[SENTIMENT] Blockchain.info fetch failed: {e}")

    def _build_snapshot(self):
        self._snapshot = SentimentSnapshot(
            fear_greed_score=self._fg_score,
            fear_greed_label=self._fg_label,
            btc_dominance=self._btc_dom,
            market_cap_change_24h=self._mkt_change,
            mempool_tx_count=self._mempool,
            tx_count_24h=self._tx24h,
        )

    def _check_alerts(self):
        """Send Telegram alert when Fear & Greed crosses key thresholds."""
        if self._fg_prev is None:
            self._fg_prev = self._fg_score
            return
        prev, curr = self._fg_prev, self._fg_score
        self._fg_prev = curr
        if self._notifier is None:
            return
        if prev >= 25 > curr:
            self._notifier.send_message(
                "⛔ <b>Extreme Fear</b> — bot pausing longs until mood improves"
            )
        elif prev < 25 <= curr:
            self._notifier.send_message(
                "✅ <b>Fear cleared</b> — longs back open"
            )
        elif prev <= 75 < curr:
            self._notifier.send_message(
                "⚠️ <b>Market is very greedy</b> — bot will be more cautious"
            )
        elif prev > 75 >= curr:
            self._notifier.send_message(
                "📉 <b>Greed fading</b> — market cooling down"
            )

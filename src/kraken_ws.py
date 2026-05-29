"""
Kraken WebSocket v2 Client

Public  (no auth): wss://ws.kraken.com/v2
  - ticker  : real-time last price for every subscribed symbol
  - ohlc    : candle data; fires a candle-close event when confirm=true

Private (auth req): wss://ws-auth.kraken.com/v2
  - executions : real-time order fills
  - balances   : account balance updates

Token for private feeds is fetched from the REST API using the existing
KRAKEN_API_KEY / KRAKEN_API_SECRET from .env.
"""

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import time
import urllib.parse
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

import aiohttp

logger = logging.getLogger(__name__)

_PUBLIC_WS  = "wss://ws.kraken.com/v2"
_PRIVATE_WS = "wss://ws-auth.kraken.com/v2"
_TOKEN_REST = "https://api.kraken.com/0/private/GetWebSocketsToken"

_RECONNECT_DELAY = 5   # seconds before reconnect attempt


# ── Data types ────────────────────────────────────────────────────────────────

@dataclass
class CandleClose:
    symbol: str
    open:   float
    high:   float
    low:    float
    close:  float
    volume: float
    timestamp: str          # ISO-8601 candle open time
    interval: int           # minutes


@dataclass
class Execution:
    order_id: str
    symbol:   str
    side:     str           # "buy" / "sell"
    qty:      float
    avg_price: float
    exec_type: str          # "trade" / "pending" / "canceled" etc.
    timestamp: str


# ── Kraken REST signing (for WS token) ────────────────────────────────────────

def _kraken_sign(api_secret: str, url_path: str, data: dict) -> tuple[str, str]:
    """Return (nonce_str, api_sign_header) for a private REST call."""
    nonce = str(int(time.time() * 1000))
    data["nonce"] = nonce
    post_str  = urllib.parse.urlencode(data)
    msg       = nonce + post_str
    sha256    = hashlib.sha256(msg.encode()).digest()
    key       = base64.b64decode(api_secret)
    signature = hmac.new(key, url_path.encode() + sha256, hashlib.sha512).digest()
    return nonce, base64.b64encode(signature).decode()


async def _fetch_ws_token(api_key: str, api_secret: str) -> Optional[str]:
    url_path = "/0/private/GetWebSocketsToken"
    data = {}
    nonce, sign = _kraken_sign(api_secret, url_path, data)
    headers = {
        "API-Key": api_key,
        "API-Sign": sign,
        "Content-Type": "application/x-www-form-urlencoded",
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                _TOKEN_REST, headers=headers,
                data=urllib.parse.urlencode(data),
                timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                body = await resp.json(content_type=None)
                if body.get("error"):
                    logger.error(f"WS token error: {body['error']}")
                    return None
                token = body["result"]["token"]
                logger.info("Kraken WS auth token obtained")
                return token
    except Exception as e:
        logger.error(f"Failed to fetch WS token: {e}")
        return None


# ── Public WebSocket ──────────────────────────────────────────────────────────

class KrakenPublicWS:
    """
    Streams real-time ticker prices and confirmed OHLC candle closes.
    No API keys required.

    Usage:
        ws = KrakenPublicWS(["BTC/USD", "ETH/USD", "SOL/USD"], ohlc_interval=1)
        asyncio.create_task(ws.start())
        # ...
        price = ws.get_price("BTC/USD")     # latest last price
        # candle-close events are pushed to ws.candle_queue (asyncio.Queue)
    """

    def __init__(self, symbols: List[str], ohlc_interval: int = 1):
        # Kraken WS v2 uses "BTC/USD" format directly
        self._symbols   = symbols
        self._interval  = ohlc_interval
        self._prices: Dict[str, float] = {}
        self.candle_queue: asyncio.Queue = asyncio.Queue(maxsize=200)
        self._running = False

    def get_price(self, symbol: str) -> Optional[float]:
        return self._prices.get(symbol)

    def get_prices(self) -> Dict[str, float]:
        return dict(self._prices)

    async def start(self):
        self._running = True
        while self._running:
            try:
                await self._connect()
            except Exception as e:
                logger.warning(f"[PublicWS] disconnected: {e} — reconnecting in {_RECONNECT_DELAY}s")
            if self._running:
                await asyncio.sleep(_RECONNECT_DELAY)

    def stop(self):
        self._running = False

    async def _connect(self):
        logger.info(f"[PublicWS] connecting to {_PUBLIC_WS}")
        async with aiohttp.ClientSession() as session:
            async with session.ws_connect(_PUBLIC_WS, heartbeat=30) as ws:
                logger.info("[PublicWS] connected")

                # Subscribe to ticker
                await ws.send_json({
                    "method": "subscribe",
                    "params": {
                        "channel": "ticker",
                        "symbol": self._symbols,
                    }
                })
                # Subscribe to OHLC candles
                await ws.send_json({
                    "method": "subscribe",
                    "params": {
                        "channel": "ohlc",
                        "symbol": self._symbols,
                        "interval": self._interval,
                    }
                })

                async for msg in ws:
                    if msg.type == aiohttp.WSMsgType.TEXT:
                        self._handle(msg.data)
                    elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                        break

    def _handle(self, raw: str):
        try:
            data = json.loads(raw)
        except Exception:
            return

        channel = data.get("channel", "")
        msg_type = data.get("type", "")

        if channel == "ticker" and msg_type in ("snapshot", "update"):
            for entry in data.get("data", []):
                sym = entry.get("symbol")
                last = entry.get("last")
                if sym and last is not None:
                    self._prices[sym] = float(last)

        elif channel == "ohlc":
            for entry in data.get("data", []):
                # Only act on confirmed (closed) candles
                if entry.get("confirm") is True:
                    sym = entry.get("symbol")
                    candle = CandleClose(
                        symbol=sym,
                        open=float(entry.get("open", 0)),
                        high=float(entry.get("high", 0)),
                        low=float(entry.get("low", 0)),
                        close=float(entry.get("close", 0)),
                        volume=float(entry.get("volume", 0)),
                        timestamp=entry.get("timestamp", ""),
                        interval=entry.get("interval", self._interval),
                    )
                    try:
                        self.candle_queue.put_nowait(candle)
                        logger.debug(f"[PublicWS] candle close: {sym} @ {candle.close}")
                    except asyncio.QueueFull:
                        pass


# ── Private WebSocket ─────────────────────────────────────────────────────────

class KrakenPrivateWS:
    """
    Authenticated WebSocket for live order fills and balance updates.
    Requires KRAKEN_API_KEY and KRAKEN_API_SECRET.

    Usage:
        ws = KrakenPrivateWS(api_key, api_secret)
        asyncio.create_task(ws.start())
        # ...
        fills = ws.pop_fills()              # list of Execution since last call
        balance = ws.get_balance("USD")
    """

    def __init__(self, api_key: str, api_secret: str,
                 on_fill: Optional[Callable[[Execution], None]] = None):
        self._api_key    = api_key
        self._api_secret = api_secret
        self._on_fill    = on_fill
        self._token: Optional[str] = None
        self._fills: List[Execution] = []
        self._balances: Dict[str, float] = {}
        self._running = False

    def get_balance(self, currency: str = "USD") -> float:
        return self._balances.get(currency, 0.0)

    def pop_fills(self) -> List[Execution]:
        """Return and clear all fills received since the last call."""
        fills, self._fills = self._fills, []
        return fills

    async def start(self):
        self._running = True
        while self._running:
            try:
                self._token = await _fetch_ws_token(self._api_key, self._api_secret)
                if not self._token:
                    logger.warning("[PrivateWS] could not get token — retrying in 30s")
                    await asyncio.sleep(30)
                    continue
                await self._connect()
            except Exception as e:
                logger.warning(f"[PrivateWS] disconnected: {e} — reconnecting in {_RECONNECT_DELAY}s")
            if self._running:
                await asyncio.sleep(_RECONNECT_DELAY)

    def stop(self):
        self._running = False

    async def _connect(self):
        logger.info(f"[PrivateWS] connecting to {_PRIVATE_WS}")
        async with aiohttp.ClientSession() as session:
            async with session.ws_connect(_PRIVATE_WS, heartbeat=30) as ws:
                logger.info("[PrivateWS] connected")

                await ws.send_json({
                    "method": "subscribe",
                    "params": {
                        "channel": "executions",
                        "token": self._token,
                        "snapshot_trades": False,
                    }
                })
                await ws.send_json({
                    "method": "subscribe",
                    "params": {
                        "channel": "balances",
                        "token": self._token,
                    }
                })

                async for msg in ws:
                    if msg.type == aiohttp.WSMsgType.TEXT:
                        self._handle(msg.data)
                    elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                        break

    def _handle(self, raw: str):
        try:
            data = json.loads(raw)
        except Exception:
            return

        channel = data.get("channel", "")
        msg_type = data.get("type", "")

        if channel == "executions" and msg_type in ("snapshot", "update"):
            for entry in data.get("data", []):
                exec_type = entry.get("exec_type", "")
                if exec_type != "trade":
                    continue
                fill = Execution(
                    order_id=entry.get("order_id", ""),
                    symbol=entry.get("symbol", ""),
                    side=entry.get("side", ""),
                    qty=float(entry.get("last_qty") or entry.get("qty", 0)),
                    avg_price=float(entry.get("avg_price") or entry.get("last_price", 0)),
                    exec_type=exec_type,
                    timestamp=entry.get("timestamp", ""),
                )
                self._fills.append(fill)
                if self._on_fill:
                    self._on_fill(fill)
                logger.info(
                    f"[PrivateWS] FILL {fill.side.upper()} {fill.qty} {fill.symbol} "
                    f"@ ${fill.avg_price:.2f}"
                )

        elif channel == "balances" and msg_type in ("snapshot", "update"):
            for entry in data.get("data", []):
                currency = entry.get("asset", "")
                balance  = float(entry.get("balance", 0))
                if currency:
                    self._balances[currency] = balance
                    logger.debug(f"[PrivateWS] balance update: {currency}={balance:.4f}")


# ── Public L2 book feed ───────────────────────────────────────────────────────

class KrakenBookFeed:
    """Maintains a streaming L2 order book per symbol via Kraken WS v2.

    The `book` channel delivers a full snapshot on subscribe, then incremental
    update events containing only the changed levels. A delta with `qty == 0`
    removes the level. We hold a `dict[price] -> qty` per side and sort+slice
    on read. The output format matches ccxt's REST shape (`[[price, qty], ...]`),
    so callers can swap REST for WS without touching consumer code.

    This is the unlock for the dormant microstructure scalper: REST polls every
    ~2s; WS updates push every book change, often dozens per second on majors.
    """

    def __init__(self, symbols: List[str], depth: int = 10):
        self._symbols = symbols
        self._depth   = depth
        self._books: Dict[str, Dict[str, Dict[float, float]]] = {
            s: {"bids": {}, "asks": {}} for s in symbols
        }
        self._last_update: Dict[str, float] = {}   # monotonic time per symbol
        self._running = False

    def get_top(self, symbol: str, depth: Optional[int] = None
                ) -> tuple[List[List[float]], List[List[float]]]:
        """Return current top-N (bids, asks) in REST-compatible shape.
        Bids descending by price, asks ascending. Empty lists if no data yet."""
        n = depth or self._depth
        book = self._books.get(symbol)
        if not book:
            return [], []
        bids = sorted(book["bids"].items(), key=lambda kv: -kv[0])[:n]
        asks = sorted(book["asks"].items(), key=lambda kv:  kv[0])[:n]
        return [[p, q] for p, q in bids], [[p, q] for p, q in asks]

    def staleness(self, symbol: str) -> float:
        """Seconds since the last update for this symbol. Inf if never seen."""
        t = self._last_update.get(symbol)
        if t is None:
            return float("inf")
        return time.monotonic() - t

    def stop(self):
        self._running = False

    async def start(self):
        self._running = True
        while self._running:
            try:
                await self._connect()
            except Exception as e:
                logger.warning(f"[BookFeed] disconnected: {e} — reconnecting in {_RECONNECT_DELAY}s")
            if self._running:
                await asyncio.sleep(_RECONNECT_DELAY)

    async def _connect(self):
        logger.info(f"[BookFeed] connecting to {_PUBLIC_WS} (depth={self._depth})")
        async with aiohttp.ClientSession() as session:
            async with session.ws_connect(_PUBLIC_WS, heartbeat=30) as ws:
                logger.info("[BookFeed] connected")
                await ws.send_json({
                    "method": "subscribe",
                    "params": {
                        "channel": "book",
                        "symbol":  self._symbols,
                        "depth":   self._depth,
                        "snapshot": True,
                    }
                })
                async for msg in ws:
                    if msg.type == aiohttp.WSMsgType.TEXT:
                        self._handle(msg.data)
                    elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                        break

    def _handle(self, raw: str):
        try:
            data = json.loads(raw)
        except Exception:
            return
        if data.get("channel") != "book":
            return
        msg_type = data.get("type", "")
        for entry in data.get("data", []):
            sym = entry.get("symbol")
            if sym not in self._books:
                continue
            book = self._books[sym]
            if msg_type == "snapshot":
                book["bids"].clear()
                book["asks"].clear()
            for b in entry.get("bids", []) or []:
                try:
                    p = float(b.get("price")); q = float(b.get("qty"))
                except Exception:
                    continue
                if q <= 0:
                    book["bids"].pop(p, None)
                else:
                    book["bids"][p] = q
            for a in entry.get("asks", []) or []:
                try:
                    p = float(a.get("price")); q = float(a.get("qty"))
                except Exception:
                    continue
                if q <= 0:
                    book["asks"].pop(p, None)
                else:
                    book["asks"][p] = q
            self._last_update[sym] = time.monotonic()


# ── Public trade tape feed (for VPIN / CVD) ───────────────────────────────────

@dataclass
class TradeTick:
    symbol:    str
    price:     float
    qty:       float
    side:      str      # "buy" or "sell" — the taker side per Kraken docs
    timestamp: str      # RFC3339


class KrakenTradeFeed:
    """Streams the WS v2 `trade` channel — every matched trade with its taker
    side. Used by VPIN / volume-bucketed flow toxicity. Consumers receive
    TradeTick events via the provided callback (called synchronously inside
    the WS message handler — keep callbacks cheap)."""

    def __init__(self, symbols: List[str], on_trade: Optional[Callable[[TradeTick], None]] = None):
        self._symbols  = symbols
        self._on_trade = on_trade
        self._last_update: Dict[str, float] = {}
        self._running = False

    def staleness(self, symbol: str) -> float:
        t = self._last_update.get(symbol)
        if t is None:
            return float("inf")
        return time.monotonic() - t

    def stop(self):
        self._running = False

    async def start(self):
        self._running = True
        while self._running:
            try:
                await self._connect()
            except Exception as e:
                logger.warning(f"[TradeFeed] disconnected: {e} — reconnecting in {_RECONNECT_DELAY}s")
            if self._running:
                await asyncio.sleep(_RECONNECT_DELAY)

    async def _connect(self):
        logger.info(f"[TradeFeed] connecting to {_PUBLIC_WS}")
        async with aiohttp.ClientSession() as session:
            async with session.ws_connect(_PUBLIC_WS, heartbeat=30) as ws:
                logger.info("[TradeFeed] connected")
                await ws.send_json({
                    "method": "subscribe",
                    "params": {
                        "channel": "trade",
                        "symbol":  self._symbols,
                    }
                })
                async for msg in ws:
                    if msg.type == aiohttp.WSMsgType.TEXT:
                        self._handle(msg.data)
                    elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                        break

    def _handle(self, raw: str):
        try:
            data = json.loads(raw)
        except Exception:
            return
        if data.get("channel") != "trade":
            return
        for entry in data.get("data", []):
            sym = entry.get("symbol")
            if sym not in self._symbols:
                continue
            try:
                tick = TradeTick(
                    symbol=sym,
                    price=float(entry.get("price", 0)),
                    qty=float(entry.get("qty", 0)),
                    side=str(entry.get("side", "")),
                    timestamp=str(entry.get("timestamp", "")),
                )
            except Exception:
                continue
            self._last_update[sym] = time.monotonic()
            if self._on_trade:
                try:
                    self._on_trade(tick)
                except Exception as e:
                    logger.debug(f"[TradeFeed] callback error: {e}")

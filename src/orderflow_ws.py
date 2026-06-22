"""
WebSocket-based Order Flow — replaces REST-polling OrderFlowImbalance.

Subscribes to Kraken's live trade tape and Level-2 book via the existing
KrakenPublicWS connection.  Computes in real time:

  CVD  — Cumulative Volume Delta from actual taker trades (buy-initiated vs
          sell-initiated), NOT the candle-approximation used by CVDTracker.
          get_cvd_trend(symbol)  → True when net buying pressure over the last
                                   `window` trades is positive.

  OBI  — Order Book Imbalance from the top-N live book levels.
          get_obi(symbol)        → float 0–1 (>0.55 = bid-heavy = bullish lean)

  Whale — Single-trade detection: a trade ≥ 3× the 100-trade average size.
          last_whale(symbol)     → (side, size, ts) or None if nothing recent

Usage (in live_trading / paper_trading):
    ofw = OrderFlowWS(symbols)
    asyncio.create_task(ofw.start())          # runs forever, auto-reconnects
    ...
    if ofw.get_cvd_trend("BTC/USD") and ofw.get_obi("BTC/USD") > 0.55:
        # order flow confirms → take the trade
"""

import asyncio
import json
import logging
import time
from collections import deque
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

import aiohttp

logger = logging.getLogger(__name__)

_WS_URL          = "wss://ws.kraken.com/v2"
_RECONNECT_DELAY = 5
_BOOK_LEVELS     = 10
_CVD_WINDOW      = 200    # last N trades for CVD trend
_CVD_TREND_HALF  = 10     # compare last 10 trades vs prior 10 for trend
_WHALE_MULT      = 3.0    # trade ≥ 3× avg is a whale
_WHALE_HISTORY   = 100    # trade history for avg size calculation
_STALE_SECS      = 90     # data older than this is considered stale


def obi_from_book(bids, asks, levels: int = _BOOK_LEVELS):
    """Order-Book Imbalance from a top-of-book snapshot in the REST/WS shape
    `[[price, qty], ...]` that KrakenBookFeed.get_top() returns.

    Returns bid_vol / (bid_vol + ask_vol) over the top `levels` on each side
    (>0.55 = bid-heavy/bullish lean, <0.40 = ask-heavy/bearish). Pure and
    side-effect free so it's unit-testable and reusable by the microstructure
    gate off the live KrakenBookFeed — None if the book is empty/degenerate."""
    if not bids or not asks:
        return None
    try:
        bid_vol = sum(float(q) for _, q in list(bids)[:levels])
        ask_vol = sum(float(q) for _, q in list(asks)[:levels])
    except (TypeError, ValueError):
        return None
    total = bid_vol + ask_vol
    if total < 1e-12:
        return None
    return bid_vol / total


@dataclass
class WhalePrint:
    side:       str     # "buy" or "sell"
    size:       float
    price:      float
    timestamp:  float   # unix


class OrderFlowWS:
    """
    Live order-flow tracker using Kraken WebSocket v2.
    Auto-reconnects on disconnect.
    """

    def __init__(self, symbols: List[str]):
        self._symbols = symbols
        self._running = False

        # CVD: deque of (signed_size, ts) — positive = buy-initiated
        self._cvd_trades: Dict[str, deque] = {s: deque(maxlen=_CVD_WINDOW) for s in symbols}
        self._cvd_updated: Dict[str, float] = {}

        # Book: {symbol: {"bids": {price: size}, "asks": {price: size}}}
        self._book: Dict[str, Dict] = {s: {"bids": {}, "asks": {}} for s in symbols}
        self._book_updated: Dict[str, float] = {}

        # Whale detection
        self._trade_sizes: Dict[str, deque] = {s: deque(maxlen=_WHALE_HISTORY) for s in symbols}
        self._last_whale: Dict[str, Optional[WhalePrint]] = {s: None for s in symbols}

    # ── Public API ─────────────────────────────────────────────────────────────

    def get_cvd_trend(self, symbol: str) -> Optional[bool]:
        """
        True  = net buying pressure (recent CVD positive).
        False = net selling pressure.
        None  = insufficient data or stale.
        """
        if time.time() - self._cvd_updated.get(symbol, 0) > _STALE_SECS:
            return None
        trades = list(self._cvd_trades.get(symbol, []))
        if len(trades) < _CVD_TREND_HALF * 2:
            return None
        recent = sum(t[0] for t in trades[-_CVD_TREND_HALF:])
        prior  = sum(t[0] for t in trades[-_CVD_TREND_HALF * 2:-_CVD_TREND_HALF])
        return recent > prior  # accelerating buying vs prior window

    def get_cvd_raw(self, symbol: str) -> Optional[float]:
        """Return the raw signed sum of the last _CVD_WINDOW trades."""
        if time.time() - self._cvd_updated.get(symbol, 0) > _STALE_SECS:
            return None
        trades = self._cvd_trades.get(symbol, [])
        if not trades:
            return None
        return sum(t[0] for t in trades)

    def get_obi(self, symbol: str) -> Optional[float]:
        """
        Order Book Imbalance: bid_vol / (bid_vol + ask_vol) from top N levels.
        >0.55 = bid-heavy = bullish lean.
        None if stale or no data.
        """
        if time.time() - self._book_updated.get(symbol, 0) > _STALE_SECS:
            return None
        book = self._book.get(symbol, {})
        bids = book.get("bids", {})
        asks = book.get("asks", {})
        if not bids or not asks:
            return None

        # Top N bid levels = highest prices
        top_bid_vols = [v for _, v in sorted(bids.items(), reverse=True)[:_BOOK_LEVELS]]
        # Top N ask levels = lowest prices
        top_ask_vols = [v for _, v in sorted(asks.items())[:_BOOK_LEVELS]]

        bid_vol = sum(top_bid_vols)
        ask_vol = sum(top_ask_vols)
        total   = bid_vol + ask_vol
        if total < 1e-12:
            return None
        return bid_vol / total

    def last_whale(self, symbol: str, max_age_secs: float = 60.0) -> Optional[WhalePrint]:
        """Return the most recent whale print if within max_age_secs, else None."""
        wp = self._last_whale.get(symbol)
        if wp and time.time() - wp.timestamp <= max_age_secs:
            return wp
        return None

    def confirms_buy(self, symbol: str) -> bool:
        """
        Returns True when order flow does NOT strongly oppose a buy entry.
        Fail-open: True when data is unavailable (don't block trades on missing data).
        Blocks only when both CVD is bearish AND OBI is strongly ask-heavy.
        """
        cvd = self.get_cvd_trend(symbol)
        obi = self.get_obi(symbol)
        if cvd is None or obi is None:
            return True   # fail-open
        # Hard block: CVD bearish AND book dominated by sellers
        if cvd is False and obi < 0.40:
            return False
        return True

    def confirms_sell(self, symbol: str) -> bool:
        """
        Returns True when order flow does NOT strongly oppose a sell/short entry.
        """
        cvd = self.get_cvd_trend(symbol)
        obi = self.get_obi(symbol)
        if cvd is None or obi is None:
            return True
        if cvd is True and obi > 0.60:
            return False
        return True

    def data_age_secs(self, symbol: str) -> float:
        """
        Returns the age in seconds of the most stale data source (cvd or book).
        Returns _STALE_SECS if no data has arrived yet.
        """
        now = time.time()
        cvd_age  = now - self._cvd_updated.get(symbol, 0)
        book_age = now - self._book_updated.get(symbol, 0)
        return max(cvd_age, book_age)

    def is_data_fresh(self, symbol: str, max_age: float = 30.0) -> bool:
        """True if both CVD and book data arrived within the last `max_age` seconds."""
        return self.data_age_secs(symbol) <= max_age

    def get_spread_pct(self, symbol: str) -> Optional[float]:
        """Best-ask minus best-bid as % of mid price. None if book data is stale."""
        if time.time() - self._book_updated.get(symbol, 0) > _STALE_SECS:
            return None
        book = self._book.get(symbol, {})
        bids = book.get("bids", {})
        asks = book.get("asks", {})
        if not bids or not asks:
            return None
        best_bid = max(bids.keys())
        best_ask = min(asks.keys())
        mid = (best_bid + best_ask) / 2.0
        if mid <= 0:
            return None
        return (best_ask - best_bid) / mid * 100

    # ── Lifecycle ──────────────────────────────────────────────────────────────

    async def start(self):
        """Run forever — reconnects automatically on any error."""
        self._running = True
        while self._running:
            try:
                await self._connect()
            except Exception as e:
                logger.warning(f"[OFW] disconnected: {e} — reconnecting in {_RECONNECT_DELAY}s")
            if self._running:
                await asyncio.sleep(_RECONNECT_DELAY)

    def stop(self):
        self._running = False

    # ── WebSocket connection ───────────────────────────────────────────────────

    async def _connect(self):
        logger.info(f"[OFW] connecting to {_WS_URL} for {self._symbols}")
        async with aiohttp.ClientSession() as session:
            async with session.ws_connect(_WS_URL, heartbeat=20) as ws:
                logger.info("[OFW] connected — subscribing to trade + book")

                # Subscribe to individual trade tape
                await ws.send_json({
                    "method": "subscribe",
                    "params": {
                        "channel": "trade",
                        "symbol": self._symbols,
                    }
                })
                # Subscribe to Level-2 book (top 10 levels, full snapshot + incremental updates)
                await ws.send_json({
                    "method": "subscribe",
                    "params": {
                        "channel": "book",
                        "symbol": self._symbols,
                        "depth":  _BOOK_LEVELS,
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

        channel  = data.get("channel", "")
        msg_type = data.get("type", "")

        if channel == "trade":
            self._handle_trades(data)
        elif channel == "book":
            if msg_type == "snapshot":
                self._handle_book_snapshot(data)
            elif msg_type == "update":
                self._handle_book_update(data)

    # ── Trade feed ─────────────────────────────────────────────────────────────

    def _handle_trades(self, data: dict):
        for entry in data.get("data", []):
            sym  = entry.get("symbol", "")
            if sym not in self._symbols:
                continue
            size  = float(entry.get("qty", 0))
            price = float(entry.get("price", 0))
            side  = entry.get("side", "")   # "buy" or "sell"
            ts    = time.time()

            # Signed size: positive = buy-initiated, negative = sell-initiated
            signed = size if side == "buy" else -size

            self._cvd_trades[sym].append((signed, ts))
            self._cvd_updated[sym] = ts
            self._trade_sizes[sym].append(size)

            # Whale detection
            sizes = list(self._trade_sizes[sym])
            if len(sizes) >= 10:
                avg = sum(sizes) / len(sizes)
                if avg > 0 and size >= _WHALE_MULT * avg:
                    self._last_whale[sym] = WhalePrint(
                        side=side, size=size, price=price, timestamp=ts
                    )
                    logger.info(
                        f"[OFW] 🐋 WHALE {side.upper()} {size:.4f} {sym} "
                        f"@ ${price:.2f} ({size/avg:.1f}× avg)"
                    )

    # ── Book feed ──────────────────────────────────────────────────────────────

    def _handle_book_snapshot(self, data: dict):
        for entry in data.get("data", []):
            sym = entry.get("symbol", "")
            if sym not in self._symbols:
                continue
            bids = {float(r["price"]): float(r["qty"]) for r in entry.get("bids", [])}
            asks = {float(r["price"]): float(r["qty"]) for r in entry.get("asks", [])}
            self._book[sym] = {"bids": bids, "asks": asks}
            self._book_updated[sym] = time.time()

    def _handle_book_update(self, data: dict):
        for entry in data.get("data", []):
            sym = entry.get("symbol", "")
            if sym not in self._symbols:
                continue
            book = self._book.setdefault(sym, {"bids": {}, "asks": {}})

            for r in entry.get("bids", []):
                price = float(r["price"])
                qty   = float(r["qty"])
                if qty == 0:
                    book["bids"].pop(price, None)
                else:
                    book["bids"][price] = qty

            for r in entry.get("asks", []):
                price = float(r["price"])
                qty   = float(r["qty"])
                if qty == 0:
                    book["asks"].pop(price, None)
                else:
                    book["asks"][price] = qty

            self._book_updated[sym] = time.time()

"""
Executor abstraction — paper, spot-live, perpetuals-live.

Decouples *what* the bot decides to do from *how* the order is placed.
Currently the bot has paper logic baked into paper_trading.py; this module
gives a clean interface so the live spot path and the future Kraken
perpetuals path can be slotted in without touching strategy code.

Three implementations:
  - PaperExecutor      : in-process simulation (delegates to PaperTrader)
  - KrakenSpotExecutor : real Kraken spot via ccxt (stub — wire when going live)
  - KrakenPerpsExecutor: Kraken Futures perpetuals (stub — wire when going live)

Select via TRADING_MODE env var: paper | spot | perps
"""

from __future__ import annotations

import logging
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from typing import Optional, Dict

logger = logging.getLogger(__name__)

TRADING_MODE = os.getenv("TRADING_MODE", "paper").lower()
PERPS_DEFAULT_LEVERAGE = float(os.getenv("PERPS_LEVERAGE", "3"))


@dataclass
class OrderResult:
    success: bool
    order_id: Optional[str]
    filled_price: float
    filled_size: float
    fee: float
    error: Optional[str] = None
    is_leveraged: bool = False
    leverage: float = 1.0


class Executor(ABC):
    """Common interface for paper, spot, and perpetuals execution."""

    name: str = "abstract"

    @abstractmethod
    def open_long(self, symbol: str, price: float, size_usd: float,
                  timestamp: datetime, **kwargs) -> OrderResult: ...

    @abstractmethod
    def open_short(self, symbol: str, price: float, size_usd: float,
                   timestamp: datetime, **kwargs) -> OrderResult: ...

    @abstractmethod
    def close_long(self, symbol: str, price: float, timestamp: datetime,
                   reason: str = "") -> OrderResult: ...

    @abstractmethod
    def close_short(self, symbol: str, price: float, timestamp: datetime,
                    reason: str = "") -> OrderResult: ...

    def supports_shorts(self) -> bool:
        return True  # default — spot overrides this


# ── Paper executor (delegates to existing PaperTrader) ─────────────────────

class PaperExecutor(Executor):
    """Thin wrapper around the existing PaperTrader.execute_* methods."""
    name = "paper"

    def __init__(self, trader):
        self.trader = trader

    def open_long(self, symbol, price, size_usd, timestamp, **kw):
        pos = self.trader.execute_buy(symbol, price, timestamp,
                                       size_usd=size_usd, signal=kw.get("signal"))
        return OrderResult(success=pos is not None, order_id=None,
                           filled_price=price, filled_size=size_usd / price if price else 0,
                           fee=getattr(pos, "entry_fee", 0.0) if pos else 0.0)

    def open_short(self, symbol, price, size_usd, timestamp, **kw):
        pos = self.trader.execute_short(symbol, price, timestamp,
                                         size_usd=size_usd, signal=kw.get("signal"))
        return OrderResult(success=pos is not None, order_id=None,
                           filled_price=price, filled_size=size_usd / price if price else 0,
                           fee=getattr(pos, "entry_fee", 0.0) if pos else 0.0)

    def close_long(self, symbol, price, timestamp, reason=""):
        trade = self.trader.execute_sell(symbol, price, timestamp, reason=reason)
        return OrderResult(success=trade is not None, order_id=None,
                           filled_price=price, filled_size=0, fee=0.0)

    def close_short(self, symbol, price, timestamp, reason=""):
        trade = self.trader.execute_cover(symbol, price, timestamp, reason=reason)
        return OrderResult(success=trade is not None, order_id=None,
                           filled_price=price, filled_size=0, fee=0.0)


# ── Kraken spot live (stub) ────────────────────────────────────────────────

class KrakenSpotExecutor(Executor):
    """
    Live Kraken spot via ccxt.
    NOT WIRED — populate when ready to go live. Shorts on Kraken spot
    require margin, which is a separate codepath; default to no shorts.
    """
    name = "kraken-spot"

    def __init__(self, ccxt_client):
        self.client = ccxt_client

    def supports_shorts(self) -> bool:
        return False  # Kraken spot doesn't support shorts without margin

    def open_long(self, symbol, price, size_usd, timestamp, **kw):
        raise NotImplementedError("KrakenSpotExecutor: wire up when going live")

    def open_short(self, *a, **kw):
        return OrderResult(False, None, 0, 0, 0, error="spot doesn't support shorts")

    def close_long(self, symbol, price, timestamp, reason=""):
        raise NotImplementedError

    def close_short(self, *a, **kw):
        return OrderResult(False, None, 0, 0, 0, error="spot doesn't support shorts")


# ── Kraken perpetuals (stub — what the user asked for) ─────────────────────

class KrakenPerpsExecutor(Executor):
    """
    Live Kraken Futures perpetuals via ccxt.krakenfutures.

    Sizing semantics: caller passes `size_usd` representing notional exposure.
    With leverage L, margin actually locked on the exchange is size_usd / L.
    Contracts to trade = size_usd / price (1 contract = 1 unit of underlying
    for BTC/ETH/SOL perps on Kraken Futures; CCXT handles the conversion).

    Expects the bot to maintain its own picture of open positions; the executor
    just routes orders. Position tracking continues to live in PaperTrader-like
    accounts, but with real fills swapped in.
    """
    name = "kraken-perps"

    def __init__(self, ccxt_futures_client, leverage: float = PERPS_DEFAULT_LEVERAGE):
        self.client    = ccxt_futures_client
        self.leverage  = max(1.0, float(leverage))
        # Track open position size per symbol so close_* can size correctly.
        self._open_size: Dict[str, float] = {}
        logger.info(f"[PERPS] Kraken Futures executor initialized "
                    f"(leverage={self.leverage:.1f}x)")

    def _amount_from_usd(self, price: float, size_usd: float) -> float:
        """Number of underlying units to trade for the requested USD notional."""
        if price <= 0:
            return 0.0
        return size_usd / price

    def _perp_symbol(self, symbol: str) -> str:
        """Map spot symbol → perp unified symbol (e.g. BTC/USD → BTC/USD:USD)."""
        if ":" in symbol:
            return symbol
        return f"{symbol}:USD"

    def _reconcile_position(self, symbol: str) -> Optional[float]:
        """
        Query the exchange for the current net position size.
        Returns positive for long, negative for short, 0.0 for flat, None on failure.
        Called after a network exception during open_long/open_short to detect whether
        the order actually executed (ghost-position guard).
        """
        try:
            perp = self._perp_symbol(symbol)
            positions = self.client.fetch_positions([perp])
            for pos in (positions or []):
                if pos.get("symbol") == perp:
                    contracts = float(pos.get("contracts") or 0)
                    side = pos.get("side", "")
                    if side == "long":
                        return contracts
                    if side == "short":
                        return -contracts
                    return 0.0
            return 0.0
        except Exception as e:
            logger.warning(f"[PERPS-LIVE] reconcile_position failed for {symbol}: {e}")
            return None

    def _market_order(self, side: str, symbol: str, amount: float,
                      reduce_only: bool = False) -> Dict:
        perp   = self._perp_symbol(symbol)
        params = {
            "leverage": self.leverage,
            "reduceOnly": reduce_only,
        }
        return self.client.create_order(
            perp, type="market", side=side, amount=amount, params=params
        )

    def open_long(self, symbol, price, size_usd, timestamp, **kw):
        amount = self._amount_from_usd(price, size_usd)
        if amount <= 0:
            return OrderResult(False, None, 0, 0, 0, error="zero amount")
        try:
            order = self._market_order("buy", symbol, amount, reduce_only=False)
            filled_price = float(order.get("average") or order.get("price") or price)
            filled_size  = float(order.get("filled") or amount)
            fee          = float((order.get("fee") or {}).get("cost", 0.0))
            self._open_size[symbol] = self._open_size.get(symbol, 0.0) + filled_size
            logger.info(f"[PERPS-LIVE] LONG  {symbol} {filled_size:.6f} @ ${filled_price:.2f}")
            return OrderResult(True, str(order.get("id") or ""), filled_price,
                               filled_size, fee, is_leveraged=True,
                               leverage=self.leverage)
        except Exception as e:
            logger.error(f"[PERPS-LIVE] open_long failed: {e}")
            net = self._reconcile_position(symbol)
            if net is not None and net > 0:
                logger.warning(
                    f"[PERPS-LIVE] open_long timed out but long position found on exchange "
                    f"({net:.6f} {symbol}); updating local state to avoid ghost position."
                )
                self._open_size[symbol] = net
                return OrderResult(True, None, price, net, 0.0,
                                   is_leveraged=True, leverage=self.leverage)
            return OrderResult(False, None, 0, 0, 0, error=str(e))

    def open_short(self, symbol, price, size_usd, timestamp, **kw):
        amount = self._amount_from_usd(price, size_usd)
        if amount <= 0:
            return OrderResult(False, None, 0, 0, 0, error="zero amount")
        try:
            order = self._market_order("sell", symbol, amount, reduce_only=False)
            filled_price = float(order.get("average") or order.get("price") or price)
            filled_size  = float(order.get("filled") or amount)
            fee          = float((order.get("fee") or {}).get("cost", 0.0))
            self._open_size[symbol] = self._open_size.get(symbol, 0.0) - filled_size
            logger.info(f"[PERPS-LIVE] SHORT {symbol} {filled_size:.6f} @ ${filled_price:.2f}")
            return OrderResult(True, str(order.get("id") or ""), filled_price,
                               filled_size, fee, is_leveraged=True,
                               leverage=self.leverage)
        except Exception as e:
            logger.error(f"[PERPS-LIVE] open_short failed: {e}")
            net = self._reconcile_position(symbol)
            if net is not None and net < 0:
                logger.warning(
                    f"[PERPS-LIVE] open_short timed out but short position found on exchange "
                    f"({net:.6f} {symbol}); updating local state to avoid ghost position."
                )
                self._open_size[symbol] = net
                return OrderResult(True, None, price, abs(net), 0.0,
                                   is_leveraged=True, leverage=self.leverage)
            return OrderResult(False, None, 0, 0, 0, error=str(e))

    def close_long(self, symbol, price, timestamp, reason=""):
        amount = abs(self._open_size.get(symbol, 0.0))
        if amount <= 0:
            # _open_size is in-process only and resets on every restart. Before
            # giving up, verify against the exchange so a real position left
            # over from a previous run still gets closed (same ghost-position
            # guard used to recover open_long/open_short after a timeout).
            net = self._reconcile_position(symbol)
            if net is not None and net > 0:
                logger.warning(
                    f"[PERPS-LIVE] close_long: local state showed no position but "
                    f"exchange has {net:.6f} {symbol}; closing the real position."
                )
                amount = net
            else:
                return OrderResult(False, None, 0, 0, 0, error="no long position")
        try:
            order = self._market_order("sell", symbol, amount, reduce_only=True)
            filled_price = float(order.get("average") or order.get("price") or price)
            self._open_size[symbol] = 0.0
            logger.info(f"[PERPS-LIVE] CLOSE-LONG {symbol} {amount:.6f} @ ${filled_price:.2f}  {reason}")
            return OrderResult(True, str(order.get("id") or ""), filled_price,
                               amount, 0.0, is_leveraged=True, leverage=self.leverage)
        except Exception as e:
            logger.error(f"[PERPS-LIVE] close_long failed: {e}")
            return OrderResult(False, None, 0, 0, 0, error=str(e))

    def close_short(self, symbol, price, timestamp, reason=""):
        amount = abs(self._open_size.get(symbol, 0.0))
        if amount <= 0:
            # See close_long: local _open_size doesn't survive a restart, so
            # confirm with the exchange before declaring there's nothing to close.
            net = self._reconcile_position(symbol)
            if net is not None and net < 0:
                logger.warning(
                    f"[PERPS-LIVE] close_short: local state showed no position but "
                    f"exchange has {net:.6f} {symbol}; closing the real position."
                )
                amount = abs(net)
            else:
                return OrderResult(False, None, 0, 0, 0, error="no short position")
        try:
            order = self._market_order("buy", symbol, amount, reduce_only=True)
            filled_price = float(order.get("average") or order.get("price") or price)
            self._open_size[symbol] = 0.0
            logger.info(f"[PERPS-LIVE] CLOSE-SHORT {symbol} {amount:.6f} @ ${filled_price:.2f}  {reason}")
            return OrderResult(True, str(order.get("id") or ""), filled_price,
                               amount, 0.0, is_leveraged=True, leverage=self.leverage)
        except Exception as e:
            logger.error(f"[PERPS-LIVE] close_short failed: {e}")
            return OrderResult(False, None, 0, 0, 0, error=str(e))


# ── Factory ────────────────────────────────────────────────────────────────

def make_executor(trader=None, ccxt_client=None, ccxt_futures_client=None) -> Executor:
    """
    Returns the executor matching TRADING_MODE env var.

    Caller must provide the relevant client. Paper mode only needs `trader`.
    """
    mode = TRADING_MODE
    if mode == "paper":
        if trader is None:
            raise ValueError("PaperExecutor requires a PaperTrader instance")
        return PaperExecutor(trader)
    if mode == "spot":
        if ccxt_client is None:
            raise ValueError("KrakenSpotExecutor requires a ccxt client")
        return KrakenSpotExecutor(ccxt_client)
    if mode == "perps":
        if ccxt_futures_client is None:
            raise ValueError("KrakenPerpsExecutor requires a ccxt Kraken Futures client")
        return KrakenPerpsExecutor(ccxt_futures_client)
    raise ValueError(f"Unknown TRADING_MODE: {mode}")

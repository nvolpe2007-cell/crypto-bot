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
    Kraken Futures perpetuals via Kraken Futures REST API or ccxt's
    'krakenfutures' module. Supports both directions natively with up
    to 50x leverage on majors (we default to PERPS_LEVERAGE=3 for safety).

    NOT YET WIRED. Two things needed before going live:
      1. Kraken Futures API key (separate from spot keys)
      2. Position sizing in CONTRACTS — perps quote in USD-margined contracts,
         so size_usd / contract_value = contracts to buy/sell
    """
    name = "kraken-perps"

    def __init__(self, ccxt_futures_client, leverage: float = PERPS_DEFAULT_LEVERAGE):
        self.client   = ccxt_futures_client
        self.leverage = leverage
        logger.info(f"[PERPS] Kraken Futures executor initialized "
                    f"(leverage={self.leverage:.1f}x) — STUB, not wired")

    def _contracts_from_usd(self, symbol: str, price: float, size_usd: float) -> float:
        # Each contract on Kraken Futures is 1 USD notional for most perps;
        # number of contracts = notional in USD. Override per-symbol if needed.
        return size_usd * self.leverage

    def open_long(self, symbol, price, size_usd, timestamp, **kw):
        # When wired:
        #   contracts = self._contracts_from_usd(symbol, price, size_usd)
        #   self.client.create_market_buy_order(symbol, contracts, params={'leverage': self.leverage})
        raise NotImplementedError(
            "KrakenPerpsExecutor.open_long: wire up when ready. "
            "Will use leverage and quote in contracts."
        )

    def open_short(self, symbol, price, size_usd, timestamp, **kw):
        raise NotImplementedError("KrakenPerpsExecutor.open_short: wire up when ready")

    def close_long(self, symbol, price, timestamp, reason=""):
        raise NotImplementedError("KrakenPerpsExecutor.close_long: wire up when ready")

    def close_short(self, symbol, price, timestamp, reason=""):
        raise NotImplementedError("KrakenPerpsExecutor.close_short: wire up when ready")


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

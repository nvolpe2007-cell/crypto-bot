"""
Thin Alpaca **paper** trading client for stockbot.

Mirrors the fail-safe pattern of src/trade_brain.py: the `alpaca-py` SDK is
imported LAZILY, so importing this module never requires the package or keys, and
a fake client can be injected for tests. Without keys/SDK, `available()` is False
and the runner simply does nothing (no crash, no orders).

PAPER ONLY by construction: the trading client is built with paper=True and the
paper endpoint. There is no live-money path here.

Env:
  ALPACA_API_KEY / ALPACA_API_SECRET   paper keys from the Alpaca paper dashboard
  ALPACA_PAPER=1                        (default) — paper endpoint; set 0 only if you
                                        REALLY mean live (we still pass paper=… through,
                                        but stockbot never sets this to 0 itself)
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, List, Optional

import pandas as pd


@dataclass
class Account:
    equity: float
    cash: float
    buying_power: float


@dataclass
class Position:
    symbol: str
    qty: float
    avg_entry: float
    unrealized_pl: float
    side: str            # 'long' | 'short'


class AlpacaPaper:
    """Lazy wrapper over alpaca-py. `clients=(trading, data)` may be injected for tests."""

    def __init__(self, api_key: Optional[str] = None, api_secret: Optional[str] = None,
                 paper: bool = True, trading=None, data=None):
        self._key = api_key if api_key is not None else os.getenv("ALPACA_API_KEY", "")
        self._secret = (api_secret if api_secret is not None
                        else os.getenv("ALPACA_API_SECRET", ""))
        # stockbot defaults to paper; ALPACA_PAPER=0 is the only way to flip it.
        self._paper = paper and os.getenv("ALPACA_PAPER", "1").strip().lower() not in ("0", "false", "no")
        self._trading = trading
        self._data = data

    def available(self) -> bool:
        return (self._trading is not None) or bool(self._key and self._secret)

    # ── lazy clients ──────────────────────────────────────────────────────────
    def _t(self):
        if self._trading is None:
            from alpaca.trading.client import TradingClient  # lazy
            if not (self._key and self._secret):
                raise RuntimeError("ALPACA_API_KEY/SECRET not set")
            self._trading = TradingClient(self._key, self._secret, paper=self._paper)
        return self._trading

    def _d(self):
        if self._data is None:
            from alpaca.data.historical import StockHistoricalDataClient  # lazy
            self._data = StockHistoricalDataClient(self._key, self._secret)
        return self._data

    # ── reads ──────────────────────────────────────────────────────────────────
    def account(self) -> Optional[Account]:
        try:
            a = self._t().get_account()
            return Account(float(a.equity), float(a.cash), float(a.buying_power))
        except Exception:
            return None

    def get_position(self, symbol: str) -> Optional[Position]:
        try:
            p = self._t().get_open_position(symbol)
        except Exception:
            return None                          # alpaca raises if no position → flat
        try:
            qty = float(p.qty)
            return Position(symbol, qty, float(p.avg_entry_price),
                            float(p.unrealized_pl), "long" if qty >= 0 else "short")
        except Exception:
            return None

    def recent_bars(self, symbol: str, timeframe: str = "5Min", limit: int = 120
                    ) -> pd.DataFrame:
        """Recent intraday bars as a DataFrame (open/high/low/close/volume,
        DatetimeIndex). Empty DataFrame on any failure."""
        try:
            from alpaca.data.requests import StockBarsRequest
            from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
            amount = int("".join(c for c in timeframe if c.isdigit()) or "5")
            unit = TimeFrameUnit.Minute if "min" in timeframe.lower() else TimeFrameUnit.Hour
            req = StockBarsRequest(symbol_or_symbols=symbol,
                                   timeframe=TimeFrame(amount, unit), limit=limit)
            bars = self._d().get_stock_bars(req).df
            if bars is None or bars.empty:
                return pd.DataFrame()
            if "symbol" in bars.index.names:     # multi-index (symbol, timestamp)
                bars = bars.xs(symbol, level="symbol")
            bars = bars.rename(columns=str.lower)
            return bars[["open", "high", "low", "close", "volume"]].astype(float)
        except Exception:
            return pd.DataFrame()

    # ── writes (paper) ──────────────────────────────────────────────────────────
    def submit_bracket(self, symbol: str, qty: float, side: str,
                       take_profit: float, stop_loss: float) -> Optional[str]:
        """Market entry with a server-side bracket (TP + SL). Returns order id or
        None on failure. Alpaca manages the exits, so a missed poll can't strand us."""
        try:
            from alpaca.trading.requests import (MarketOrderRequest, TakeProfitRequest,
                                                 StopLossRequest)
            from alpaca.trading.enums import OrderSide, TimeInForce, OrderClass
            req = MarketOrderRequest(
                symbol=symbol, qty=qty,
                side=OrderSide.BUY if side == "long" else OrderSide.SELL,
                time_in_force=TimeInForce.DAY, order_class=OrderClass.BRACKET,
                take_profit=TakeProfitRequest(limit_price=round(take_profit, 2)),
                stop_loss=StopLossRequest(stop_price=round(stop_loss, 2)))
            return str(self._t().submit_order(req).id)
        except Exception:
            return None

    def close_all(self) -> bool:
        """Flatten every position + cancel open orders (EOD safety). True on success."""
        try:
            self._t().close_all_positions(cancel_orders=True)
            return True
        except Exception:
            return False

"""Alpaca API wrapper — paper or live trading."""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)

PAPER_BASE_URL = "https://paper-api.alpaca.markets"
LIVE_BASE_URL = "https://api.alpaca.markets"


@dataclass
class OrderResult:
    order_id: str
    symbol: str
    side: str
    qty: float
    limit_price: Optional[float]
    stop_price: Optional[float]
    take_profit_price: Optional[float]
    status: str


@dataclass
class AccountInfo:
    equity: float
    cash: float
    buying_power: float
    portfolio_value: float


class AlpacaBroker:
    """Thin wrapper around alpaca-py TradingClient for stocks."""

    def __init__(self, api_key: str, secret_key: str, paper: bool = True):
        # Import here so the rest of the codebase can load without alpaca-py installed
        from alpaca.trading.client import TradingClient
        from alpaca.trading.enums import OrderSide, TimeInForce, OrderClass
        from alpaca.trading.requests import (
            LimitOrderRequest,
            MarketOrderRequest,
            TakeProfitRequest,
            StopLossRequest,
        )
        from alpaca.data.historical import StockHistoricalDataClient
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame

        self._trading = TradingClient(api_key, secret_key, paper=paper)
        self._data = StockHistoricalDataClient(api_key, secret_key)
        self._paper = paper

        # Stash classes for later use
        self._LimitOrderRequest = LimitOrderRequest
        self._MarketOrderRequest = MarketOrderRequest
        self._TakeProfitRequest = TakeProfitRequest
        self._StopLossRequest = StopLossRequest
        self._OrderSide = OrderSide
        self._TimeInForce = TimeInForce
        self._OrderClass = OrderClass
        self._StockBarsRequest = StockBarsRequest
        self._TimeFrame = TimeFrame
        self._StockHistoricalDataClient = StockHistoricalDataClient

        logger.info("AlpacaBroker init: paper=%s", paper)

    @classmethod
    def from_env(cls, paper: bool = True) -> "AlpacaBroker":
        key = os.environ.get("ALPACA_API_KEY", "")
        secret = os.environ.get("ALPACA_SECRET_KEY", "")
        if not key or not secret:
            raise RuntimeError(
                "Set ALPACA_API_KEY and ALPACA_SECRET_KEY environment variables.\n"
                "Get free paper trading keys at https://alpaca.markets"
            )
        return cls(key, secret, paper=paper)

    def get_account(self) -> AccountInfo:
        acct = self._trading.get_account()
        return AccountInfo(
            equity=float(acct.equity),
            cash=float(acct.cash),
            buying_power=float(acct.buying_power),
            portfolio_value=float(acct.portfolio_value),
        )

    def place_bracket_order(
        self,
        symbol: str,
        qty: float,
        side: str,          # "long" | "short"
        limit_price: float,
        stop_price: float,
        take_profit_price: float,
    ) -> OrderResult:
        """Submit a bracket limit order: entry limit + stop-loss + take-profit."""
        alpaca_side = self._OrderSide.BUY if side == "long" else self._OrderSide.SELL

        request = self._LimitOrderRequest(
            symbol=symbol,
            qty=qty,
            side=alpaca_side,
            time_in_force=self._TimeInForce.DAY,
            limit_price=round(limit_price, 2),
            order_class=self._OrderClass.BRACKET,
            stop_loss=self._StopLossRequest(stop_price=round(stop_price, 2)),
            take_profit=self._TakeProfitRequest(limit_price=round(take_profit_price, 2)),
        )

        try:
            order = self._trading.submit_order(request)
            logger.info(
                "Order submitted: %s %s %s qty=%.4f limit=%.2f stop=%.2f tp=%.2f",
                side.upper(), symbol, order.id, qty,
                limit_price, stop_price, take_profit_price,
            )
            return OrderResult(
                order_id=str(order.id),
                symbol=symbol,
                side=side,
                qty=qty,
                limit_price=limit_price,
                stop_price=stop_price,
                take_profit_price=take_profit_price,
                status=str(order.status),
            )
        except Exception as exc:
            logger.error("Order submission failed for %s: %s", symbol, exc)
            raise

    def close_position(self, symbol: str) -> None:
        """Market-close an open position."""
        try:
            self._trading.close_position(symbol)
            logger.info("Closed position: %s", symbol)
        except Exception as exc:
            logger.warning("close_position %s: %s", symbol, exc)

    def cancel_order(self, order_id: str) -> None:
        try:
            self._trading.cancel_order_by_id(order_id)
        except Exception as exc:
            logger.warning("cancel_order %s: %s", order_id, exc)

    def get_open_positions(self) -> list:
        return self._trading.get_all_positions()

    def get_historical_bars(
        self,
        symbol: str,
        timeframe_str: str,
        start,
        end=None,
        limit: int = 1000,
    ):
        """Fetch historical OHLCV bars. timeframe_str: '1Min', '5Min', '1Day', etc."""
        from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
        import pandas as pd

        tf_map = {
            "1Min": TimeFrame.Minute,
            "5Min": TimeFrame(5, TimeFrameUnit.Minute),
            "15Min": TimeFrame(15, TimeFrameUnit.Minute),
            "1Hour": TimeFrame.Hour,
            "1Day": TimeFrame.Day,
        }
        tf = tf_map.get(timeframe_str, TimeFrame.Minute)

        req = self._StockBarsRequest(
            symbol_or_symbols=symbol,
            timeframe=tf,
            start=start,
            end=end,
            limit=limit,
            feed="iex",
        )
        bars = self._data.get_stock_bars(req)
        df = bars.df

        if isinstance(df.index, pd.MultiIndex):
            df = df.xs(symbol, level="symbol")

        df = df.rename(columns={
            "open": "open", "high": "high", "low": "low",
            "close": "close", "volume": "volume",
        })
        return df[["open", "high", "low", "close", "volume"]]

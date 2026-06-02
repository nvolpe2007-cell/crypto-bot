"""OHLCV bar aggregation and VWAP utilities."""
from __future__ import annotations

import pandas as pd
import numpy as np
from typing import Dict


def aggregate_bars(bars_1m: pd.DataFrame, minutes: int) -> pd.DataFrame:
    """Resample 1-minute OHLCV bars into a larger timeframe.

    bars_1m must have a DatetimeIndex (timezone-aware) and columns:
        open, high, low, close, volume
    """
    if bars_1m.empty:
        return bars_1m.copy()

    rule = f"{minutes}min"
    agg = bars_1m.resample(rule, closed="left", label="left").agg(
        open=("open", "first"),
        high=("high", "max"),
        low=("low", "min"),
        close=("close", "last"),
        volume=("volume", "sum"),
    )
    return agg.dropna(subset=["open"])


def aggregate_daily(bars_1m: pd.DataFrame) -> pd.DataFrame:
    """Collapse 1-minute bars into daily OHLCV bars."""
    if bars_1m.empty:
        return bars_1m.copy()

    agg = bars_1m.resample("1D", closed="left", label="left").agg(
        open=("open", "first"),
        high=("high", "max"),
        low=("low", "min"),
        close=("close", "last"),
        volume=("volume", "sum"),
    )
    return agg.dropna(subset=["open"])


def compute_vwap(bars: pd.DataFrame) -> float:
    """Running VWAP over the provided bars (same-day slice)."""
    if bars.empty:
        return float("nan")
    typical = (bars["high"] + bars["low"] + bars["close"]) / 3.0
    total_volume = bars["volume"].sum()
    if total_volume == 0:
        return float(bars["close"].iloc[-1])
    return float((typical * bars["volume"]).sum() / total_volume)


def make_empty_bars() -> pd.DataFrame:
    return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])


class BarBuffer:
    """Rolling buffer of 1-minute bars per symbol, with on-demand aggregation."""

    def __init__(self, max_bars: int = 500):
        self._max = max_bars
        self._bars: Dict[str, pd.DataFrame] = {}

    def update(self, symbol: str, bar: pd.Series) -> None:
        if symbol not in self._bars:
            self._bars[symbol] = make_empty_bars()

        df = self._bars[symbol]
        new_row = pd.DataFrame([bar])
        new_row.index = pd.to_datetime(new_row.index)
        self._bars[symbol] = pd.concat([df, new_row]).tail(self._max)

    def get_1m(self, symbol: str) -> pd.DataFrame:
        return self._bars.get(symbol, make_empty_bars())

    def get_5m(self, symbol: str) -> pd.DataFrame:
        return aggregate_bars(self.get_1m(symbol), 5)

    def get_15m(self, symbol: str) -> pd.DataFrame:
        return aggregate_bars(self.get_1m(symbol), 15)

    def get_1h(self, symbol: str) -> pd.DataFrame:
        return aggregate_bars(self.get_1m(symbol), 60)

    def get_vwap(self, symbol: str) -> float:
        return compute_vwap(self.get_1m(symbol))

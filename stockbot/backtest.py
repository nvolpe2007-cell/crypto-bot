"""
Event-driven intraday backtest runner. Groups bars by session date and runs the
ORB strategy one day at a time (at most one trade/day/symbol). Honest by
construction: costs are charged per trade, fills are gap-aware, every position is
flat by the close (no overnight P&L), and there is no look-ahead (simulate_day
only ever reads the current bar's OHLC). Returns the list of Trades; metrics.py
judges them.
"""
from __future__ import annotations

from typing import Dict, List

import pandas as pd

from .strategy import ORBConfig, Trade, simulate_day


def run_backtest(df: pd.DataFrame, cfg: ORBConfig, symbol: str = "?") -> List[Trade]:
    """Run ORB over one symbol's intraday bars (DatetimeIndex, columns
    open/high/low/close/volume). One trade per session at most."""
    if df.empty:
        return []
    df = df.sort_index()
    trades: List[Trade] = []
    for _, day in df.groupby(df.index.date):
        t = simulate_day(day, cfg, symbol=symbol)
        if t is not None:
            trades.append(t)
    return trades


def run_multi(frames: Dict[str, pd.DataFrame], cfg: ORBConfig) -> List[Trade]:
    """Run across several symbols; trades pooled (and tagged by symbol)."""
    out: List[Trade] = []
    for sym, df in frames.items():
        out.extend(run_backtest(df, cfg, symbol=sym))
    return out


def net_returns(trades: List[Trade]) -> List[float]:
    return [t.net_ret for t in trades]

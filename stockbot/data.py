"""
Data loaders for the intraday backtest. Three sources:

  • load_csv(path)        — your own bars (time,open,high,low,close,volume).
  • fetch_yfinance(...)   — convenience pull (needs `pip install yfinance` + network;
                            intraday history is limited, e.g. 60d of 5m bars).
  • synthetic_intraday(...) — deterministic fake RTH bars, for tests/demo offline.

All return a pandas DataFrame indexed by a (naive, Eastern-assumed) DatetimeIndex
with columns open/high/low/close/volume, restricted to regular trading hours.
"""
from __future__ import annotations

from datetime import datetime, time, timedelta

import numpy as np
import pandas as pd

_RTH_START, _RTH_END = time(9, 30), time(16, 0)


def _rth(df: pd.DataFrame) -> pd.DataFrame:
    t = df.index.time
    mask = [(_RTH_START <= ti <= _RTH_END) for ti in t]
    return df[mask]


def load_csv(path: str, tz_naive: bool = True) -> pd.DataFrame:
    """CSV with a datetime column (first col or named time/timestamp/datetime) +
    open/high/low/close/volume (case-insensitive). Restricted to RTH."""
    df = pd.read_csv(path)
    cols = {c.lower(): c for c in df.columns}
    tcol = next((cols[k] for k in ("time", "timestamp", "datetime", "date") if k in cols),
                df.columns[0])
    df.index = pd.to_datetime(df[tcol])
    if tz_naive and df.index.tz is not None:
        df.index = df.index.tz_localize(None)
    ren = {}
    for k in ("open", "high", "low", "close", "volume"):
        if k in cols:
            ren[cols[k]] = k
    df = df.rename(columns=ren)[["open", "high", "low", "close", "volume"]].astype(float)
    return _rth(df.sort_index())


def fetch_yfinance(symbol: str, period: str = "60d", interval: str = "5m") -> pd.DataFrame:
    """Pull intraday bars via yfinance (optional dep). Raises a clear error if the
    package or network is unavailable."""
    try:
        import yfinance as yf
    except ImportError as e:
        raise RuntimeError("yfinance not installed — `pip install yfinance` (and you "
                           "need network).") from e
    df = yf.download(symbol, period=period, interval=interval, progress=False,
                     auto_adjust=False)
    if df is None or df.empty:
        raise RuntimeError(f"no data for {symbol} ({interval}/{period}) — intraday "
                           "history is limited; try a shorter period.")
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df = df.rename(columns=str.lower)
    if df.index.tz is not None:
        df.index = df.index.tz_convert("America/New_York").tz_localize(None)
    return _rth(df[["open", "high", "low", "close", "volume"]].astype(float).sort_index())


def synthetic_intraday(days: int = 40, bar_minutes: int = 5, seed: int = 7,
                       trend_per_day: float = 0.004, intrabar_vol: float = 0.0015,
                       start_price: float = 100.0) -> pd.DataFrame:
    """Deterministic RTH bars for tests/demo. Each day gets a random drift sign; on
    up-drift days price tends to break the opening range upward (so long ORB has
    something to trade). NOT a real edge — just exercises the machine offline."""
    rng = np.random.default_rng(seed)
    bars_per_day = int((6 * 60 + 30) / bar_minutes)  # 09:30→16:00
    rows, idx = [], []
    px = start_price
    day0 = datetime(2026, 1, 5, 9, 30)  # a Monday
    d = 0
    placed = 0
    while placed < days:
        day_start = day0 + timedelta(days=d)
        d += 1
        if day_start.weekday() >= 5:      # skip weekends
            continue
        placed += 1
        drift = trend_per_day * (1 if rng.random() > 0.45 else -1) / bars_per_day
        for b in range(bars_per_day):
            ts = day_start + timedelta(minutes=b * bar_minutes)
            ret = drift + rng.normal(0, intrabar_vol)
            o = px
            c = max(0.01, o * (1 + ret))
            hi = max(o, c) * (1 + abs(rng.normal(0, intrabar_vol)))
            lo = min(o, c) * (1 - abs(rng.normal(0, intrabar_vol)))
            vol = float(rng.integers(1000, 5000))
            rows.append((o, hi, lo, c, vol)); idx.append(ts)
            px = c
    df = pd.DataFrame(rows, columns=["open", "high", "low", "close", "volume"],
                      index=pd.DatetimeIndex(idx))
    return df

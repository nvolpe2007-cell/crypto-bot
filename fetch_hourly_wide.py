#!/usr/bin/env python3
"""Fetch REAL 5y hourly OHLCV (not daily-forward-filled) for coins that only
had daily cache, to fix the data-fidelity gap found 2026-08-07: the altcoin
pairs backtest used daily-ffill'd data disguised as hourly for every coin
except BTC/ETH/SOL, which meant all 4 shipped pairs had one artificially-
smoothed leg. Same fetch method as the original cache_1h data."""
import sys
import time
from pathlib import Path

import ccxt
import pandas as pd

CACHE_DIR = Path(__file__).parent / "data" / "cache_1h"
DAYS = 1825


def fetch_1h(symbol: str, days: int) -> pd.DataFrame:
    ex = ccxt.okx({"enableRateLimit": True})
    since = ex.milliseconds() - days * 86400_000
    rows = []
    while True:
        batch = ex.fetch_ohlcv(symbol, "1h", since=since, limit=300)
        if not batch:
            break
        rows += batch
        nxt = batch[-1][0] + 3600_000
        if nxt <= since or len(batch) < 2:
            break
        since = nxt
        if since > ex.milliseconds() - 3600_000:
            break
    df = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close", "volume"])
    df = df.drop_duplicates("ts").sort_values("ts").reset_index(drop=True)
    return df


def main():
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    symbols = sys.argv[1:] or ["AVAX/USDT", "BCH/USDT", "XRP/USDT", "LINK/USDT"]
    for sym in symbols:
        safe = sym.replace("/", "_")
        cache = CACHE_DIR / f"{safe}_{DAYS}d.parquet"
        if cache.exists():
            print(f"  {sym}: cached, skip")
            continue
        try:
            df = fetch_1h(sym, DAYS)
        except Exception as e:
            print(f"  {sym}: FAILED ({e})", file=sys.stderr)
            continue
        df.to_pickle(cache)
        dt0 = pd.to_datetime(df.ts.iloc[0], unit="ms")
        dt1 = pd.to_datetime(df.ts.iloc[-1], unit="ms")
        print(f"  {sym}: {len(df)} REAL hourly bars ({dt0.date()} -> {dt1.date()}) -> {cache.name}")
        time.sleep(0.5)


if __name__ == "__main__":
    main()

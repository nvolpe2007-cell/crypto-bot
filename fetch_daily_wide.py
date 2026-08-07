#!/usr/bin/env python3
"""Fetch 5y daily OHLCV for a wider coin universe (OKX), cached as pickle,
for extending the EMA(21/55)-cross robustness check beyond BTC/ETH/SOL."""
import sys
import time
from pathlib import Path

import ccxt
import pandas as pd

CACHE_DIR = Path(__file__).parent / "data" / "cache_daily"
DAYS = 1825


def fetch_daily(symbol: str, days: int) -> pd.DataFrame:
    ex = ccxt.okx({"enableRateLimit": True})
    since = ex.milliseconds() - days * 86400_000
    rows = []
    while True:
        batch = ex.fetch_ohlcv(symbol, "1d", since=since, limit=300)
        if not batch:
            break
        rows += batch
        nxt = batch[-1][0] + 86400_000
        if nxt <= since or len(batch) < 2:
            break
        since = nxt
        if since > ex.milliseconds() - 86400_000:
            break
    df = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close", "volume"])
    df = df.drop_duplicates("ts").sort_values("ts").reset_index(drop=True)
    df["dt"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
    return df


def main():
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    symbols = sys.argv[1:] or ["LTC/USDT", "BCH/USDT", "XRP/USDT", "ADA/USDT", "LINK/USDT", "AVAX/USDT", "DOT/USDT"]
    for sym in symbols:
        safe = sym.replace("/", "_")
        cache = CACHE_DIR / f"{safe}_{DAYS}d.pkl"
        if cache.exists():
            print(f"  {sym}: cached, skip")
            continue
        try:
            df = fetch_daily(sym, DAYS)
        except Exception as e:
            print(f"  {sym}: FAILED ({e})", file=sys.stderr)
            continue
        if len(df) < DAYS * 0.7:
            print(f"  {sym}: only {len(df)} bars, likely too new -- keeping anyway")
        df.to_pickle(cache)
        print(f"  {sym}: {len(df)} bars ({df.dt.iloc[0].date()} -> {df.dt.iloc[-1].date()}) -> {cache.name}")
        time.sleep(0.5)


if __name__ == "__main__":
    main()

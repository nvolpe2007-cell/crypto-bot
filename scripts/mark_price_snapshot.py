#!/usr/bin/env python3
"""
Mark-price history collector for Binance/Bybit/Kraken Futures perps.

The funding scanner (arbitrage/funding_scanner.py) already hits these same
public endpoints every minute but discards the markPrice field after
computing funding APY. This script re-hits them independently (read-only,
no auth, doesn't touch the live scanner or any trading state) purely to
accumulate a mark-price time series per exchange:symbol, so cross-exchange
basis risk (how much do two venues' marks for the same coin actually drift
apart during a hold) becomes measurable instead of a total unknown.

Persisted to data/mark_price_history.json, same {"samples": {"Exch:SYM":
[[iso_ts, mark_price], ...]}} shape as funding_history.json for consistency.
No pruning here (unlike funding_history.py's 30-day rolling window) — this
is meant to accumulate for a genuinely long research archive, not drive live
trading gates.

    python scripts/mark_price_snapshot.py   # one fetch-and-append, then exit
"""
from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timezone
from pathlib import Path

import aiohttp

STATE_PATH = Path(os.getenv("MARK_PRICE_STATE_FILE", "data/mark_price_history.json"))
MAX_SAMPLES_PER_SYMBOL = int(os.getenv("MARK_PRICE_MAX_SAMPLES", "20000"))


async def fetch_binance(session: aiohttp.ClientSession) -> dict:
    out = {}
    try:
        async with session.get(
            "https://fapi.binance.com/fapi/v1/premiumIndex",
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            if resp.status != 200:
                return out
            data = await resp.json()
        for item in data:
            symbol = item.get("symbol", "")
            if not symbol.endswith("USDT"):
                continue
            try:
                mark = float(item.get("markPrice", 0))
            except (TypeError, ValueError):
                continue
            if mark > 0:
                out[f"Binance:{symbol}"] = mark
    except Exception as e:
        print(f"Binance mark-price fetch error: {e}")
    return out


async def fetch_bybit(session: aiohttp.ClientSession) -> dict:
    out = {}
    try:
        async with session.get(
            "https://api.bybit.com/v5/market/tickers?category=linear",
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            if resp.status != 200:
                return out
            data = await resp.json()
        tickers = data.get("result", {}).get("list", [])
        for t in tickers:
            symbol = t.get("symbol", "")
            if not symbol.endswith("USDT"):
                continue
            try:
                mark = float(t.get("markPrice", 0))
            except (TypeError, ValueError):
                continue
            if mark > 0:
                out[f"Bybit:{symbol}"] = mark
    except Exception as e:
        print(f"Bybit mark-price fetch error: {e}")
    return out


async def fetch_kraken(session: aiohttp.ClientSession) -> dict:
    out = {}
    try:
        async with session.get(
            "https://futures.kraken.com/derivatives/api/v3/tickers",
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            if resp.status != 200:
                return out
            data = await resp.json()
        for t in data.get("tickers", []):
            symbol = t.get("symbol", "")
            if not symbol.startswith("PF_"):
                continue
            try:
                mark = float(t.get("markPrice", 0))
            except (TypeError, ValueError):
                continue
            if mark > 0:
                out[f"Kraken Futures:{symbol}"] = mark
    except Exception as e:
        print(f"Kraken mark-price fetch error: {e}")
    return out


def _load_state() -> dict:
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text())
        except Exception:
            pass
    return {"samples": {}}


def _save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(exist_ok=True)
    tmp = STATE_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state))
    tmp.replace(STATE_PATH)


async def main():
    async with aiohttp.ClientSession() as session:
        results = await asyncio.gather(
            fetch_binance(session), fetch_bybit(session), fetch_kraken(session)
        )
    marks: dict = {}
    for r in results:
        marks.update(r)

    state = _load_state()
    ts = datetime.now(timezone.utc).isoformat()
    for key, price in marks.items():
        series = state["samples"].setdefault(key, [])
        series.append([ts, price])
        if len(series) > MAX_SAMPLES_PER_SYMBOL:
            del series[: len(series) - MAX_SAMPLES_PER_SYMBOL]
    _save_state(state)

    total = sum(len(v) for v in state["samples"].values())
    print(f"[mark_price_snapshot] {ts} symbols_seen={len(marks)} "
          f"total_symbols_tracked={len(state['samples'])} total_samples={total}")


if __name__ == "__main__":
    asyncio.run(main())

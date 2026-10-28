"""
Read-only wrapper for Kalshi's PUBLIC market-data API (KXBTC15M series).

No API key, no account, no order placement anywhere in this package — every
call here is an unauthenticated GET.
"""

from __future__ import annotations

import time
from datetime import datetime
from typing import Dict, Iterator, List, Optional

import requests

BASE = "https://api.elections.kalshi.com/trade-api/v2"
SERIES = "KXBTC15M"


def _get(path: str, params: Optional[dict] = None, tries: int = 4) -> dict:
    for i in range(tries):
        try:
            r = requests.get(f"{BASE}{path}", params=params, timeout=20)
            if r.status_code == 429:
                time.sleep(1 + 2 * i)
                continue
            r.raise_for_status()
            return r.json()
        except Exception:
            if i == tries - 1:
                raise
            time.sleep(1 + i)
    return {}


def iso_ts(s: str) -> int:
    return int(datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp())


def _f(m: dict, key: str) -> Optional[float]:
    v = m.get(key)
    try:
        return float(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def quote(m: dict) -> Dict[str, Optional[float]]:
    """Best asks for both sides. Buying NO at no_ask is selling YES at yes_bid."""
    return {
        "yes_ask": _f(m, "yes_ask_dollars"),
        "yes_ask_size": _f(m, "yes_ask_size_fp"),
        "no_ask": _f(m, "no_ask_dollars"),
        "no_ask_size": _f(m, "yes_bid_size_fp"),
    }


def open_market(now: Optional[int] = None) -> Optional[dict]:
    """The KXBTC15M market whose 15-minute window contains `now`."""
    now = now or int(time.time())
    for m in _get("/markets", {"series_ticker": SERIES, "status": "open", "limit": 20}).get("markets", []):
        if iso_ts(m["open_time"]) <= now < iso_ts(m["close_time"]):
            return m
    return None


def get_market(ticker: str) -> dict:
    return _get(f"/markets/{ticker}").get("market", {})


def settled_markets(min_close_ts: int, max_close_ts: int) -> Iterator[dict]:
    cursor = None
    while True:
        p = {"series_ticker": SERIES, "status": "settled", "limit": 1000,
             "min_close_ts": min_close_ts, "max_close_ts": max_close_ts}
        if cursor:
            p["cursor"] = cursor
        j = _get("/markets", p)
        yield from j.get("markets", [])
        cursor = j.get("cursor")
        if not cursor:
            return


def candlesticks(ticker: str, start_ts: int, end_ts: int) -> List[dict]:
    """1-minute candles; yes_ask/yes_bid closes are the last quote of each minute."""
    return _get(f"/series/{SERIES}/markets/{ticker}/candlesticks",
                {"start_ts": start_ts, "end_ts": end_ts, "period_interval": 1}).get("candlesticks", [])

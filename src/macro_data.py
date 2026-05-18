"""
Macro Data Provider — gold + BTC-gold rolling correlation.

Source: Stooq (free, no API key needed). Single daily fetch, on-disk cache.

The gold-BTC correlation is regime-dependent (see memory: btc-gold-correlation).
We expose:
  - gold_change_1d: yesterday's gold % change
  - btc_gold_corr_30d: 30-day rolling Pearson correlation of daily closes
  - btc_gold_corr_strength: |corr| — used by the gate to decide whether gold
    is usable as an edge at all

This module *never* blocks the main loop on I/O. Refresh runs in a worker
loop with a long interval (default 6h). Reads always return the cached state.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from typing import List, Optional, Tuple

import requests
import pandas as pd

logger = logging.getLogger(__name__)

DATA_DIR  = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")
CACHE_FILE = os.path.join(DATA_DIR, "macro_state.json")
# CoinGecko: free, no API key. PAXG tracks spot gold 1:1.
CG_BASE      = "https://api.coingecko.com/api/v3/coins"
GOLD_HIST_URL = f"{CG_BASE}/pax-gold/market_chart?vs_currency=usd&days=90&interval=daily"
BTC_HIST_URL  = f"{CG_BASE}/bitcoin/market_chart?vs_currency=usd&days=90&interval=daily"
REFRESH_SEC  = int(os.getenv("MACRO_REFRESH_SEC", "21600"))   # 6h default


@dataclass
class MacroState:
    ts: float                       # epoch seconds when computed
    gold_close: float               # latest gold close (USD)
    gold_prev_close: float          # day-before-latest
    gold_change_1d: float           # % change, signed
    btc_gold_corr_30d: float        # Pearson on last 30 daily closes
    btc_gold_corr_60d: float        # longer horizon for context
    sample_size: int                # how many daily pairs the correlation used

    @property
    def corr_strength(self) -> float:
        return abs(self.btc_gold_corr_30d)

    @property
    def is_inverse_regime(self) -> bool:
        return self.btc_gold_corr_30d <= -0.5

    @property
    def is_positive_regime(self) -> bool:
        return self.btc_gold_corr_30d >= 0.5


def _fetch_coingecko_history(url: str) -> Optional[pd.DataFrame]:
    """Returns a DataFrame indexed by date with a single 'Close' column."""
    try:
        r = requests.get(url, timeout=15, headers={"User-Agent": "crypto-bot/1.0"})
        r.raise_for_status()
        payload = r.json()
        prices = payload.get("prices", [])  # list of [ms_timestamp, price]
        if not prices:
            return None
        df = pd.DataFrame(prices, columns=["ts_ms", "Close"])
        df["Date"] = pd.to_datetime(df["ts_ms"], unit="ms").dt.normalize()
        df = df.drop(columns=["ts_ms"]).drop_duplicates("Date").set_index("Date").sort_index()
        return df[["Close"]].astype(float)
    except Exception as e:
        logger.warning(f"[MACRO] CoinGecko fetch failed for {url}: {e}")
        return None


def _compute_state() -> Optional[MacroState]:
    gold = _fetch_coingecko_history(GOLD_HIST_URL)
    btc  = _fetch_coingecko_history(BTC_HIST_URL)
    if gold is None or btc is None:
        return None

    # Align on common dates, take last ~90 daily closes for correlation context
    joined = gold.join(btc, how="inner", lsuffix="_gold", rsuffix="_btc")
    joined = joined.dropna().tail(90)
    if len(joined) < 10:
        return None

    g_series = joined["Close_gold"]
    b_series = joined["Close_btc"]

    # Daily returns for correlation (price-level correlation is misleading)
    gr = g_series.pct_change().dropna()
    br = b_series.pct_change().dropna()
    common = gr.index.intersection(br.index)
    gr = gr.loc[common]
    br = br.loc[common]

    n = len(common)
    if n < 10:
        return None

    corr_30 = float(gr.tail(30).corr(br.tail(30))) if n >= 10 else 0.0
    corr_60 = float(gr.tail(60).corr(br.tail(60))) if n >= 30 else corr_30

    gold_close = float(g_series.iloc[-1])
    gold_prev  = float(g_series.iloc[-2])
    change_1d  = (gold_close / gold_prev - 1.0) * 100.0 if gold_prev else 0.0

    return MacroState(
        ts                = time.time(),
        gold_close        = gold_close,
        gold_prev_close   = gold_prev,
        gold_change_1d    = change_1d,
        btc_gold_corr_30d = corr_30,
        btc_gold_corr_60d = corr_60,
        sample_size       = n,
    )


def _load_cache() -> Optional[MacroState]:
    try:
        with open(CACHE_FILE, "r") as f:
            d = json.load(f)
        return MacroState(**d)
    except Exception:
        return None


def _save_cache(state: MacroState) -> None:
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(CACHE_FILE, "w") as f:
            json.dump(asdict(state), f, indent=2)
    except Exception as e:
        logger.warning(f"[MACRO] cache save failed: {e}")


class MacroDataProvider:
    """
    Holds the latest MacroState in memory. Call start() to launch the
    background refresh task. Call current() any time (non-blocking).
    """

    def __init__(self, refresh_sec: int = REFRESH_SEC):
        self.refresh_sec = refresh_sec
        self._state: Optional[MacroState] = _load_cache()
        self._task: Optional[asyncio.Task] = None
        if self._state:
            age_h = (time.time() - self._state.ts) / 3600
            logger.info(f"[MACRO] loaded cache: corr_30d={self._state.btc_gold_corr_30d:+.2f} "
                        f"gold_1d={self._state.gold_change_1d:+.2f}%  (age {age_h:.1f}h)")

    def current(self) -> Optional[MacroState]:
        return self._state

    async def _refresh_loop(self):
        # If cache is missing or older than refresh interval, fetch immediately
        while True:
            try:
                age = (time.time() - self._state.ts) if self._state else 1e9
                if age >= self.refresh_sec:
                    new_state = await asyncio.to_thread(_compute_state)
                    if new_state:
                        self._state = new_state
                        _save_cache(new_state)
                        logger.info(
                            f"[MACRO] refreshed: gold=${new_state.gold_close:,.0f} "
                            f"({new_state.gold_change_1d:+.2f}%)  "
                            f"corr_30d={new_state.btc_gold_corr_30d:+.2f}  "
                            f"regime={'INVERSE' if new_state.is_inverse_regime else 'POSITIVE' if new_state.is_positive_regime else 'WEAK'}"
                        )
                    else:
                        logger.warning("[MACRO] refresh returned no data")
            except Exception as e:
                logger.error(f"[MACRO] refresh error: {e}")
            await asyncio.sleep(max(60, self.refresh_sec // 4))

    def start(self) -> asyncio.Task:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._refresh_loop())
        return self._task


# ── Alt-beta map (down-side amplification vs BTC) ──────────────────────────
# See memory: alt-beta-to-btc. Used by the contagion edge.
ALT_BETA = {
    "BTC/USD": 1.0,
    "ETH/USD": 1.05,
    "SOL/USD": 1.30,
}

def alt_beta(symbol: str) -> float:
    return ALT_BETA.get(symbol, 1.2)   # default for unknown alts: amplify slightly

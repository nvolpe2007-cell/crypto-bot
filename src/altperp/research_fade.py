"""
Research: measure the fade-short core on a FROTHY historical window — the one
regime it was 0-trades-untestable in (recent calm tape). Uses backtest.run_backtest
(funding≥threshold + not-strong-uptrend, the faithful superset). Paginates funding
history past the 200-record API cap and pulls 4h klines for a target date range.

Runs on the VPS (Bybit reachable). Default window = the Nov-2023→Mar-2024 alt
euphoria (SOL ~$30→$200), where funding on these alts went extreme.

  python -m src.altperp.research_fade [START_ISO] [END_ISO]
"""

import asyncio
import logging
import sys
from datetime import datetime, timezone
from typing import Dict, List, Tuple

from . import config
from .backtest import run_backtest
from .signals import trend_signal

logger = logging.getLogger(__name__)

COINS = ["SOLUSDT", "AVAXUSDT", "ARBUSDT"]
DEFAULT_START = "2023-11-01"
DEFAULT_END = "2024-03-31"


def _ms(iso: str) -> int:
    return int(datetime.fromisoformat(iso).replace(tzinfo=timezone.utc).timestamp() * 1000)


async def fetch_window(coin: str, start_ms: int, end_ms: int) -> List[Dict]:
    """4h klines for [start,end] with funding merged. Paginates funding backward."""
    from .data import BybitData
    dc = BybitData()
    try:
        res = await dc._get("/v5/market/kline",
                            {"category": "linear", "symbol": coin, "interval": "240",
                             "start": start_ms, "end": end_ms, "limit": 1000})
        klines = []
        for r in (res or {}).get("list", []):
            klines.append({"ts": int(r[0]), "open": float(r[1]), "high": float(r[2]),
                           "low": float(r[3]), "close": float(r[4]), "volume": float(r[5])})
        klines.sort(key=lambda k: k["ts"])

        funds: List[Tuple[int, float]] = []
        cursor_end = end_ms
        for _ in range(12):  # 12×200 settles ≫ any window we'd test
            fr = await dc._get("/v5/market/funding/history",
                               {"category": "linear", "symbol": coin,
                                "startTime": start_ms, "endTime": cursor_end, "limit": 200})
            rows = (fr or {}).get("list", [])
            if not rows:
                break
            for r in rows:
                funds.append((int(r.get("fundingRateTimestamp", 0)),
                              float(r.get("fundingRate", 0) or 0)))
            oldest = min(int(r.get("fundingRateTimestamp", 0)) for r in rows)
            if oldest <= start_ms or len(rows) < 200:
                break
            cursor_end = oldest - 1
        funds.sort()

        for bar in klines:
            rate = 0.0
            for ts, f in funds:
                if ts <= bar["ts"]:
                    rate = f
                else:
                    break
            bar["funding"] = rate
        return klines
    finally:
        await dc.close()


def _froth_diag(klines: List[Dict]) -> Dict:
    """How frothy was the window, and how many extremes the trend filter blocks?"""
    thr = config.FUNDING_THRESHOLD_SHORT
    extreme = [b for b in klines if b.get("funding", 0) >= thr]
    blocked = eligible = 0
    for i, b in enumerate(klines):
        if b.get("funding", 0) >= thr:
            if trend_signal(klines[: i + 1]).get("strong_uptrend"):
                blocked += 1
            else:
                eligible += 1
    fundings = [b.get("funding", 0) for b in klines]
    return {
        "bars": len(klines),
        "max_funding_pct": max(fundings) * 100 if fundings else 0.0,
        "extreme_bars": len(extreme),
        "blocked_uptrend": blocked,        # funding extreme but fade blocked (ripping)
        "eligible_rollover": eligible,     # funding extreme AND not strong uptrend → fade fires
    }


async def _amain(start_iso: str, end_iso: str):
    s, e = _ms(start_iso), _ms(end_iso)
    print(f"FADE-SHORT on frothy window {start_iso} → {end_iso}\n")
    for coin in COINS:
        klines = await fetch_window(coin, s, e)
        if not klines:
            print(f"[{coin}] no data (listed yet? Bybit reachable?)")
            continue
        d = _froth_diag(klines)
        st = run_backtest(klines)
        print(f"[{coin}] {d['bars']} 4h bars | maxFunding={d['max_funding_pct']:.3f}%/8h | "
              f"extreme bars={d['extreme_bars']} "
              f"(blocked-uptrend={d['blocked_uptrend']}, eligible-rollover={d['eligible_rollover']})")
        print(f"         {st.render()}\n")


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    start = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_START
    end = sys.argv[2] if len(sys.argv) > 2 else DEFAULT_END
    asyncio.run(_amain(start, end))

"""
COT (Commitments of Traders) — CME Bitcoin positioning, as a MACRO research
signal for the long-only swing book.

WHAT IT IS / IS NOT:
A weekly, macro-timeframe CONTEXT signal — NOT a trade and NOT (yet) wired into
entries. The CFTC's Traders-in-Financial-Futures report breaks CME Bitcoin
futures into Leveraged Funds (trend-following hedge funds/CTAs) and Asset
Managers. The classic read: when Leveraged Funds are at a net-positioning
EXTREME, the trade is crowded and vulnerable to an unwind.

For a long-only spot account this is a FILTER signal:
  • Leveraged Funds crowded NET-LONG  → caution on new longs (unwind risk)
  • Leveraged Funds crowded NET-SHORT  → tailwind for longs (squeeze potential)

You can't trade CME — this only informs the swing strategy's bias. Per the proof
discipline it is logged as context and MEASURED before it ever gates a trade.

Data: CFTC Socrata API (publicreporting.cftc.gov), TFF futures-only dataset
gpe5-46if, market "BITCOIN - CHICAGO MERCANTILE EXCHANGE". Cached weekly to
data/cot_cache.json; degrades to None on any failure (never blocks the bot).
"""
from __future__ import annotations

import json
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

_DATASET = "https://publicreporting.cftc.gov/resource/gpe5-46if.json"
_MARKET = "BITCOIN - CHICAGO MERCANTILE EXCHANGE"
_CACHE = Path("data/cot_cache.json")
_CACHE_TTL_HOURS = 24.0          # COT is weekly; a daily refresh is plenty

# Net-positioning percentile thresholds for "crowded".
_HIGH_PCTILE = 0.90
_LOW_PCTILE = 0.10


@dataclass
class COTSignal:
    date: str
    lev_net: int                 # Leveraged Funds net = long − short (contracts)
    lev_net_pctile: float        # percentile of lev_net within the trailing window
    asset_mgr_net: int           # Asset Managers net = long − short
    extreme: str                 # "crowded_long" | "crowded_short" | "none"
    bias: str                    # "caution_long" | "favor_long" | "neutral"
    n_weeks: int


def _row_to_week(r: dict) -> Optional[dict]:
    try:
        return {
            "date": str(r["report_date_as_yyyy_mm_dd"])[:10],
            "lev_long": int(float(r["lev_money_positions_long"])),
            "lev_short": int(float(r["lev_money_positions_short"])),
            "am_long": int(float(r["asset_mgr_positions_long"])),
            "am_short": int(float(r["asset_mgr_positions_short"])),
        }
    except (KeyError, ValueError, TypeError):
        return None


def fetch_cot(weeks: int = 104, cache: Path = _CACHE) -> List[dict]:
    """Weekly CME-Bitcoin TFF history (ascending by date). Uses a <24h cache,
    then the CFTC API. Returns [] on any failure — never raises."""
    # fresh cache?
    try:
        if cache.exists():
            blob = json.loads(cache.read_text())
            ts = datetime.fromisoformat(blob["fetched_at"])
            age_h = (datetime.now(timezone.utc) - ts).total_seconds() / 3600.0
            if age_h < _CACHE_TTL_HOURS and blob.get("weeks"):
                return blob["weeks"]
    except Exception:
        pass

    where = f"market_and_exchange_names='{_MARKET}'"
    query = {"$where": where, "$order": "report_date_as_yyyy_mm_dd DESC",
             "$limit": str(int(weeks))}
    url = _DATASET + "?" + urllib.parse.urlencode(query)
    try:
        with urllib.request.urlopen(url, timeout=25) as r:
            rows = json.loads(r.read())
    except Exception:
        # fall back to a stale cache if we have one
        try:
            return json.loads(cache.read_text()).get("weeks", [])
        except Exception:
            return []

    parsed = [w for row in rows if (w := _row_to_week(row)) is not None]
    parsed.sort(key=lambda w: w["date"])         # ascending
    try:
        cache.parent.mkdir(exist_ok=True)
        cache.write_text(json.dumps(
            {"fetched_at": datetime.now(timezone.utc).isoformat(), "weeks": parsed}))
    except Exception:
        pass
    return parsed


def compute_signal(history: List[dict]) -> Optional[COTSignal]:
    """Build the COT signal from weekly history (ascending). Needs >=8 weeks to
    have a meaningful percentile. Pure — unit-tested without network."""
    if len(history) < 8:
        return None
    nets = [h["lev_long"] - h["lev_short"] for h in history]
    latest = nets[-1]
    pct = sum(1 for x in nets if x <= latest) / len(nets)
    am_net = history[-1]["am_long"] - history[-1]["am_short"]
    if pct >= _HIGH_PCTILE:
        extreme, bias = "crowded_long", "caution_long"
    elif pct <= _LOW_PCTILE:
        extreme, bias = "crowded_short", "favor_long"
    else:
        extreme, bias = "none", "neutral"
    return COTSignal(history[-1]["date"], latest, round(pct, 3), am_net,
                     extreme, bias, len(history))


def cot_signal(weeks: int = 104) -> Optional[COTSignal]:
    """Convenience: fetch (cached) + compute. None on any failure."""
    return compute_signal(fetch_cot(weeks))

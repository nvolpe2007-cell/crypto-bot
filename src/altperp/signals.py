"""
The four strategy signals, as pure functions over already-fetched data.

Tier 1 (gates): funding_signal, oi_signal
Tier 2 (confirmation/boost): cvd_signal, liq_proximity_signal

Keeping these pure (data in → result dict out, no I/O) makes them deterministic
and unit-testable offline. The data layer (data.py) fetches; confluence.py
combines these into setups.
"""

import statistics
from typing import Dict, List

from . import config
from .math_utils import cvd_from_trades, pct_change, is_funding_spike, ema


def funding_signal(funding_rate: float, funding_history: List[float]) -> Dict:
    """Tier-1 funding gate. funding_rate is a decimal (0.0005 == 0.05%/8h)."""
    avg48 = (sum(funding_history) / len(funding_history)) if funding_history else None
    prev = funding_history[-1] if funding_history else None
    # Flush-long trigger: funding dropped >=50% vs the previous reading (crowd unwinding)
    collapsed = bool(prev is not None and prev > 0 and funding_rate <= prev * 0.5)
    return {
        "funding_rate": funding_rate,
        "funding_48hr_avg": avg48,
        "short_eligible": funding_rate >= config.FUNDING_THRESHOLD_SHORT,
        "long_eligible": funding_rate <= config.FUNDING_THRESHOLD_LONG,
        "is_spike": is_funding_spike(funding_rate, funding_history, config.FUNDING_SPIKE_MULTIPLIER),
        "funding_collapsed": collapsed,
    }


def oi_signal(oi_points: List[Dict], price: float) -> Dict:
    """Tier-1 OI gate. oi_points oldest→newest (≥3 for an 8h read). `oi` is in
    contracts; normalized to USD with `price` for cross-coin comparability."""
    oi_4h = oi_8h = None
    if len(oi_points) >= 2:
        oi_4h = pct_change(oi_points[-2]["oi"], oi_points[-1]["oi"])
    if len(oi_points) >= 3:
        oi_8h = pct_change(oi_points[-3]["oi"], oi_points[-1]["oi"])

    short_spike = bool(
        (oi_4h is not None and oi_4h >= config.OI_SPIKE_THRESHOLD_4HR) or
        (oi_8h is not None and oi_8h >= config.OI_SPIKE_THRESHOLD_8HR)
    )
    long_flush = bool(
        (oi_4h is not None and oi_4h <= -config.OI_FLUSH_THRESHOLD) or
        (oi_8h is not None and oi_8h <= -config.OI_FLUSH_THRESHOLD)
    )
    oi_usd = oi_points[-1]["oi"] * price if oi_points and price else None
    return {
        "oi_current_usd": oi_usd,
        "oi_4hr_change": oi_4h,
        "oi_8hr_change": oi_8h,
        "short_spike": short_spike,
        "long_flush": long_flush,
    }


def cvd_signal(perp_trades: List[Dict], spot_trades: List[Dict]) -> Dict:
    """Tier-2 spot/perp CVD divergence.

    Bearish (confirms short): perp net-buying while spot is flat/selling →
      leverage-driven move with no real spot demand.
    Bullish (confirms long): spot net-buying while perp was net-selling.
    """
    perp_cvd = cvd_from_trades(perp_trades)
    spot_cvd = cvd_from_trades(spot_trades)
    bearish = perp_cvd > 0 and spot_cvd <= 0
    bullish = spot_cvd > 0 and perp_cvd < 0
    return {
        "perp_cvd": perp_cvd,
        "spot_cvd": spot_cvd,
        "bearish_divergence": bool(bearish),
        "bullish_divergence": bool(bullish),
    }


def liq_proximity_signal(orderbook: Dict, price: float) -> Dict:
    """Tier-2 liquidation-proximity proxy via large book clusters.

    A "wall" = a level whose size >= mean + N·stdev of all level sizes. If a wall
    sits within LIQ_PROXIMITY_PCT *below* price → magnet below → favors SHORT.
    A wall within range *above* → favors LONG. (Approximation; true liq heatmaps
    need Coinglass — see config notes.)
    """
    out = {"short_proximity": False, "long_proximity": False,
           "cluster_below": None, "cluster_above": None}
    if not orderbook or not price:
        return out
    bids = orderbook.get("bids", [])
    asks = orderbook.get("asks", [])
    sizes = [s for _, s in bids] + [s for _, s in asks]
    if len(sizes) < 5:
        return out
    # Robust wall detection: size >= MULT × median level. Median ignores the
    # dominant wall itself, unlike mean+stdev (which the wall would inflate past
    # its own threshold, detecting nothing).
    median = statistics.median(sizes)
    wall_threshold = config.LIQ_CLUSTER_SIZE_MULT * median if median > 0 else float("inf")
    band = config.LIQ_PROXIMITY_PCT

    # Largest wall below price (from bids), within band
    below = [(p, s) for p, s in bids if s >= wall_threshold and 0 <= (price - p) / price <= band]
    if below:
        cluster = max(below, key=lambda x: x[1])
        out["short_proximity"] = True
        out["cluster_below"] = cluster[0]

    # Largest wall above price (from asks), within band
    above = [(p, s) for p, s in asks if s >= wall_threshold and 0 <= (p - price) / price <= band]
    if above:
        cluster = max(above, key=lambda x: x[1])
        out["long_proximity"] = True
        out["cluster_above"] = cluster[0]
    return out


def trend_signal(klines: List[Dict]) -> Dict:
    """Regime filter on 4h closes. Blocks fading into a sustained trend — the
    research-flagged #1 failure mode of this strategy.

    strong_uptrend  = price above a rising EMA by > TREND_EXT_PCT  → block SHORT
    strong_downtrend = price below a falling EMA by > TREND_EXT_PCT → block FLUSH LONG
    """
    out = {"ema": None, "slope": 0.0, "strong_uptrend": False, "strong_downtrend": False}
    if not config.TREND_FILTER_ENABLED:
        return out
    closes = [c["close"] for c in klines] if klines else []
    series = ema(closes, config.TREND_EMA_PERIOD)
    if not series or len(series) <= config.TREND_SLOPE_LOOKBACK:
        return out  # not enough data → don't block (fail-open)
    ema_now = series[-1]
    ema_prev = series[-1 - config.TREND_SLOPE_LOOKBACK]
    price = closes[-1]
    slope = (ema_now - ema_prev) / ema_prev if ema_prev else 0.0
    ext = (price - ema_now) / ema_now if ema_now else 0.0
    out["ema"] = ema_now
    out["slope"] = slope
    out["strong_uptrend"] = slope > 0 and ext > config.TREND_EXT_PCT
    out["strong_downtrend"] = slope < 0 and ext < -config.TREND_EXT_PCT
    return out


def _selftest():
    # Funding: 0.07%/8h with a calm 0.02% history → eligible short + spike
    f = funding_signal(0.0007, [0.0002, 0.0002, 0.0003, 0.0002, 0.0002, 0.0003])
    assert f["short_eligible"] and f["is_spike"] and not f["long_eligible"], f

    # OI: 100 → 132 over last 4h (+32%) → short spike
    oi = oi_signal([{"ts": 1, "oi": 90}, {"ts": 2, "oi": 100}, {"ts": 3, "oi": 132}], price=185.0)
    assert oi["short_spike"] and not oi["long_flush"], oi
    assert abs(oi["oi_4hr_change"] - 0.32) < 1e-9

    # OI flush: 100 → 78 (-22%) → long flush
    flush = oi_signal([{"ts": 1, "oi": 105}, {"ts": 2, "oi": 100}, {"ts": 3, "oi": 78}], price=185.0)
    assert flush["long_flush"] and not flush["short_spike"], flush

    # CVD: perp buying (+), spot selling (-) → bearish divergence
    perp = [{"side": "Buy", "size": 50}, {"side": "Sell", "size": 10}]
    spot = [{"side": "Sell", "size": 30}, {"side": "Buy", "size": 5}]
    c = cvd_signal(perp, spot)
    assert c["bearish_divergence"] and not c["bullish_divergence"], c

    # Liq proximity: a big bid wall 1% below price → short proximity
    ob = {
        "bids": [[183.0, 5], [182.5, 6], [183.2, 400], [180.0, 7]],  # 183.2 ≈ 1.2% below 185.6? within 2%
        "asks": [[186.0, 4], [186.5, 5], [187.0, 6]],
    }
    lp = liq_proximity_signal(ob, price=185.5)
    assert lp["short_proximity"] is True and lp["cluster_below"] == 183.2, lp
    assert lp["long_proximity"] is False, lp

    # Trend: steadily rising closes well above EMA → strong uptrend → block short
    rising = [{"close": 100 + i} for i in range(60)]
    tr = trend_signal(rising)
    assert tr["strong_uptrend"] is True and tr["strong_downtrend"] is False, tr
    falling = [{"close": 200 - i} for i in range(60)]
    td = trend_signal(falling)
    assert td["strong_downtrend"] is True and td["strong_uptrend"] is False, td
    print("signals selftest OK")


if __name__ == "__main__":
    _selftest()

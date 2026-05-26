"""
Pure calculation helpers: CVD, OI % change, rolling averages, volume spike.
No I/O — all functions take data in and return numbers, so they're trivially testable.
"""

from typing import List, Dict, Optional


def cvd_from_trades(trades: List[Dict]) -> float:
    """Cumulative Volume Delta from a list of trades.

    Each trade is a dict with a taker `side` ('Buy'/'Sell') and a size field
    (`size` or `qty`). CVD = aggressive-buy volume − aggressive-sell volume.
    On Bybit recent-trade, `side` is the taker side, so it maps directly.
    """
    buy = sell = 0.0
    for t in trades:
        side = str(t.get("side", "")).lower()
        try:
            qty = float(t.get("size", t.get("qty", 0)) or 0)
        except (TypeError, ValueError):
            continue
        if side in ("buy", "b"):
            buy += qty
        elif side in ("sell", "s"):
            sell += qty
    return buy - sell


def pct_change(old: float, new: float) -> Optional[float]:
    """Fractional change (new-old)/old. None if old is missing/zero."""
    if old is None or new is None or old == 0:
        return None
    return (new - old) / old


def rolling_average(values: List[float]) -> Optional[float]:
    vals = [v for v in values if v is not None]
    return sum(vals) / len(vals) if vals else None


def is_funding_spike(current: float, history: List[float], multiplier: float) -> bool:
    """True if |current| >= multiplier × |rolling avg of history|."""
    avg = rolling_average(history)
    if avg is None or avg == 0:
        return False
    return abs(current) >= multiplier * abs(avg)


def is_volume_spike(last_volume: float, recent_volumes: List[float], multiplier: float) -> bool:
    """True if last_volume >= multiplier × average of recent_volumes."""
    avg = rolling_average(recent_volumes)
    if avg is None or avg <= 0:
        return False
    return last_volume >= multiplier * avg


def ema(values: List[float], period: int) -> Optional[List[float]]:
    """Exponential moving average series (same length as values). None if too short."""
    vals = [v for v in values if v is not None]
    if len(vals) < period:
        return None
    k = 2.0 / (period + 1)
    out = [vals[0]]
    for v in vals[1:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


def _selftest():
    trades = [
        {"side": "Buy", "size": "10"}, {"side": "Sell", "size": "4"},
        {"side": "Buy", "size": "1.5"}, {"side": "Sell", "qty": 2},
    ]
    assert abs(cvd_from_trades(trades) - (11.5 - 6.0)) < 1e-9
    assert abs(pct_change(100, 125) - 0.25) < 1e-9
    assert pct_change(0, 5) is None
    assert is_funding_spike(0.0006, [0.0002, 0.0003, 0.0001], 2.0) is True
    assert is_funding_spike(0.0003, [0.0002, 0.0003, 0.0004], 2.0) is False
    assert is_volume_spike(3500, [1000, 1000, 1000], 3.0) is True
    assert is_volume_spike(2000, [1000, 1000, 1000], 3.0) is False
    print("math_utils selftest OK")


if __name__ == "__main__":
    _selftest()

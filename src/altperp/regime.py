"""
Regime classifier — labels current market conditions from 4h klines so the
router can allocate to the strategy that has an edge there (and sit out where
none does). Lightweight (no pandas), reuses trend_signal + atr.

Regimes: CRASH > TRENDING_UP/DOWN > VOLATILE > CALM > RANGING (priority order).
"""

from dataclasses import dataclass
from typing import Dict, List

from . import config
from .math_utils import atr
from .signals import trend_signal

# Canonical regime labels
CRASH = "CRASH"
TRENDING_UP = "TRENDING_UP"
TRENDING_DOWN = "TRENDING_DOWN"
VOLATILE = "VOLATILE"
CALM = "CALM"
RANGING = "RANGING"


@dataclass
class RegimeResult:
    regime: str
    atr_pct: float
    trend_slope: float
    recent_return: float
    detail: Dict


def classify(klines: List[Dict]) -> RegimeResult:
    """Classify regime from 4h klines (oldest→newest)."""
    if not klines:
        return RegimeResult(RANGING, 0.0, 0.0, 0.0, {"reason": "no_data"})
    price = klines[-1]["close"]
    a = atr(klines, config.REGIME_ATR_PERIOD)
    atr_pct = (a / price) if (a and price) else 0.0

    lb = config.REGIME_CRASH_LOOKBACK
    recent_return = 0.0
    if len(klines) > lb and klines[-1 - lb]["close"]:
        recent_return = (price - klines[-1 - lb]["close"]) / klines[-1 - lb]["close"]

    tr = trend_signal(klines)
    slope = tr.get("slope", 0.0)
    detail = {"atr_pct": atr_pct, "slope": slope, "recent_return": recent_return,
              "strong_uptrend": tr.get("strong_uptrend"), "strong_downtrend": tr.get("strong_downtrend")}

    # Priority order
    if recent_return <= config.REGIME_CRASH_RETURN_PCT:
        regime = CRASH
    elif tr.get("strong_uptrend"):
        regime = TRENDING_UP
    elif tr.get("strong_downtrend"):
        regime = TRENDING_DOWN
    elif atr_pct >= config.REGIME_VOLATILE_ATR_PCT:
        regime = VOLATILE
    elif atr_pct <= config.REGIME_CALM_ATR_PCT:
        regime = CALM
    else:
        regime = RANGING

    return RegimeResult(regime, round(atr_pct, 5), round(slope, 5), round(recent_return, 5), detail)


def _selftest():
    def bars(seq, atr_size=0.5):
        out = []
        for i, c in enumerate(seq):
            out.append({"ts": i, "open": c, "high": c + atr_size, "low": c - atr_size,
                        "close": c, "volume": 1000})
        return out

    # Steady strong uptrend
    up = classify(bars([100 + i for i in range(60)]))
    assert up.regime == TRENDING_UP, up

    # Steady downtrend
    down = classify(bars([200 - i for i in range(60)]))
    assert down.regime == TRENDING_DOWN, down

    # Crash: flat then a >15% drop in the last 6 bars
    seq = [100.0] * 54 + [100, 96, 92, 88, 84, 82]
    cr = classify(bars(seq, atr_size=0.3))
    assert cr.regime == CRASH, cr

    # Calm: flat, tiny ATR
    calm = classify(bars([100 + (i % 2) * 0.05 for i in range(60)], atr_size=0.2))
    assert calm.regime in (CALM, RANGING), calm

    # Volatile: near-flat closes (no trend) but big intrabar ranges → high ATR
    volseq = [100 + (0.1 if i % 2 else -0.1) for i in range(60)]
    vol = classify(bars(volseq, atr_size=8.0))
    assert vol.regime == VOLATILE, vol
    print("regime selftest OK")


if __name__ == "__main__":
    _selftest()

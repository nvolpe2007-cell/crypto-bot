"""
Mean-reversion strategy — covers the RANGING regime. Fade Bollinger-style
extremes (price ±MR_Z_ENTRY σ from the SMA) back toward the mean. Only routed
when the regime classifier says RANGING.

Returns an MRSetup compatible with position_manager.open_position, carrying a
`target_price` (the mean) used by the MR exit, plus `stop_frac` for sizing.
"""

import statistics
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from . import config


@dataclass
class MRSetup:
    coin: str
    direction: Optional[str] = None       # 'long' | 'short' | None
    setup_type: Optional[str] = None      # 'mr_long' | 'mr_short'
    size_multiplier: float = 1.0
    cvd_confirmed: bool = False
    liq_proximity: bool = False
    tier2_score: int = 0
    stop_frac: float = config.MR_STOP_PCT
    target_price: float = 0.0             # the mean (revert target)
    context: Dict = field(default_factory=dict)

    @property
    def should_enter(self) -> bool:
        return self.direction is not None


def evaluate(coin: str, klines: List[Dict], regime_result=None) -> MRSetup:
    """Fade price extremes vs the rolling mean. Long the dip, short the rip."""
    s = MRSetup(coin=coin)
    n = config.MR_LOOKBACK
    if not klines or len(klines) < n:
        return s
    closes = [k["close"] for k in klines[-n:]]
    mean = statistics.mean(closes)
    sd = statistics.pstdev(closes)
    price = klines[-1]["close"]
    if sd <= 0 or mean <= 0:
        return s
    z = (price - mean) / sd
    s.target_price = mean
    s.stop_frac = config.MR_STOP_PCT
    s.context = {"regime": getattr(regime_result, "regime", None),
                 "mean": round(mean, 6), "z": round(z, 3)}

    if z <= -config.MR_Z_ENTRY:
        s.direction = "long"          # bought the dip, target = mean above
        s.setup_type = "mr_long"
    elif z >= config.MR_Z_ENTRY:
        s.direction = "short"         # sold the rip, target = mean below
        s.setup_type = "mr_short"
    return s


def _selftest():
    # A range: mostly ~100, then a sharp dip well below the mean → mr_long
    closes = [100 + (1 if i % 2 else -1) for i in range(19)] + [90.0]
    kl = [{"ts": i, "open": c, "high": c + 0.5, "low": c - 0.5, "close": c, "volume": 1000}
          for i, c in enumerate(closes)]
    s = evaluate("SOLUSDT", kl)
    assert s.should_enter and s.direction == "long" and s.setup_type == "mr_long", s
    assert s.target_price > 90, s   # target is the mean (~100), above the dip

    # A spike above the mean → mr_short
    closes2 = [100 + (1 if i % 2 else -1) for i in range(19)] + [110.0]
    kl2 = [{"ts": i, "open": c, "high": c + 0.5, "low": c - 0.5, "close": c, "volume": 1000}
           for i, c in enumerate(closes2)]
    s2 = evaluate("SOLUSDT", kl2)
    assert s2.should_enter and s2.direction == "short", s2

    # Price near the mean → no trade
    flat = [{"ts": i, "open": 100, "high": 100.5, "low": 99.5, "close": 100, "volume": 1000}
            for i in range(20)]
    s3 = evaluate("SOLUSDT", flat)
    assert not s3.should_enter, s3
    print("mean_reversion selftest OK")


if __name__ == "__main__":
    _selftest()

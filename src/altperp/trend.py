"""
Trend-following strategy — covers the TRENDING regimes the fade/MR strategies
get run over in. Classic Donchian breakout entry + ATR chandelier exit (handled
in exits.py): go with an established trend, ride it, cut fast when it breaks.

Long  when price breaks above the highest high of the last N bars (uptrend).
Short when price breaks below the lowest low of the last N bars (downtrend).
Only in a TRENDING regime with enough volatility to bother.

Returns a TrendSetup compatible with position_manager.open_position (same duck
type as confluence.Setup) plus `stop_frac` and `atr` for ATR-based sizing/exits.
"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional

from . import config, regime as rg
from .math_utils import atr


@dataclass
class TrendSetup:
    coin: str
    direction: Optional[str] = None       # 'long' | 'short' | None
    setup_type: Optional[str] = None      # 'trend_long' | 'trend_short'
    size_multiplier: float = 1.0
    cvd_confirmed: bool = False
    liq_proximity: bool = False
    tier2_score: int = 0
    stop_frac: float = 0.0                # ATR-based stop distance (fraction of price)
    atr: float = 0.0                      # ATR in price units (for chandelier stop)
    context: Dict = field(default_factory=dict)

    @property
    def should_enter(self) -> bool:
        return self.direction is not None


def evaluate(coin: str, klines: List[Dict], regime_result) -> TrendSetup:
    """Breakout entry in a trending regime. `regime_result` from regime.classify."""
    s = TrendSetup(coin=coin)
    n = config.TREND_BREAKOUT_BARS
    if not klines or len(klines) < n + 2:
        return s

    price = klines[-1]["close"]
    a = atr(klines, config.REGIME_ATR_PERIOD)
    if not a or price <= 0 or (a / price) < config.TREND_MIN_ATR_PCT:
        return s

    prior = klines[-(n + 1):-1]            # last N bars, excluding the current
    hh = max(b["high"] for b in prior)
    ll = min(b["low"] for b in prior)
    s.atr = a
    s.stop_frac = config.TREND_ATR_STOP_MULT * a / price
    s.context = {"regime": regime_result.regime, "atr_pct": round(a / price, 5),
                 "breakout_high": hh, "breakout_low": ll}

    reg = regime_result.regime
    if reg == rg.TRENDING_UP and price >= hh:
        s.direction = "long"
        s.setup_type = "trend_long"
    elif reg == rg.TRENDING_DOWN and price <= ll:
        s.direction = "short"
        s.setup_type = "trend_short"
    return s


def _selftest():
    from .regime import classify, TRENDING_UP

    def bars(seq, atr_size=1.0):
        return [{"ts": i, "open": c, "high": c + atr_size, "low": c - atr_size,
                 "close": c, "volume": 1000} for i, c in enumerate(seq)]

    # Strong uptrend, step > bar range so the close breaks prior highs → long breakout
    seq = [100 + i * 2.0 for i in range(60)]
    kl = bars(seq, atr_size=1.0)
    reg = classify(kl)
    assert reg.regime == TRENDING_UP, reg
    s = evaluate("SOLUSDT", kl, reg)
    assert s.should_enter and s.direction == "long" and s.setup_type == "trend_long", s
    assert s.stop_frac > 0 and s.atr > 0, s

    # Flat/calm → no trend trade
    flat = bars([100 + (i % 2) * 0.1 for i in range(60)], atr_size=0.2)
    s2 = evaluate("SOLUSDT", flat, classify(flat))
    assert not s2.should_enter, s2
    print("trend selftest OK")


if __name__ == "__main__":
    _selftest()

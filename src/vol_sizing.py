"""
Realized-volatility (inverse-vol) position sizing.

The strongest-proven sizing result in the research is volatility targeting: size
∝ target_vol / realized_vol, which holds portfolio risk roughly constant — it
sharply cuts drawdown and lifts risk-adjusted return (a real backtest moved Sharpe
0.99→1.54, maxDD −31%→−14%). The bot already has an IMPLIED-vol sizer
(`CryptoVolMonitor`) but it only covers BTC/ETH (Deribit options) and returns 1.0
for every alt — so alts size flat. This is the universal REALIZED-vol complement:
it works for every symbol from the ATR the strategy already computes, no options
feed needed.

Bounded and env-gated so it can never over- or under-size pathologically:
  multiplier = clamp(target_atr_pct / realized_atr_pct, FLOOR, CAP)
At realized == target the multiplier is 1.0 (neutral); higher vol → size down,
lower vol → size up, within [FLOOR, CAP]. Pure + unit-testable.

NOTE: applied on the DIRECTIONAL main-loop sizing only (its natural home, beside
the IV sizer). It is deliberately NOT applied to the swing / funding / brain
forward-tests, whose pre-registered proofs assume uniform sizing — changing that
mid-flight would corrupt an in-progress 90-day proof.
"""
from __future__ import annotations

import os

# Master switch (default ON — vol targeting is the proven default).
ENABLED = os.getenv("VOL_TARGET_SIZING", "1") == "1"
# Target per-bar ATR-as-fraction-of-price the book is sized to. 0.4% is a
# reasonable middle for the 1m majors this path trades; env-tunable.
TARGET_ATR_PCT = float(os.getenv("VOL_TARGET_ATR_PCT", "0.004"))
# Hard bounds so a quiet or wild tape can't blow size up/down without limit.
FLOOR = float(os.getenv("VOL_SIZE_FLOOR", "0.5"))
CAP = float(os.getenv("VOL_SIZE_CAP", "1.5"))


def realized_vol_multiplier(atr_pct: float | None,
                            target: float | None = None,
                            floor: float | None = None,
                            cap: float | None = None) -> float:
    """clamp(target / realized_atr_pct, floor, cap). Neutral (1.0) on missing or
    non-positive vol so it never fabricates a size change from bad data."""
    target = TARGET_ATR_PCT if target is None else target
    floor = FLOOR if floor is None else floor
    cap = CAP if cap is None else cap
    if not atr_pct or atr_pct <= 0:
        return 1.0
    return max(floor, min(cap, target / atr_pct))


def apply_vol_target(size_usd: float, atr: float | None, price: float | None) -> float:
    """Scale a USD size by the realized-vol multiplier. No-op when disabled or
    when ATR/price are unavailable (fail-neutral). `atr` is the absolute ATR;
    realized_atr_pct = atr / price."""
    if not ENABLED or not atr or not price or price <= 0:
        return size_usd
    return size_usd * realized_vol_multiplier(atr / price)

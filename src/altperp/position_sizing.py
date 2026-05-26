"""
Risk-based position sizing.

Core: risk a fixed % of equity per trade, derived from the stop distance, then
apply the confluence size multiplier and a hard leverage cap.

    risk_usdt    = equity × BASE_RISK_PCT × size_multiplier
    notional     = risk_usdt / stop_distance_frac        (USDT exposure)
    notional     = min(notional, equity × MAX_LEVERAGE)  (leverage cap)
    qty          = notional / price

`size_multiplier` (1.0 / 1.5 / 2.0) scales the RISK, not raw notional — so higher
conviction risks more, but the leverage cap still bounds total exposure.
"""

from dataclasses import dataclass
from typing import Optional

from . import config


@dataclass
class SizePlan:
    notional_usdt: float        # position notional (USDT exposure)
    qty: float                  # contracts / base units
    risk_usdt: float            # intended $ risk if stop is hit
    leverage_used: float        # notional / equity
    stop_distance_frac: float   # stop distance as a fraction of price
    capped_by_leverage: bool


def stop_distance_frac(direction: str, setup_type: str) -> float:
    """Fractional stop distance from entry for the given setup."""
    if direction == "short":
        return config.SHORT_STOP_PCT
    return config.LONG_STOP_PCT


def compute_size(equity: float,
                 price: float,
                 direction: str,
                 setup_type: str,
                 size_multiplier: float,
                 stop_frac: float = None) -> Optional[SizePlan]:
    """Return a SizePlan, or None if inputs are unusable / size rounds to zero.

    `stop_frac` override is used by the trend strategy (ATR-based stop distance);
    fade/flush use the fixed per-setup stops.
    """
    if equity <= 0 or price <= 0 or size_multiplier <= 0:
        return None
    if stop_frac is None:
        stop_frac = stop_distance_frac(direction, setup_type)
    if stop_frac <= 0:
        return None

    risk_usdt = equity * config.BASE_RISK_PCT * size_multiplier
    notional = risk_usdt / stop_frac

    # Hard leverage cap — never exceed MAX_LEVERAGE of equity, regardless of conviction.
    max_notional = equity * config.MAX_LEVERAGE
    capped = notional > max_notional
    if capped:
        notional = max_notional
        # risk actually taken shrinks to match the capped notional
        risk_usdt = notional * stop_frac

    qty = notional / price
    if qty <= 0:
        return None
    return SizePlan(
        notional_usdt=round(notional, 4),
        qty=qty,
        risk_usdt=round(risk_usdt, 4),
        leverage_used=round(notional / equity, 3),
        stop_distance_frac=stop_frac,
        capped_by_leverage=capped,
    )


def _selftest():
    eq = 1000.0
    # Short, 1.0x: risk $10 at 2% stop → $500 notional, 0.5x leverage
    p = compute_size(eq, 185.0, "short", "fade_short", 1.0)
    assert abs(p.risk_usdt - 10.0) < 1e-6 and abs(p.notional_usdt - 500.0) < 1e-6, p
    assert not p.capped_by_leverage and abs(p.leverage_used - 0.5) < 1e-6, p

    # Short, 2.0x: risk $20 → $1000 notional, 1.0x leverage (still < 5x cap)
    p2 = compute_size(eq, 185.0, "short", "fade_short", 2.0)
    assert abs(p2.notional_usdt - 1000.0) < 1e-6 and not p2.capped_by_leverage, p2

    # Tiny stop would blow past leverage cap → capped at 5x
    cfg_stop = config.SHORT_STOP_PCT
    try:
        config.SHORT_STOP_PCT = 0.001   # 0.1% stop → 10x notional uncapped
        p3 = compute_size(eq, 185.0, "short", "fade_short", 1.0)
        assert p3.capped_by_leverage and abs(p3.leverage_used - config.MAX_LEVERAGE) < 1e-6, p3
        assert abs(p3.notional_usdt - eq * config.MAX_LEVERAGE) < 1e-6, p3
    finally:
        config.SHORT_STOP_PCT = cfg_stop
    print("position_sizing selftest OK")


if __name__ == "__main__":
    _selftest()

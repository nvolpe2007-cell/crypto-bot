"""
All-weather router — the keystone. Given the current regime, decide which
strategy is eligible and at what size scale, then pick the routed decision among
the strategies that actually fired. Sits the directional book out entirely in
regimes where nothing has an edge (CALM), so "all-weather" means *coverage*, not
forcing trades.

Strategy names are plug-ins; the runner supplies each one's evaluated decision
(or None). Strategies not yet built simply never fire — the router degrades
gracefully as new ones (trend, mean-reversion) are added.

The market-neutral funding arb is NOT routed here — it runs continuously as the
always-on base layer regardless of regime.
"""

from dataclasses import dataclass
from typing import Dict, Optional

from . import regime as rg

# Which directional strategies have an edge in each regime, in priority order.
REGIME_STRATEGIES = {
    rg.TRENDING_UP:   ["trend"],
    rg.TRENDING_DOWN: ["trend"],
    rg.RANGING:       ["mean_reversion"],
    rg.VOLATILE:      ["fade"],            # froth/extreme funding fade
    rg.CRASH:         ["flush"],           # post-liquidation bounce
    rg.CALM:          [],                  # no directional edge → sit out
}

# Portfolio size scaling by regime (risk-off where edge is weaker / tail-heavy).
REGIME_SIZE_SCALE = {
    rg.TRENDING_UP:   1.0,
    rg.TRENDING_DOWN: 1.0,
    rg.RANGING:       0.8,
    rg.VOLATILE:      0.5,    # fades are negatively skewed — smaller in chop/froth
    rg.CRASH:         0.5,    # flush longs are high-risk
    rg.CALM:          0.0,
}


@dataclass
class RouteDecision:
    regime: str
    active_strategy: Optional[str]      # None → flat
    decision: object = None             # the chosen strategy's decision object
    size_scale: float = 0.0
    reason: str = ""


def eligible_strategies(regime: str):
    return REGIME_STRATEGIES.get(regime, [])


def route(regime: str, decisions: Dict[str, object]) -> RouteDecision:
    """`decisions` maps strategy_name → its evaluated decision (truthy if it wants
    to trade, else None/falsey). Returns the routed decision for this regime."""
    scale = REGIME_SIZE_SCALE.get(regime, 0.0)
    elig = eligible_strategies(regime)
    if not elig or scale <= 0:
        return RouteDecision(regime, None, None, 0.0, "no_edge_in_regime")
    for name in elig:                       # priority order
        dec = decisions.get(name)
        if dec:
            return RouteDecision(regime, name, dec, scale, "routed")
    return RouteDecision(regime, None, None, scale, "eligible_strategy_no_signal")


def _selftest():
    # Trending up routes to trend (if it fired), scaled 1.0
    r = route(rg.TRENDING_UP, {"trend": object(), "fade": object()})
    assert r.active_strategy == "trend" and r.size_scale == 1.0, r

    # Volatile routes to fade, scaled 0.5
    r2 = route(rg.VOLATILE, {"fade": object()})
    assert r2.active_strategy == "fade" and r2.size_scale == 0.5, r2

    # Calm → always flat, even if a strategy "fired"
    r3 = route(rg.CALM, {"trend": object(), "fade": object()})
    assert r3.active_strategy is None and r3.size_scale == 0.0, r3

    # Eligible regime but the strategy didn't fire → flat with reason
    r4 = route(rg.RANGING, {"mean_reversion": None})
    assert r4.active_strategy is None and r4.reason == "eligible_strategy_no_signal", r4

    # Regime's strategy not built yet (no key) → flat, graceful
    r5 = route(rg.TRENDING_DOWN, {})
    assert r5.active_strategy is None, r5
    print("router selftest OK")


if __name__ == "__main__":
    _selftest()

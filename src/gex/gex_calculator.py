"""Dealer gamma exposure (GEX) from an option chain.

SIGN CONVENTION -- READ THIS FIRST
-----------------------------------
Deribit's public data tells us open interest per strike, not who is on which
side of it. Nobody outside the exchange knows dealers' actual net position.
Every public "GEX" number (this one included) rests on an ASSUMPTION about
dealer positioning, not a measurement of it. The assumption used here is the
one most public GEX trackers use (SqueezeMetrics-style, popularized for SPX):

    dealers are net LONG the calls they've sold to customers is WRONG --
    the actual convention is: dealers are assumed short whatever customers
    are net long. Since we cannot see customer-vs-dealer flow, the standard
    simplification is:
        net dealer gamma at a strike = (call OI - put OI) * gamma_BS(strike)
    i.e. call open interest contributes POSITIVE dealer gamma, put open
    interest contributes NEGATIVE dealer gamma. This is an assumption, not a
    fact -- it can be wrong for any given strike, and there is no way to
    verify it from public data. Treat every number this module produces as
    conditional on that assumption holding on average across the chain.

WHAT "POSITIVE"/"NEGATIVE" GEX MEANS IF THE ASSUMPTION HOLDS
    Positive net dealer gamma: dealers are long gamma, so their hedging
    (buying dips / selling rallies to stay delta-neutral) DAMPENS price
    moves -- the textbook case for a fade-the-wall / mean-reversion regime.
    Negative net dealer gamma: dealers are short gamma, hedging AMPLIFIES
    moves in the direction they're already going -- trends can extend
    through this zone; fading it fights dealer flow instead of riding it.

CONTRACT SIZE: Deribit BTC/ETH options have contract_size=1 (each option
controls 1 unit of the underlying), so the multiplier used elsewhere for
equity options (100 shares/contract) is 1 here, folded into the formula
below without an explicit constant.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from .black_scholes import gamma as bs_gamma
from .deribit_client import OptionQuote

# GEX is conventionally scaled by spot^2 * 0.01 (dollar/BTC exposure per 1%
# move in the underlying) rather than raw spot^2, purely so the numbers are a
# readable "exposure per 1% move" rather than an arbitrary large float. This
# scaling does not change the SIGN or the zero-crossing (flip) point.
GEX_PCT_MOVE_SCALE = 0.01


@dataclass(frozen=True)
class StrikeGex:
    strike: float
    net_gex: float  # signed: + = call-dominated (dealer long gamma at this strike)
    call_oi: float
    put_oi: float


def _time_to_expiry_years(expiry_ms: int, as_of: datetime | None = None) -> float:
    as_of = as_of or datetime.now(timezone.utc)
    expiry_dt = datetime.fromtimestamp(expiry_ms / 1000, tz=timezone.utc)
    seconds = (expiry_dt - as_of).total_seconds()
    return max(seconds, 0.0) / (365.0 * 24 * 3600)


def compute_gex_by_strike(
    chain: list[OptionQuote],
    spot: float,
    as_of: datetime | None = None,
) -> list[StrikeGex]:
    """Net dealer GEX per strike at the CURRENT spot price (see module
    docstring for the sign convention this rests on)."""
    by_strike: dict[float, dict[str, float]] = {}
    for q in chain:
        t = _time_to_expiry_years(q.expiration_timestamp_ms, as_of)
        g = bs_gamma(spot, q.strike, t, q.mark_iv_pct / 100.0)
        exposure = g * q.open_interest * spot * spot * GEX_PCT_MOVE_SCALE
        bucket = by_strike.setdefault(q.strike, {"call": 0.0, "put": 0.0, "call_oi": 0.0, "put_oi": 0.0})
        if q.option_type == "call":
            bucket["call"] += exposure
            bucket["call_oi"] += q.open_interest
        else:
            bucket["put"] += exposure
            bucket["put_oi"] += q.open_interest

    return sorted(
        (
            StrikeGex(
                strike=k,
                net_gex=v["call"] - v["put"],
                call_oi=v["call_oi"],
                put_oi=v["put_oi"],
            )
            for k, v in by_strike.items()
        ),
        key=lambda s: s.strike,
    )


def net_gex_at_hypothetical_spot(
    chain: list[OptionQuote],
    hypothetical_spot: float,
    as_of: datetime | None = None,
) -> float:
    """Total net dealer GEX if spot were `hypothetical_spot` right now, holding
    strikes/OI/IV fixed. This is the standard (if imperfect -- it ignores that
    IV itself would shift with spot, i.e. the vol smile) approximation public
    gamma-flip trackers use: gamma depends on spot through Black-Scholes even
    though OI and quoted IV don't change with a hypothetical repricing."""
    total = 0.0
    for q in chain:
        t = _time_to_expiry_years(q.expiration_timestamp_ms, as_of)
        g = bs_gamma(hypothetical_spot, q.strike, t, q.mark_iv_pct / 100.0)
        exposure = g * q.open_interest * hypothetical_spot * hypothetical_spot * GEX_PCT_MOVE_SCALE
        total += exposure if q.option_type == "call" else -exposure
    return total


def find_zero_gamma_flip(
    chain: list[OptionQuote],
    spot: float,
    as_of: datetime | None = None,
    search_pct: float = 0.30,
    n_points: int = 200,
) -> float | None:
    """Scan hypothetical spot prices in [spot*(1-search_pct), spot*(1+search_pct)]
    and linearly interpolate the first sign change in net GEX. Returns None if
    net GEX doesn't change sign across the search range (no flip point found
    within +/- search_pct of current spot)."""
    if not chain or spot <= 0:
        return None
    lo, hi = spot * (1 - search_pct), spot * (1 + search_pct)
    step = (hi - lo) / n_points
    prev_x, prev_y = lo, net_gex_at_hypothetical_spot(chain, lo, as_of)
    for i in range(1, n_points + 1):
        x = lo + i * step
        y = net_gex_at_hypothetical_spot(chain, x, as_of)
        if prev_y == 0.0:
            return prev_x
        if (prev_y < 0) != (y < 0):
            # linear interpolation between (prev_x, prev_y) and (x, y)
            frac = prev_y / (prev_y - y)
            return prev_x + frac * (x - prev_x)
        prev_x, prev_y = x, y
    return None


def atm_iv_near_tenor(
    chain: list[OptionQuote],
    spot: float,
    target_days: float = 30.0,
    as_of: datetime | None = None,
) -> float | None:
    """ATM-proxy IV from the expiry closest to `target_days` out, not simply
    the nearest expiry -- the nearest expiry can be hours from settlement
    (a 0DTE-like quote), whose IV isn't comparable to a rolling multi-day
    realized-vol index. Picks the closest-to-spot strike within that expiry."""
    if not chain:
        return None
    as_of = as_of or datetime.now(timezone.utc)
    target_ms = as_of.timestamp() * 1000 + target_days * 24 * 3600 * 1000
    best_expiry = min(
        {q.expiration_timestamp_ms for q in chain},
        key=lambda ms: abs(ms - target_ms),
    )
    same_expiry = [q for q in chain if q.expiration_timestamp_ms == best_expiry]
    closest = min(same_expiry, key=lambda q: abs(q.strike - spot))
    return closest.mark_iv_pct


def find_walls(strikes: list[StrikeGex], top_n: int = 3) -> tuple[list[StrikeGex], list[StrikeGex]]:
    """(ceiling candidates, floor candidates): the `top_n` strikes with the
    largest positive net_gex (ceiling -- dealers long gamma, resist upside)
    and the largest-magnitude negative net_gex (floor -- dealers long gamma
    on the put side, resist downside), sorted strongest first."""
    positive = sorted((s for s in strikes if s.net_gex > 0), key=lambda s: -s.net_gex)
    negative = sorted((s for s in strikes if s.net_gex < 0), key=lambda s: s.net_gex)
    return positive[:top_n], negative[:top_n]

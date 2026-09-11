"""Black-Scholes gamma — the one Greek this module needs.

Deribit's public book-summary endpoint gives open interest and mark IV per
instrument but not per-instrument greeks (fetching greeks one instrument at a
time would be ~1000 API calls). Gamma is cheap to compute directly from
(spot, strike, time-to-expiry, IV) and, unlike delta/theta, is identical for a
call and a put at the same strike/expiry under Black-Scholes -- so one
function covers the whole chain.

r=0 by default: Deribit options are inverse (premium and settlement in the
underlying crypto), and the "get_book_summary_by_currency" interest_rate
field is consistently ~0 in practice. This is the standard simplification
used by public dealer-gamma trackers, not a claim that crypto risk-free rates
are actually zero.
"""
from __future__ import annotations

import math


def _norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def d1(spot: float, strike: float, t_years: float, iv: float, r: float = 0.0) -> float:
    if spot <= 0 or strike <= 0 or t_years <= 0 or iv <= 0:
        raise ValueError(
            f"d1 requires positive spot/strike/t_years/iv, got "
            f"spot={spot}, strike={strike}, t_years={t_years}, iv={iv}"
        )
    return (math.log(spot / strike) + (r + 0.5 * iv * iv) * t_years) / (
        iv * math.sqrt(t_years)
    )


def gamma(spot: float, strike: float, t_years: float, iv: float, r: float = 0.0) -> float:
    """Black-Scholes gamma: d^2(price)/d(spot)^2, same for call and put.

    Returns 0.0 for an already-expired or degenerate contract (t_years <= 0)
    rather than raising -- callers scanning a real chain will hit a few of
    these near expiry and should treat them as contributing nothing.
    """
    if t_years <= 0 or spot <= 0 or strike <= 0 or iv <= 0:
        return 0.0
    d1_ = d1(spot, strike, t_years, iv, r)
    return _norm_pdf(d1_) / (spot * iv * math.sqrt(t_years))

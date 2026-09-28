"""
Fair probability for Polymarket "Bitcoin Up or Down" windows.

The markets resolve on a Chainlink BTC/USD TWAP, so part-way through a window
the outcome is partly LOCKED IN by the average already accumulated. That makes
P(Up) something we can compute, not guess:

  window rule   - Up iff the TWAP over the whole window >= the start price.
                  final = (A_e*t_e + R*t_r) / T, where A_e is the elapsed average
                  and R the average of the remaining t_r seconds. Under a
                  driftless random walk from spot S, R ~ N(S, (sigma*S)^2 * t_r/3),
                  so final ~ N(mean, sd) with sd = sigma*S*t_r^1.5 / (sqrt(3)*T).
  endpoint rule - Up iff the (short-TWAP) price at the end >= the start price;
                  a plain digital: P = Phi(ln(S/S0) / (sigma*sqrt(t_r))).

Which rule Polymarket actually applies is measured, not assumed — see
scripts/polymarket_twap_backtest.py, which scores both against real resolutions.

Fees: Polymarket's documented taker fee is fee = C * rate * p * (1 - p) with
rate 0.07 for the crypto category (1.75c/share at p=0.50, ~3.5% of notional).
The market API reports feeType crypto_fees_v2 / base_fee 1000, which may mean a
higher rate — callers should sensitivity-test FEE_RATE, not trust it.
"""

from __future__ import annotations

from math import erf, log, sqrt
from typing import Sequence

WINDOW_S = 900
FEE_RATE = 0.07


def _phi(x: float) -> float:
    return 0.5 * (1.0 + erf(x / sqrt(2.0)))


def prob_up_window_twap(
    start_price: float,
    elapsed_avg: float,
    elapsed_s: float,
    spot: float,
    sigma_per_s: float,
    window_s: float = WINDOW_S,
) -> float:
    """P(window TWAP >= start_price) given the average accumulated so far."""
    t_e = min(max(elapsed_s, 0.0), window_s)
    t_r = window_s - t_e
    avg_so_far = elapsed_avg if t_e > 0 else spot
    mean = (avg_so_far * t_e + spot * t_r) / window_s
    sd = sigma_per_s * spot * t_r ** 1.5 / (sqrt(3.0) * window_s)
    if sd <= 0:
        return 1.0 if mean >= start_price else 0.0
    return _phi((mean - start_price) / sd)


def prob_up_endpoint(start_price: float, spot: float, remaining_s: float, sigma_per_s: float) -> float:
    """P(price at window end >= start_price) — plain driftless digital."""
    sd = sigma_per_s * sqrt(max(remaining_s, 0.0))
    if sd <= 0:
        return 1.0 if spot >= start_price else 0.0
    return _phi(log(spot / start_price) / sd)


def sigma_per_second(closes: Sequence[float], bar_s: float = 60.0) -> float:
    """Realized volatility per sqrt(second) from consecutive bar closes."""
    rets = [log(b / a) for a, b in zip(closes, closes[1:]) if a > 0 and b > 0]
    if len(rets) < 2:
        return 0.0
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    return sqrt(var / bar_s)


def taker_fee_per_share(price: float, rate: float = FEE_RATE) -> float:
    return rate * price * (1.0 - price)


def edge_per_share(fair_prob: float, ask: float, rate: float = FEE_RATE) -> float:
    """Expected profit per share of buying an outcome at `ask`, net of taker fee."""
    return fair_prob - ask - taker_fee_per_share(ask, rate)

"""
Fair P(Up) for Kalshi KXBTC15M ("BTC price up in next 15 mins?").

Settlement rule (from the contract's rules_primary): YES iff the simple average
of the 60 BRTI prints in the minute before the close is >= the average of the
60 prints in the minute before the open. That opening average is published as
`floor_strike`, so the target is known exactly; only the live price is a proxy.

Ending on a 60s average instead of a point lowers the variance: for a driftless
walk with r seconds remaining (r >= 60), Var(avg) = sigma^2 S^2 (r - 40). So the
endpoint digital is evaluated at an effective horizon of r - 40 seconds.

Fees: Kalshi's quadratic taker fee, fee = ceil_to_cent(0.07 * C * P * (1 - P))
per order (series fee_type "quadratic", fee_multiplier 1).
"""

from __future__ import annotations

from math import ceil

from polymarket_bot.fair_value import WINDOW_S, prob_up_endpoint, sigma_per_second  # noqa: F401

FEE_RATE = 0.07
AVG_S = 60


def effective_remaining(remaining_s: float) -> float:
    if remaining_s >= AVG_S:
        return remaining_s - AVG_S * 2 / 3
    # inside the averaging minute: only the unobserved part still moves
    return remaining_s ** 3 / (3 * AVG_S ** 2)


def prob_up(strike: float, spot: float, remaining_s: float, sigma_per_s: float) -> float:
    return prob_up_endpoint(strike, spot, effective_remaining(remaining_s), sigma_per_s)


def taker_fee(contracts: float, price: float, rate: float = FEE_RATE) -> float:
    """Total fee in dollars for one order, rounded UP to the cent."""
    raw = rate * contracts * price * (1.0 - price)
    return ceil(round(raw * 100, 9)) / 100


def edge_per_contract(fair_prob: float, ask: float, contracts: float, rate: float = FEE_RATE) -> float:
    """Expected profit per contract of buying at `ask`, net of the order's fee."""
    return fair_prob - ask - taker_fee(contracts, ask, rate) / contracts

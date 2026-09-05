"""Tests for src/gex/ -- Black-Scholes gamma, per-strike GEX, and the
zero-gamma flip-point scan. All synthetic data; no network calls."""

from datetime import datetime, timedelta, timezone

import pytest

from src.gex.black_scholes import gamma as bs_gamma
from src.gex.deribit_client import OptionQuote
from src.gex.gex_calculator import (
    atm_iv_near_tenor,
    compute_gex_by_strike,
    find_walls,
    find_zero_gamma_flip,
    net_gex_at_hypothetical_spot,
)

AS_OF = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _quote(strike, option_type, oi, expiry_days=30, iv_pct=60.0, spot=50_000.0):
    expiry = AS_OF + timedelta(days=expiry_days)
    return OptionQuote(
        instrument_name=f"BTC-TEST-{strike}-{'C' if option_type == 'call' else 'P'}",
        strike=float(strike),
        option_type=option_type,
        expiration_timestamp_ms=int(expiry.timestamp() * 1000),
        open_interest=float(oi),
        mark_iv_pct=iv_pct,
        underlying_price=spot,
    )


def test_bs_gamma_positive_and_symmetric_around_atm():
    # Gamma is highest ATM and falls off away from the strike either direction.
    atm = bs_gamma(50_000, 50_000, 30 / 365, 0.60)
    otm_call = bs_gamma(50_000, 60_000, 30 / 365, 0.60)
    otm_put_side = bs_gamma(50_000, 40_000, 30 / 365, 0.60)
    assert atm > 0
    assert atm > otm_call > 0
    assert atm > otm_put_side > 0


def test_bs_gamma_zero_for_expired_or_bad_input():
    assert bs_gamma(50_000, 50_000, 0.0, 0.60) == 0.0
    assert bs_gamma(50_000, 50_000, -1.0, 0.60) == 0.0
    assert bs_gamma(0, 50_000, 30 / 365, 0.60) == 0.0
    assert bs_gamma(50_000, 50_000, 30 / 365, 0.0) == 0.0


def test_gex_by_strike_call_positive_put_negative():
    spot = 50_000.0
    chain = [
        _quote(50_000, "call", oi=1000, spot=spot),
        _quote(50_000, "put", oi=200, spot=spot),
    ]
    strikes = compute_gex_by_strike(chain, spot, as_of=AS_OF)
    assert len(strikes) == 1
    s = strikes[0]
    assert s.strike == 50_000
    assert s.call_oi == 1000
    assert s.put_oi == 200
    # more call OI than put OI at the same strike/gamma -> net positive
    assert s.net_gex > 0


def test_gex_by_strike_put_dominant_is_negative():
    spot = 50_000.0
    chain = [
        _quote(50_000, "call", oi=100, spot=spot),
        _quote(50_000, "put", oi=900, spot=spot),
    ]
    strikes = compute_gex_by_strike(chain, spot, as_of=AS_OF)
    assert strikes[0].net_gex < 0


def test_find_walls_picks_strongest_strikes_each_side():
    spot = 50_000.0
    chain = [
        _quote(52_000, "call", oi=5000, spot=spot),  # strong ceiling candidate
        _quote(51_000, "call", oi=500, spot=spot),  # weaker ceiling candidate
        _quote(48_000, "put", oi=4000, spot=spot),  # strong floor candidate
        _quote(49_000, "put", oi=300, spot=spot),  # weaker floor candidate
    ]
    strikes = compute_gex_by_strike(chain, spot, as_of=AS_OF)
    ceilings, floors = find_walls(strikes, top_n=2)
    assert ceilings[0].strike == 52_000
    assert ceilings[1].strike == 51_000
    assert floors[0].strike == 48_000
    assert floors[1].strike == 49_000


def test_zero_gamma_flip_finds_crossing_between_call_and_put_dominant_regions():
    # Heavy call OI far above spot (net positive there), heavy put OI far
    # below (net negative there) -- net GEX at current spot should sit
    # between the two, and the flip search should find *a* crossing within
    # the scanned range without erroring, landing strictly between the two
    # strikes that define the regime change.
    spot = 50_000.0
    chain = [
        _quote(58_000, "call", oi=5000, spot=spot),
        _quote(42_000, "put", oi=5000, spot=spot),
    ]
    flip = find_zero_gamma_flip(chain, spot, as_of=AS_OF, search_pct=0.30)
    assert flip is not None
    assert 42_000 < flip < 58_000


def test_zero_gamma_flip_none_when_chain_empty():
    assert find_zero_gamma_flip([], 50_000.0, as_of=AS_OF) is None


def test_atm_iv_near_tenor_prefers_30d_expiry_over_0dte():
    spot = 50_000.0
    zero_dte = _quote(50_000, "call", oi=100, expiry_days=0.1, iv_pct=15.0, spot=spot)
    thirty_day = _quote(50_050, "call", oi=100, expiry_days=30, iv_pct=55.0, spot=spot)
    far_dated = _quote(50_000, "call", oi=100, expiry_days=180, iv_pct=70.0, spot=spot)
    iv = atm_iv_near_tenor([zero_dte, thirty_day, far_dated], spot, target_days=30.0, as_of=AS_OF)
    assert iv == 55.0


def test_net_gex_at_hypothetical_spot_matches_compute_gex_by_strike_sum():
    spot = 50_000.0
    chain = [
        _quote(50_000, "call", oi=1000, spot=spot),
        _quote(50_000, "put", oi=200, spot=spot),
        _quote(52_000, "call", oi=300, spot=spot),
    ]
    strikes = compute_gex_by_strike(chain, spot, as_of=AS_OF)
    total_from_strikes = sum(s.net_gex for s in strikes)
    total_hypothetical = net_gex_at_hypothetical_spot(chain, spot, as_of=AS_OF)
    assert total_from_strikes == pytest.approx(total_hypothetical, rel=1e-9)

#!/usr/bin/env python3
"""
Print live BTC gamma levels (from Deribit's free options chain) in the exact
text format the TradingView indicator "Gamma Exposure Levels [BackQuant]"
parses — paste the output into its "Paste GEX Levels Data" box.

    python scripts/gex_to_backquant.py            # BTC
    python scripts/gex_to_backquant.py --currency ETH

Level definitions (all rest on src/gex's dealer-positioning ASSUMPTION — calls
= dealer long gamma, puts = dealer short gamma; public data cannot verify it):
  HVL              strike with the largest GROSS gamma exposure (calls + puts)
  Call Resistance  strike with the largest positive net GEX
  Put Support      strike with the most negative net GEX
  0DTE *           the same three, using only options expiring within 24h
  Zero Gamma       spot where total net GEX crosses zero (nearest to spot)
  Flip Zones       every net-GEX sign change within +/-15% of spot
  Max Pain         strike minimising option-holder payout, nearest expiry >12h out
  Expected Move    +/-1 sigma over one day from the ~1-day ATM IV

These are levels to LOOK at, not a tested signal: whether BTC respects them is
unmeasured (registry gex-dealer-exposure-walls, status infrastructure).

Parser notes (why the layout is what it is): the indicator finds each key with
str.pos(), so the all-expiry "HVL:" line must come before "0DTE HVL:" (which
contains it); flip zones are one line separated by ", "; each Top-10 block ends
with a "━" rule, which is what stops the indicator's line scanner.
"""

from __future__ import annotations

import argparse
import math
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.gex.black_scholes import gamma as bs_gamma  # noqa: E402
from src.gex.deribit_client import OptionQuote, fetch_option_chain  # noqa: E402
from src.gex.gex_calculator import (  # noqa: E402
    GEX_PCT_MOVE_SCALE,
    _time_to_expiry_years,
    compute_gex_by_strike,
    net_gex_at_hypothetical_spot,
)

DAY_MS = 24 * 3600 * 1000
RULE = "━" * 24


def usd(x: float) -> str:
    return f"${x:,.0f}"


def gross_by_strike(chain: list[OptionQuote], spot: float, as_of: datetime) -> dict[float, float]:
    out: dict[float, float] = {}
    for q in chain:
        t = _time_to_expiry_years(q.expiration_timestamp_ms, as_of)
        g = bs_gamma(spot, q.strike, t, q.mark_iv_pct / 100.0)
        out[q.strike] = out.get(q.strike, 0.0) + g * q.open_interest * spot * spot * GEX_PCT_MOVE_SCALE
    return out


def core_levels(chain, spot, as_of):
    """(HVL, call resistance, put support, strikes-by-|net GEX|) or Nones."""
    if not chain:
        return None, None, None, []
    strikes = compute_gex_by_strike(chain, spot, as_of)
    gross = gross_by_strike(chain, spot, as_of)
    hvl = max(gross, key=gross.get) if gross else None
    pos = [s for s in strikes if s.net_gex > 0]
    neg = [s for s in strikes if s.net_gex < 0]
    call_res = max(pos, key=lambda s: s.net_gex).strike if pos else None
    put_sup = min(neg, key=lambda s: s.net_gex).strike if neg else None
    top = sorted((s for s in strikes if s.net_gex != 0), key=lambda s: -abs(s.net_gex))[:10]
    return hvl, call_res, put_sup, top


def flip_zones(chain, spot, as_of, search_pct=0.15, n=240) -> list[float]:
    lo, hi = spot * (1 - search_pct), spot * (1 + search_pct)
    xs = [lo + (hi - lo) * i / n for i in range(n + 1)]
    ys = [net_gex_at_hypothetical_spot(chain, x, as_of) for x in xs]
    zones = []
    for (x0, y0), (x1, y1) in zip(zip(xs, ys), zip(xs[1:], ys[1:])):
        if y0 == 0:
            zones.append(x0)
        elif (y0 < 0) != (y1 < 0):
            zones.append(x0 + y0 / (y0 - y1) * (x1 - x0))
    return zones


def max_pain(chain, as_of) -> float | None:
    now_ms = as_of.timestamp() * 1000
    future = sorted({q.expiration_timestamp_ms for q in chain if q.expiration_timestamp_ms - now_ms > DAY_MS / 2})
    if not future:
        return None
    exp = [q for q in chain if q.expiration_timestamp_ms == future[0]]
    strikes = sorted({q.strike for q in exp})

    def payout(s):
        return sum(q.open_interest * (max(0.0, s - q.strike) if q.option_type == "call" else max(0.0, q.strike - s))
                   for q in exp)
    return min(strikes, key=payout)


def one_day_atm_iv(chain, spot, as_of) -> float | None:
    now_ms = as_of.timestamp() * 1000
    candidates = [q for q in chain if q.expiration_timestamp_ms - now_ms > DAY_MS / 4]
    if not candidates:
        return None
    exp = min({q.expiration_timestamp_ms for q in candidates}, key=lambda ms: abs(ms - (now_ms + DAY_MS)))
    same = [q for q in candidates if q.expiration_timestamp_ms == exp]
    return min(same, key=lambda q: abs(q.strike - spot)).mark_iv_pct / 100.0


def build_text(chain: list[OptionQuote], as_of: datetime, currency: str) -> str:
    spot = chain[0].underlying_price
    now_ms = as_of.timestamp() * 1000
    zero_dte = [q for q in chain if 0 < q.expiration_timestamp_ms - now_ms <= DAY_MS]

    hvl, cres, psup, top_all = core_levels(chain, spot, as_of)
    d_hvl, d_call, d_put, top_0 = core_levels(zero_dte, spot, as_of)
    zones = flip_zones(chain, spot, as_of)
    zero_gamma = min(zones, key=lambda z: abs(z - spot)) if zones else None
    mp = max_pain(chain, as_of)
    iv = one_day_atm_iv(chain, spot, as_of)
    em = spot * iv * math.sqrt(1 / 365) if iv else None

    def line(label, val):
        return f"{label} {usd(val)}" if val else f"{label} n/a"

    out = [f"{currency} gamma levels from Deribit options, {as_of:%Y-%m-%d %H:%M} UTC, spot {usd(spot)}",
           "Assumes calls = dealer long gamma, puts = dealer short gamma (unverifiable).",
           "",
           line("HVL:", hvl), line("Call Resistance:", cres), line("Put Support:", psup),
           line("0DTE HVL:", d_hvl), line("0DTE Call:", d_call), line("0DTE Put:", d_put),
           line("Zero Gamma:", zero_gamma), line("Max Pain:", mp),
           f"Expected Move: {usd(spot - em)} to {usd(spot + em)}" if em else "Expected Move: n/a",
           "Flip Zones (All): " + (", ".join(usd(z) for z in zones) if zones else "none within 15%"),
           ""]
    for header, top in (("All-Expiry GEX Top 10", top_all), ("0DTE GEX Top 10", top_0)):
        out.append(header)
        for i, s in enumerate(top, 1):
            out.append(f"{i}. {usd(s.strike)} net {'+' if s.net_gex > 0 else '-'}{abs(s.net_gex) / 1e6:.1f}M")
        out.append(RULE)
    return "\n".join(out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--currency", default="BTC")
    args = ap.parse_args()
    chain = fetch_option_chain(args.currency)
    if not chain:
        sys.exit("Deribit returned no options")
    sys.stdout.reconfigure(encoding="utf-8")
    print(build_text(chain, datetime.now(timezone.utc), args.currency))


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
Dealer gamma-exposure (GEX) walls + HV-vs-IV -- SIGNAL LOGGING ONLY, NO TRADES.

This exact idea (options-derived market-maker exposure as ceiling/floor
levels) was evaluated on 2026-06-08 and PARKED: "genuinely new idea, but
needs a Deribit options-data pipeline we don't have, and is unvalidated in
crypto at daily horizon." (memory: market_structure_signals_verdict). This
script builds that missing pipeline and starts the forward record the parked
note said was needed before wiring anything -- it does not open positions,
does not touch entry_checklist/probability_gate, and is not judged by
proof_scorecard until there is enough forward data to judge.

WHAT IT COMPUTES (see src/gex/ for the math and its assumptions)
  1. Per-strike net dealer gamma exposure (GEX) from Deribit's live option
     chain -- ASSUMES call OI = dealer long gamma, put OI = dealer short
     gamma at that strike. This is the standard public-GEX-tracker
     convention, not a verified fact about actual dealer positioning; see
     src/gex/gex_calculator.py's module docstring before trusting any single
     number here.
  2. The zero-gamma "flip point" -- the hypothetical spot price where net
     dealer gamma crosses zero. Above it (if the standard convention holds)
     dealers are long gamma and dampen moves (fade-friendly); below it they
     are short gamma and amplify moves (trend-friendly, don't fade).
  3. Ceiling/floor walls -- the strikes with the largest positive/negative
     net GEX, a naive "price magnet" proxy.
  4. Deribit's own realized-volatility index vs. the chain's at-the-money
     implied vol, as a vol-risk-premium sanity check alongside the walls.

WHAT THIS DOES NOT DO
  Open, size, or suggest a trade. Nothing here has been measured against
  forward price action yet -- that measurement is the entire point of
  running this repeatedly and building the CSV log before drawing any
  conclusion. Per this repo's ledger rule: never edit a logged row after
  the fact, only append new ones.

Usage: python gex_paper.py            # BTC only, default
       GEX_SYMBOLS=BTC,ETH python gex_paper.py
"""
from __future__ import annotations

import csv
import json
import os
from datetime import datetime, timezone
from pathlib import Path

from src.gex.deribit_client import fetch_option_chain, latest_historical_volatility
from src.gex.gex_calculator import (
    atm_iv_near_tenor,
    compute_gex_by_strike,
    find_walls,
    find_zero_gamma_flip,
    net_gex_at_hypothetical_spot,
)
from src.state import sanitize_for_json

DATA_DIR = Path(__file__).parent / "data"
STATE_PATH = DATA_DIR / "gex_state.json"
LOG_PATH = DATA_DIR / "gex_log.csv"

SYMBOLS = [s.strip().upper() for s in os.getenv("GEX_SYMBOLS", "BTC").split(",") if s.strip()]

CSV_FIELDS = [
    "ts", "symbol", "spot", "regime", "net_gex_at_spot", "flip_point",
    "dist_to_flip_pct", "ceiling_strike", "ceiling_gex", "floor_strike",
    "floor_gex", "atm_iv_pct", "realized_vol_pct", "iv_minus_hv_pct",
]


def run_one_symbol(symbol: str) -> dict:
    chain = fetch_option_chain(symbol)
    if not chain:
        return {"symbol": symbol, "error": "empty option chain (market closed / no OI?)"}

    spot = chain[0].underlying_price
    strikes = compute_gex_by_strike(chain, spot)
    net_gex_now = net_gex_at_hypothetical_spot(chain, spot)
    flip = find_zero_gamma_flip(chain, spot)
    ceilings, floors = find_walls(strikes, top_n=3)
    atm_iv = atm_iv_near_tenor(chain, spot, target_days=30.0)
    hv = latest_historical_volatility(symbol)

    regime = "positive (dealer-long-gamma, dampening)" if net_gex_now > 0 else "negative (dealer-short-gamma, amplifying)"
    dist_to_flip_pct = ((spot - flip) / spot * 100.0) if flip else None
    iv_minus_hv = (atm_iv - hv) if (atm_iv is not None and hv is not None) else None

    result = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "symbol": symbol,
        "spot": spot,
        "regime": regime,
        "net_gex_at_spot": net_gex_now,
        "flip_point": flip,
        "dist_to_flip_pct": dist_to_flip_pct,
        "ceiling_strike": ceilings[0].strike if ceilings else None,
        "ceiling_gex": ceilings[0].net_gex if ceilings else None,
        "floor_strike": floors[0].strike if floors else None,
        "floor_gex": floors[0].net_gex if floors else None,
        "atm_iv_pct": atm_iv,
        "realized_vol_pct": hv,
        "iv_minus_hv_pct": iv_minus_hv,
        "all_ceilings": [(c.strike, c.net_gex) for c in ceilings],
        "all_floors": [(f.strike, f.net_gex) for f in floors],
    }
    return result


def _print_report(r: dict) -> None:
    if "error" in r:
        print(f"{r['symbol']}: ERROR — {r['error']}")
        return
    print(f"\n=== {r['symbol']} @ {r['ts']} ===")
    print(f"spot: {r['spot']:,.2f}")
    print(f"regime: {r['regime']}  (net GEX at spot: {r['net_gex_at_spot']:,.0f})")
    if r["flip_point"]:
        print(f"zero-gamma flip point: {r['flip_point']:,.2f}  ({r['dist_to_flip_pct']:+.2f}% from spot)")
    else:
        print("zero-gamma flip point: none found within +/-30% of spot")
    print(f"ceiling wall: {r['ceiling_strike']:,.0f}" if r["ceiling_strike"] else "ceiling wall: none")
    print(f"floor wall:   {r['floor_strike']:,.0f}" if r["floor_strike"] else "floor wall: none")
    if r["atm_iv_pct"] is not None and r["realized_vol_pct"] is not None:
        print(
            f"ATM IV {r['atm_iv_pct']:.1f}%  vs  realized vol {r['realized_vol_pct']:.1f}%  "
            f"(IV-HV spread: {r['iv_minus_hv_pct']:+.1f}pp)"
        )
    print("Signal-logging only — no trade taken, no gate evaluated.")


def _append_csv(rows: list[dict]) -> None:
    DATA_DIR.mkdir(exist_ok=True)
    is_new = not LOG_PATH.exists()
    with open(LOG_PATH, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        if is_new:
            writer.writeheader()
        for row in rows:
            writer.writerow({k: row.get(k) for k in CSV_FIELDS})


def _write_state(rows: list[dict]) -> None:
    DATA_DIR.mkdir(exist_ok=True)
    state = {r["symbol"]: r for r in rows if "error" not in r}
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(sanitize_for_json(state), f, indent=2, default=str)


def main() -> None:
    results = [run_one_symbol(sym) for sym in SYMBOLS]
    for r in results:
        _print_report(r)
    _append_csv(results)
    _write_state(results)
    print(f"\nLogged to {LOG_PATH}")


if __name__ == "__main__":
    main()

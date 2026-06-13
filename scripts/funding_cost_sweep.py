"""
Funding cost-sweep — does delta-neutral funding capture beat the fee at a LOWER
cost than Kraken's ~0.54% round-trip?

The live Kraken arms are cost-walled: funding flips before breakeven, cost >
capture. This asks the structural question on REAL recorded funding history
(data/funding_history.json — ~10 days, Bybit/Binance/Kraken, pulled from the VPS):
at what round-trip COST does a delta-neutral MAJORS funding position turn
net-positive, and is Bybit-level fee enough?

Model (best case — ONE entry+exit, ride the carry over the observed window):
  Integrate the signed funding rate over each symbol's window → gross carry %.
  net(cost) = gross_carry - round_trip_cost.   (real arms churn → only worse.)
  Interval dt is capped at one 8h cycle so unobserved gaps can't manufacture
  carry. We report MAJORS (liquid, actually capturable) separately from all.

This is a FEASIBILITY check, not proof. If even the best case doesn't clear the
bar at a realistic cost, funding arb is dead at this size; if it clears at Bybit
cost but not Kraken, that justifies a clean forward paper arm to PROVE it.
"""
from __future__ import annotations
import json
import os
from datetime import datetime
from statistics import mean, median

HIST = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'data', 'funding_history.json')

MIN_SAMPLES = int(os.getenv('SWEEP_MIN_SAMPLES', '24'))   # well-observed only
CAP_HOURS = 8.0                                           # don't carry across gaps
MAJORS = {'BTC', 'XBT', 'ETH', 'SOL', 'XRP', 'DOGE', 'ADA', 'LINK', 'LTC',
          'BCH', 'AVAX', 'DOT', 'MATIC', 'TRX', 'BNB'}

# Round-trip cost levels (fraction): Kraken funding-arb vs Bybit maker vs free.
COSTS = [('Kraken 0.54%', 0.0054), ('mid 0.30%', 0.0030),
         ('Bybit ~0.20%', 0.0020), ('Bybit-maker 0.11%', 0.0011), ('free', 0.0)]


def _base(symbol_key: str) -> str:
    s = symbol_key.split(':', 1)[1].upper()
    for pre in ('PF_', 'PI_'):
        if s.startswith(pre):
            s = s[len(pre):]
    for suf in ('USDT', 'USDC', 'USD', 'PERP'):
        if s.endswith(suf):
            s = s[:-len(suf)]
    return s.strip('_')


def _is_major(symbol_key: str) -> bool:
    return _base(symbol_key) in MAJORS


def integrate(samples):
    """Return (signed_carry_frac, flips, window_days). Caps each interval at one
    cycle so big unobserved gaps don't fabricate carry."""
    signed = 0.0
    flips = 0
    prev_sign = None
    for (t0, a0), (t1, _a1) in zip(samples, samples[1:]):
        dt_h = (datetime.fromisoformat(t1) - datetime.fromisoformat(t0)).total_seconds() / 3600
        if dt_h <= 0:
            continue
        dt_years = min(dt_h, CAP_HOURS) / (365.25 * 24)
        signed += (a0 / 100.0) * dt_years
        s = 1 if a0 > 0 else (-1 if a0 < 0 else 0)
        if prev_sign and s and s != prev_sign:
            flips += 1
        if s:
            prev_sign = s
    window_days = (datetime.fromisoformat(samples[-1][0])
                   - datetime.fromisoformat(samples[0][0])).total_seconds() / 86400
    return signed, flips, window_days


def report(label, rows):
    """rows: list of (key, signed_carry, flips, window_days)."""
    if not rows:
        print(f"\n### {label}: no symbols with >= {MIN_SAMPLES} samples")
        return
    carries = [r[1] for r in rows]
    wins_days = [r[3] for r in rows if r[3] > 0]
    print(f"\n### {label}  (n={len(rows)} symbols, ~{mean(wins_days):.1f}d window)")
    print(f"  gross carry over window: mean {mean(carries)*100:+.2f}%  median {median(carries)*100:+.2f}%")
    print(f"  median sign-flips/symbol: {int(median([r[2] for r in rows]))}  "
          f"(more flips = harder to time = real arms churn & pay cost repeatedly)")
    print(f"  {'cost level':<20}{'%net>0':>8}{'mean net%':>11}{'mean net APY':>14}")
    print("  " + "-" * 51)
    for name, c in COSTS:
        nets = [sc - c for sc in carries]
        pct_pos = sum(1 for x in nets if x > 0) / len(nets) * 100
        mean_net = mean(nets)
        # annualize: net over the window → APY
        apys = [(sc - c) / wd * 365 for sc, (_k, _sc, _f, wd) in zip(carries, rows) if wd > 0]
        mean_apy = mean(apys) * 100 if apys else 0.0
        flag = "  <== net-positive" if mean_net > 0 else ""
        print(f"  {name:<20}{pct_pos:>7.0f}%{mean_net*100:>+10.2f}%{mean_apy:>+13.1f}%{flag}")
    # breakeven cost = mean gross carry (cost below which mean net > 0)
    be = mean(carries)
    print(f"  => break-even round-trip cost (mean): {be*100:.3f}%  "
          f"({'BELOW' if be < 0.0011 else 'between Bybit and Kraken' if be < 0.0054 else 'ABOVE Kraken'})")


def main():
    d = json.load(open(HIST, encoding='utf-8'))
    S = d.get('samples', d)
    all_rows, major_rows = [], []
    for key, samples in S.items():
        if len(samples) < MIN_SAMPLES:
            continue
        signed, flips, wd = integrate(samples)
        row = (key, signed, flips, wd)
        all_rows.append(row)
        if _is_major(key):
            major_rows.append(row)

    print("=" * 60)
    print(f"FUNDING COST-SWEEP — {len(S)} symbols, {len(all_rows)} well-observed "
          f"(>= {MIN_SAMPLES} samples)")
    print("=" * 60)
    print("Best case: one entry+exit, ride the carry. Real churning arms do worse.")

    report("MAJORS (liquid / capturable)", major_rows)
    report("ALL well-observed symbols", all_rows)

    # Positive-funding majors only (the conservative arm's universe): of majors,
    # how many had net-POSITIVE average funding at all?
    pos_majors = [r for r in major_rows if r[1] > 0]
    print(f"\n-- positive-carry majors: {len(pos_majors)}/{len(major_rows)} "
          f"had positive average funding over the window --")
    if pos_majors:
        for name, c in COSTS:
            nets = [r[1] - c for r in pos_majors]
            pct = sum(1 for x in nets if x > 0) / len(nets) * 100
            print(f"     at {name:<18}: {pct:.0f}% of positive-carry majors clear cost")

    print("\n" + "=" * 60)
    print("READ: if MAJORS mean net is +ve at Bybit cost but -ve at Kraken, the")
    print("lever is VENUE/turnover, not signal — justifies a clean forward arm.")
    print("If it's -ve even free, funding arb is dead at this size. Either way,")
    print("this is feasibility; PROOF is a forward paper arm vs the scorecard.")


if __name__ == '__main__':
    main()

"""
Trade forensics — per-trade autopsy of the directional journal.

Answers, with numbers, the questions that decide whether tweaking filters can
ever flip this bot positive:

  1. COST vs EDGE: is the loss a weak/negative directional edge, a cost problem,
     or both? (Decompose net P&L into price-move gross vs fees.)
  2. THE FLIP TEST: if we inverted every trade (long<->short), would it make the
     money back? (User hypothesis, tested on the real fills, costs still paid.)
  3. THE ZERO-COST TEST: at ZERO fees, is the directional edge positive at all?
  4. MAKER/TAKER what-if: does a cheaper cost structure save it?
  5. FILTER SWEEPS: does ANY threshold carve out a net-positive subset (n>=30)?
  6. THE PER-TRADE WALL: avg edge per trade vs avg cost per trade.

Reads data/trade_journal.json — the DEFINITIVE record. (The .csv is polluted
with backtest rows dated 2024-01-01 and has unquoted-comma corruption; do not
use it.) Read-only; writes nothing.

paper_trader pnl is NET of fees (paper_trader.py:267); these are spot trades
(no funding), so price-move gross = pnl + fees_paid.
"""
from __future__ import annotations
import json
import os

JSON = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'data', 'trade_journal.json')

TAKER_RT = 0.0052   # Kraken spot round-trip taker (~0.26%/leg)
MAKER_RT = 0.0032   # round-trip maker (~0.16%/leg)


def m(x: float) -> str:
    return f"${x:+.2f}"


def main():
    with open(JSON, encoding='utf-8') as f:
        recs = json.load(f)
    n = len(recs)

    def col(name, default=0.0):
        out = []
        for r in recs:
            try:
                out.append(float(r.get(name, default)))
            except (TypeError, ValueError):
                out.append(default)
        return out

    pnl   = col('pnl')
    fees  = col('fees_paid')
    gross = [p + fz for p, fz in zip(pnl, fees)]
    notional = [r.get('position_size_usd') or 0.0 for r in recs]
    atrp  = col('atr_pct')

    net_sum   = sum(pnl)
    fee_sum   = sum(fees)
    gross_sum = sum(gross)
    wins = sum(1 for p in pnl if p > 0)

    print("=" * 64)
    print(f"TRADE FORENSICS -- {n} real directional trades (JSON)")
    print("=" * 64)
    print(f"Win rate: {wins/n*100:.1f}%   ({wins}W / {n-wins}L)")

    print("\n-- 1. COST vs EDGE decomposition --")
    print(f"  Net P&L (account felt)        : {m(net_sum)}")
    print(f"  Fees paid                     : {m(-fee_sum)}  ({fee_sum/abs(net_sum)*100:.0f}% of the loss)")
    print(f"  Price-move gross (pre-fees)   : {m(gross_sum)}   <- the actual directional edge")
    print(f"  => avg edge/trade {m(gross_sum/n)}  vs  avg cost/trade ${fee_sum/n:.3f}")

    print("\n-- 2. THE FLIP TEST (invert every trade, still pay fees) --")
    inv_net = sum(-g - fz for g, fz in zip(gross, fees))
    print(f"  Inverted net P&L              : {m(inv_net)}")
    print(f"  (= -gross - fees = {m(-gross_sum)} - ${fee_sum:.2f})")
    print(f"  => Flipping {'STILL LOSES' if inv_net < 0 else 'would profit'}. "
          f"You pay the {m(-fee_sum)} fee floor in BOTH directions.")

    print("\n-- 3. THE ZERO-COST TEST (fees = 0) --")
    print(f"  At zero cost, original edge   : {m(gross_sum)}")
    print(f"  At zero cost, inverted edge   : {m(-gross_sum)}")
    if gross_sum < 0:
        print(f"  => Even FREE, entries lost on price moves ({m(gross_sum)}). The signal")
        print(f"     has a weak NEGATIVE edge; inverted is +${-gross_sum:.2f} = "
              f"${-gross_sum/n:.3f}/trade, dwarfed by ${fee_sum/n:.3f}/trade cost.")
    else:
        print(f"  => Small positive gross edge ({m(gross_sum)}), erased by cost.")

    print("\n-- 4. WHAT-IF cost structure (same trades, different fee) --")
    avg_notional = (sum(notional) / n) if n else 0.0
    for label, rt in (("taker (current)", TAKER_RT), ("maker", MAKER_RT), ("zero", 0.0)):
        fees_cf = [(nt or avg_notional) * rt for nt in notional]
        net_cf = sum(g - fz for g, fz in zip(gross, fees_cf))
        print(f"  {label:<16}: net {m(net_cf)}")
    print("  => If gross is negative, NO cost level saves it -- the edge is the problem.")

    print("\n-- 5. FILTER SWEEPS -- can any threshold carve out a winner? --")
    print("  (keep only passing trades; need net>0 AND n>=30 to be usable)")

    def sweep(name, thresholds):
        vals = col(name)
        print(f"\n  by {name} (>=):")
        for t in thresholds:
            idx = [i for i, v in enumerate(vals) if v >= t]
            k = len(idx)
            if k == 0:
                continue
            sub_net = sum(pnl[i] for i in idx)
            sub_gross = sum(gross[i] for i in idx)
            wr = sum(1 for i in idx if pnl[i] > 0) / k * 100
            flag = "  <== net>0 & n>=30" if (sub_net > 0 and k >= 30) else ""
            print(f"    >={t:<5}: n={k:<4} WR={wr:4.1f}%  net={m(sub_net):<8} gross={m(sub_gross)}{flag}")

    sweep('confidence', [50, 55, 60, 65, 70, 75, 80])
    sweep('atr_pct',    [0.05, 0.10, 0.15, 0.20, 0.30])

    print("\n  best net-positive subsets (group net>0 at n>=30):")
    found = False
    for name in ('regime', 'entry_path', 'direction', 'hour_utc', 'symbol'):
        groups = {}
        for i, r in enumerate(recs):
            key = r.get(name, '?')
            groups.setdefault(key, []).append(i)
        for key, idx in groups.items():
            s = sum(pnl[i] for i in idx)
            if s > 0 and len(idx) >= 30:
                print(f"    {name}={key}: net {m(s)} (n={len(idx)})  <== usable winner")
                found = True
    if not found:
        print("    NONE. No regime/path/direction/hour/symbol subset is net>0 at n>=30.")

    print("\n-- 6. THE PER-TRADE WALL --")
    srt = sorted(atrp)
    med_atr = srt[len(srt)//2] if srt else 0.0
    nz = sorted(x for x in notional if x)
    med_notional = nz[len(nz)//2] if nz else 0.0
    move_dollars = med_atr / 100 * med_notional
    cost_dollars = fee_sum / n
    print(f"  Median ATR at entry  : {med_atr:.3f}% of price")
    print(f"  Median position size : ${med_notional:.2f}")
    if move_dollars > 0:
        print(f"  => a 1-ATR move ~= ${move_dollars:.3f}; round-trip cost ~= ${cost_dollars:.3f} "
              f"= {cost_dollars/move_dollars:.1f}x a full ATR move")
    cost_dom = sum(1 for g, fz in zip(gross, fees) if abs(g) < fz) / n * 100
    print(f"  {cost_dom:.0f}% of trades moved less (in $) than their own fee.")

    print("\n" + "=" * 64)
    print("VERDICT")
    print("=" * 64)
    edge_pt = abs(gross_sum) / n
    print(f"  Binding constraint = COST, not direction. Per-trade edge ~${edge_pt:.3f}")
    print(f"  is ~{cost_dollars/max(edge_pt,1e-9):.0f}x smaller than per-trade cost ~${cost_dollars:.3f}.")
    print("  An always-on long/short 'brain' trades MORE -> pays MORE toll.")
    print("  Sign-flips only from: trade RARELY (move >> cost), trade CHEAPER")
    print("  (maker/venue), or stay market-neutral and capture funding > cost.")


if __name__ == '__main__':
    main()

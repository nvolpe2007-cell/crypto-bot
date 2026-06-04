#!/usr/bin/env python3
"""
Swing trade REVIEW — reads the reasoning behind every closed trade and asks
"what was I thinking, and did it work?"

This closes the loop on the decision log. For each closed trade it reconstructs
the exact thought process at entry (which gates fired, the indicator values, the
reason) and pairs it with the outcome, then surfaces honest aggregate patterns:
win rate by exit type, average result by the conditions present at entry.

CRITICAL DISCIPLINE: this is for UNDERSTANDING, not auto-tuning. With a small
sample every "pattern" here is mostly noise — if you change the strategy to chase
what looks good in <30 trades, you are curve-fitting and you will lose real money.
Read it to understand WHY trades win/lose. Only act on a pattern that (a) has a
mechanical reason and (b) survives 30+ trades. The script says so out loud.

    python swing_review.py                      # review forward paper trades
    python swing_review.py data/swing_backtest_decisions.jsonl   # review a backtest
"""
from __future__ import annotations

import sys
from collections import defaultdict
from pathlib import Path

from src.decision_log import DecisionLog


def _bucket(v, edges, labels):
    for e, lab in zip(edges, labels):
        if v < e:
            return lab
    return labels[-1]


def main():
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("data/swing_decisions.jsonl")
    dlog = DecisionLog(path=path)
    recs = dlog.records()
    if not recs:
        print(f"no decisions logged yet at {path}")
        return

    # index the ENTER evaluations (they carry gates + indicators) by (symbol, ts)
    entries = {(r["symbol"], r["ts"]): r
               for r in recs if r["kind"] == "evaluation" and r["action"] == "ENTER"}
    closes = [r for r in recs if r["kind"] == "close"]

    print("=" * 78)
    print(f"TRADE REVIEW — {len(closes)} closed trades   ({path})")
    print("=" * 78)

    by_exit = defaultdict(list)
    by_rsi = defaultdict(list)
    by_roc = defaultdict(list)

    for c in closes:
        sym, t_in = c["symbol"], c.get("ts_in") or c.get("entry_ts")
        ent = entries.get((sym, str(t_in)))
        ind = (ent or {}).get("indicators", {})
        rsi = ind.get("rsi")
        roc = ind.get("roc")
        outcome = "WIN " if c["won"] else "LOSS"
        # per-trade narrative: the thought process + the result
        think = (f"RSI {ind.get('rsi_prev', float('nan')):.0f}->{rsi:.0f}, "
                 f"ROC {roc*100:+.1f}%, price {ind.get('close', 0):.2f} "
                 f"vs EMA50 {ind.get('ema_slow', 0):.2f}") if ent else "(entry log missing)"
        print(f"\n[{outcome}] {sym}  {c['pnl_pct']:+.2f}%  net ${c['pnl']:+.2f}  "
              f"exit={c['reason']}")
        print(f"   thesis at entry: {think}")
        by_exit[c["reason"]].append(c["pnl"])
        if rsi is not None:
            by_rsi[_bucket(rsi, [50, 55, 60], ["<50", "50-55", "55-60", "60+"])].append(c["pnl"])
        if roc is not None:
            by_roc[_bucket(roc * 100, [2, 5, 10], ["<2%", "2-5%", "5-10%", "10%+"])].append(c["pnl"])

    def _summ(title, groups):
        print(f"\n{title}")
        for k, v in sorted(groups.items()):
            wins = sum(1 for x in v if x > 0)
            print(f"   {k:<8} n={len(v):<3} win={wins/len(v)*100:3.0f}%  "
                  f"avg=${sum(v)/len(v):+.2f}  total=${sum(v):+.2f}")

    print("\n" + "-" * 78)
    print("PATTERNS (observation only — do NOT tune on a small sample):")
    _summ("by exit type:", by_exit)
    _summ("by entry RSI:", by_rsi)
    _summ("by entry trend-momentum (ROC):", by_roc)

    n = len(closes)
    print("\n" + "-" * 78)
    if n < 30:
        print(f"NOTE: {n} trades is too few to trust any pattern above (need 30+). "
              f"This is for understanding the WHY, not for changing the strategy yet.")
    else:
        print(f"{n} trades — patterns with a mechanical reason are now worth a careful look. "
              f"Still: change one thing at a time and re-prove with proof_scorecard.py.")
    print("=" * 78)


if __name__ == "__main__":
    main()

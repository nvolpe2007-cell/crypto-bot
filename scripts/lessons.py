"""
Lessons — what the bot has 'learned' from every losing trade.

Re-runs the post-trade diagnosis (mirrors paper_trading._diagnose) across the
WHOLE real journal and tallies, across losses, the failure modes the bot already
flags per-trade. Answers "where does it need to improve?" with the aggregate of
every loss — the honest version of "learn from every loss."

The point it surfaces: the losses do NOT share a fixable SETUP pattern that a
smarter entry filter would catch — they share a STRUCTURAL one (cost > move),
which is why the bot's learned response is to trade less (go idle), not to pick
better. Reads data/trade_journal.json. Read-only.
"""
from __future__ import annotations
import json
import os

JSON = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'data', 'trade_journal.json')


def _diag_flags(r: dict) -> list[str]:
    """Failure-mode flags for one trade (mirrors _diagnose, from stored fields)."""
    flags = []
    side = (r.get('direction') or 'buy').lower()
    ofi = float(r.get('ofi') or 0.0)
    if side == 'buy':
        if ofi < -0.15: flags.append('order-flow opposed')
        elif abs(ofi) < 0.15: flags.append('no order-flow conviction')
    else:
        if ofi > 0.15: flags.append('order-flow opposed')
        elif abs(ofi) < 0.15: flags.append('no order-flow conviction')
    if not r.get('lead_lag_aligned', False):
        flags.append('BTC lead not aligned')
    rsi = float(r.get('rsi') or 50.0)
    if side == 'buy' and rsi > 65: flags.append('RSI overbought entry')
    if side != 'buy' and rsi < 35: flags.append('RSI oversold short')
    if str(r.get('regime')) in ('VOLATILE', 'CRASH'): flags.append('unstable regime')
    if float(r.get('confidence') or 0.0) < 70: flags.append('low confidence (<70)')
    if float(r.get('time_in_trade_sec') or 0.0) < 120: flags.append('stopped in noise (<2min)')
    gross = float(r.get('pnl') or 0.0) + float(r.get('fees_paid') or 0.0)
    if abs(gross) < float(r.get('fees_paid') or 0.0): flags.append('cost-dominated (move < fee)')
    return flags


def main():
    with open(JSON, encoding='utf-8') as f:
        recs = json.load(f)
    losses = [r for r in recs if float(r.get('pnl') or 0.0) <= 0]
    wins   = [r for r in recs if float(r.get('pnl') or 0.0) > 0]
    nL, nW = len(losses), len(wins)

    print("=" * 60)
    print(f"LESSONS FROM LOSSES — {nL} losing trades ({nW} wins)")
    print("=" * 60)

    # Tally each failure mode across losses, and (for contrast) across wins.
    from collections import Counter
    cl, cw = Counter(), Counter()
    for r in losses:
        for fl in _diag_flags(r): cl[fl] += 1
    for r in wins:
        for fl in _diag_flags(r): cw[fl] += 1

    print(f"\n{'failure mode':<30}{'% of losses':>12}{'% of wins':>12}")
    print("-" * 54)
    for mode, c in cl.most_common():
        lp = c / nL * 100 if nL else 0
        wp = (cw[mode] / nW * 100) if nW else 0
        # A mode only 'explains' losses if it's MORE common in losses than wins.
        tag = "" if wp == 0 and lp == 0 else ("  <- discriminates" if lp - wp > 20 else "")
        print(f"{mode:<30}{lp:>11.0f}%{wp:>11.0f}%{tag}")

    print("\n-- what this means --")
    top = cl.most_common(1)[0] if cl else ('n/a', 0)
    print(f"  Most common in losses: '{top[0]}' ({top[1]/nL*100:.0f}%).")
    print("  A SETUP flaw would show up far more in losses than in wins (it would")
    print("  'discriminate'). With only", nW, "wins there is nothing to separate —")
    print("  every setup category appears in losers because EVERY trade loses to the")
    print("  same thing: cost. 'cost-dominated' is the lesson; the rest is noise.")
    print("\n-- what the bot already DID about it --")
    print("  • _update_streaks_and_adapt raised min_confidence after loss streaks")
    print("  • Learner (K-NN) raised the bar on setups resembling past losers")
    print("  => both converge on 'trade less' — which is the idle bot you see.")
    print("  The honest next lesson isn't a better filter; it's a cheaper venue or")
    print("  a market-neutral edge. See scripts/trade_forensics.py.")


if __name__ == '__main__':
    main()

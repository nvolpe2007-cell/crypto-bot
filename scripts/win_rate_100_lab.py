#!/usr/bin/env python3
"""Can an indicator have a 100% win rate and still beat fees? Build it and measure.

A 100% win rate is trivially CONSTRUCTIBLE and that is exactly the problem. This
script builds the three standard ways to do it, on real data, and reports what
each one actually costs -- so the claim is settled by measurement rather than by
assertion.

    A. NEVER REALISE A LOSS. Enter on the trend signal; exit only once price is
       above entry + costs. A losing position is simply held. Realised win rate
       is 100% BY CONSTRUCTION -- the losses move to the unrealised column and
       the equity curve, which is where nobody looks.

    B. MARTINGALE. Same, but double the position on each adverse move. The
       classic "recover everything on the bounce" construction.

    C. TINY TARGET vs WIDE STOP. Take profit at +0.5%, stop at -20%. Wins are
       frequent and small, losses are rare and enormous.

For each: the realised win rate, and then the numbers the win rate conceals --
unrealised drawdown, capital locked in underwater positions, positions never
recovered inside the sample, and terminal wealth against just holding the asset.

    python scripts/win_rate_100_lab.py
"""
from __future__ import annotations

import json
import statistics as st
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from trend_signal_rr_research import (BAND, COST_RT, COST_RT_EQ, CRYPTO, MA_LEN,
                                      load_data, sma)

BARS_PER_YEAR = 365


def _signals(bars: list[dict]) -> list[int]:
    """Indices of the trend rule's BUY bars -- the same entries as the shipped rule."""
    closes = [b['c'] for b in bars]
    ma = sma(closes, MA_LEN)
    out, long = [], False
    for i in range(len(bars)):
        if ma[i] is None:
            continue
        upper, lower = ma[i] * (1 + BAND), ma[i] * (1 - BAND)
        if long:
            if closes[i] < lower:
                long = False
        elif closes[i] > upper:
            long = True
            out.append(i)
    return out


# ── A. never realise a loss ───────────────────────────────────────────────────

def never_lose(bars: list[dict], cost: float) -> dict:
    """Enter on every BUY; exit ONLY when price clears entry + cost. Otherwise hold.

    One position at a time (capital is finite), which is itself part of the cost:
    while underwater you cannot take the next signal."""
    closes = [b['c'] for b in bars]
    sig = set(_signals(bars))
    wins, holds, missed = [], [], 0
    ent = ent_i = None
    worst_unreal = 0.0
    for i, c in enumerate(closes):
        if ent is None:
            if i in sig:
                ent, ent_i = c, i
        else:
            unreal = c / ent - 1.0 - cost
            worst_unreal = min(worst_unreal, unreal)
            if unreal > 0:
                wins.append(unreal)
                holds.append(i - ent_i)
                ent = None
            elif i in sig:
                missed += 1
    open_loss = (closes[-1] / ent - 1.0 - cost) if ent is not None else None
    n = len(wins)
    return {
        'n_closed': n,
        'win_rate': 1.0 if n else 0.0,          # by construction
        'avg_win_pct': (sum(wins) / n * 100) if n else 0.0,
        'total_realised_pct': (sum(wins) * 100) if n else 0.0,
        'median_hold_bars': st.median(holds) if holds else 0,
        'max_hold_bars': max(holds) if holds else 0,
        'worst_unrealised_pct': worst_unreal * 100,
        'signals_missed_while_stuck': missed,
        'open_at_end_pct': (open_loss * 100) if open_loss is not None else None,
    }


# ── B. martingale ─────────────────────────────────────────────────────────────

def martingale(bars: list[dict], cost: float, step: float = 0.10,
               max_adds: int = 8) -> dict:
    """Double down every `step` adverse move; exit when the AVERAGE entry clears
    cost. Reports the peak exposure required, which is the number that kills it."""
    closes = [b['c'] for b in bars]
    sig = set(_signals(bars))
    wins, peak_units_all, blowups = [], [], 0
    units = cost_basis = 0.0
    last_add = None
    for i, c in enumerate(closes):
        if units == 0:
            if i in sig:
                units, cost_basis, last_add = 1.0, c, c
        else:
            avg = cost_basis / units
            if c / avg - 1.0 - cost > 0:
                wins.append((c / avg - 1.0 - cost) * units)
                peak_units_all.append(units)
                units = cost_basis = 0.0
            elif c <= last_add * (1 - step):
                adds = int(units).bit_length() - 1
                if adds >= max_adds:
                    blowups += 1                      # required size exceeds any account
                    peak_units_all.append(units)
                    units = cost_basis = 0.0          # forced liquidation
                    continue
                units *= 2
                cost_basis += c * units / 2
                last_add = c
    return {
        'n_closed': len(wins),
        'win_rate': (len(wins) / (len(wins) + blowups)) if (wins or blowups) else 0.0,
        'peak_units_required': max(peak_units_all) if peak_units_all else 0,
        'forced_liquidations': blowups,
        'note': f'{max_adds} doublings = {2 ** max_adds}x the initial bet',
    }


# ── C. tiny target, wide stop ─────────────────────────────────────────────────

def tiny_target(bars: list[dict], cost: float, tp: float = 0.005,
                sl: float = 0.20) -> dict:
    closes = [b['c'] for b in bars]
    highs, lows = [b['h'] for b in bars], [b['l'] for b in bars]
    sig = set(_signals(bars))
    rets = []
    ent = None
    for i in range(len(bars)):
        if ent is None:
            if i in sig:
                ent = closes[i]
        else:
            if lows[i] <= ent * (1 - sl):            # stop resolves first
                rets.append(-sl - cost)
                ent = None
            elif highs[i] >= ent * (1 + tp):
                rets.append(tp - cost)
                ent = None
    n = len(rets)
    if not n:
        return {'n_closed': 0}
    wins = [r for r in rets if r > 0]
    eq = 1.0
    for r in rets:
        eq *= (1 + r)
    return {
        'n_closed': n,
        'win_rate': len(wins) / n,
        'avg_win_pct': (sum(wins) / len(wins) * 100) if wins else 0.0,
        'avg_loss_pct': (sum(r for r in rets if r <= 0) / (n - len(wins)) * 100)
                        if n > len(wins) else 0.0,
        'expectancy_pct': sum(rets) / n * 100,
        'terminal_x': eq,
    }


def main() -> int:
    data = load_data()
    print()
    print('=' * 78)
    print('THE 100% WIN RATE, BUILT THREE WAYS AND MEASURED')
    print('=' * 78)

    # ── A ────────────────────────────────────────────────────────────────────
    print('\nA. NEVER REALISE A LOSS  (hold every position until it is green)')
    print('   Realised win rate is 100% by construction. Here is the rest of it.\n')
    print(f"{'asset':<6}{'closed':>7}{'win%':>6}{'avgWin%':>9}{'medHold':>9}{'maxHold':>9}"
          f"{'worstUnreal%':>14}{'missed':>8}{'openEnd%':>10}")
    agg_worst, agg_missed, agg_maxhold = [], 0, 0
    for sym, bars in data.items():
        r = never_lose(bars, COST_RT if sym in CRYPTO else COST_RT_EQ)
        agg_worst.append(r['worst_unrealised_pct'])
        agg_missed += r['signals_missed_while_stuck']
        agg_maxhold = max(agg_maxhold, r['max_hold_bars'])
        oe = f"{r['open_at_end_pct']:+.1f}" if r['open_at_end_pct'] is not None else '-'
        print(f"{sym:<6}{r['n_closed']:>7}{r['win_rate']*100:>5.0f}%{r['avg_win_pct']:>9.2f}"
              f"{r['median_hold_bars']:>9.0f}{r['max_hold_bars']:>9.0f}"
              f"{r['worst_unrealised_pct']:>13.1f}%{r['signals_missed_while_stuck']:>8}"
              f"{oe:>10}")
    print(f"\n   Every asset: 100% realised win rate. Worst unrealised drawdown while "
          f"holding: {min(agg_worst):.1f}%")
    print(f"   Longest time locked in one underwater position: {agg_maxhold} bars "
          f"({agg_maxhold/BARS_PER_YEAR:.1f} years)")
    print(f"   BUY signals missed because capital was stuck underwater: {agg_missed}")
    print('   The losses did not disappear. They moved to a column the win rate '
          'does not report.')

    # ── B ────────────────────────────────────────────────────────────────────
    print('\n\nB. MARTINGALE  (double down on every 10% adverse move)\n')
    print(f"{'asset':<6}{'closed':>7}{'win%':>7}{'peakUnits':>11}{'forcedLiq':>11}")
    for sym, bars in data.items():
        r = martingale(bars, COST_RT if sym in CRYPTO else COST_RT_EQ)
        print(f"{sym:<6}{r['n_closed']:>7}{r['win_rate']*100:>6.0f}%"
              f"{r['peak_units_required']:>11.0f}{r['forced_liquidations']:>11}")
    print('\n   Win rate stays near 100% until the run of doublings that ends it.')
    print('   "peakUnits" is the multiple of the FIRST bet the account had to fund.')
    print('   Any forced liquidation is account death, not a losing trade.')

    # ── C ────────────────────────────────────────────────────────────────────
    print('\n\nC. TINY TARGET (+0.5%) vs WIDE STOP (-20%)\n')
    print(f"{'asset':<6}{'closed':>7}{'win%':>7}{'avgWin%':>9}{'avgLoss%':>10}"
          f"{'exp%':>8}{'terminal':>11}")
    for sym, bars in data.items():
        r = tiny_target(bars, COST_RT if sym in CRYPTO else COST_RT_EQ)
        if not r.get('n_closed'):
            continue
        print(f"{sym:<6}{r['n_closed']:>7}{r['win_rate']*100:>6.0f}%{r['avg_win_pct']:>9.2f}"
              f"{r['avg_loss_pct']:>10.2f}{r['expectancy_pct']:>8.3f}{r['terminal_x']:>10.2f}x")
    print('\n   This is the highest win rate of anything in this repo, and it loses '
          'money.')

    print('\n' + '=' * 78)
    print('CONCLUSION')
    print('=' * 78)
    print("""  A 100% win rate is not hard. It is purchasable in three lines of code,
  and all three ways to buy it are paid for out of the same account:

    A moves losses from realised to unrealised and locks up the capital.
    B converts a bounded loss into an unbounded one.
    C makes losses rare and enormous instead of frequent and small.

  Win rate is a FREE PARAMETER. It can be set to any value up to 100% without
  ever touching whether the strategy makes money. What cannot be manufactured
  is EXPECTANCY -- average profit per trade after costs -- and that is the only
  number worth optimising. This repo has now reproduced that four times
  (tpMult, the 12/12 vol overlay, the 3R/5R take-profit, and this).""")
    print('=' * 78)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

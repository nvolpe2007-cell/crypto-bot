#!/usr/bin/env python3
"""How much of the trend rule survives when you are not allowed to peek?

Everything measured so far on this rule -- the crypto backtest, the 8-asset OOS
check, the 70-ticker grid -- picked parameters with the whole history visible.
That is how a result looks trustworthy and then is not. This script runs the one
test that answers "would it have worked in real time":

  WALK-FORWARD. Split each asset's history into consecutive windows. For each
  test window, choose (ma_len, band) using ONLY the windows before it, then trade
  the test window with that choice and never revisit it. Concatenate the test
  windows -- that sequence is the only honest track record.

  Three arms are compared on exactly the same test windows:
    ADAPTIVE  re-picks the best parameters on each training window (what a
              diligent person actually does, and the one most likely to overfit)
    FIXED     always the shipped parameters, never tuned (SMA100/2% crypto,
              SMA200/1% equities)
    HOLD      buy and hold, the control that pays no fees and needs no signal

  If ADAPTIVE loses to FIXED, tuning is noise and the honest indicator is the
  untuned one. If both lose to HOLD, the rule is not a return edge -- which is
  what this repo already believes, and this measures whether that holds
  out-of-sample too.

No lookahead: the moving average at bar i uses only bars <= i, parameter choice
for a test window uses only bars strictly before it, and trades are entered on
confirmed closes.

    python scripts/walkforward_trust.py
"""
from __future__ import annotations

import json
import math
import statistics as st
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

CRYPTO_CACHE = ROOT / 'data' / 'research_cache_crypto_wide.json'
EQUITY_CACHE = ROOT / 'data' / 'research_cache_equity_lab.json'

# Widened 2026-09-08: n=3 was too few to trust a 2-of-3 result.
CRYPTO = None  # None -> every symbol in the cache
COST_CRYPTO = 0.0054
COST_EQUITY = 0.0010

# The grid ADAPTIVE is allowed to choose from on each training window.
GRID = [(m, b) for m in (50, 100, 150, 200) for b in (0.0, 0.01, 0.02, 0.04)]
FIXED_CRYPTO = (100, 0.02)
FIXED_EQUITY = (200, 0.01)

N_FOLDS = 5           # test windows per asset
MIN_TRAIN_BARS = 400  # a training window shorter than this cannot pick anything


def sma(vals, n):
    out, run = [], 0.0
    for i, v in enumerate(vals):
        run += v
        if i >= n:
            run -= vals[i - n]
        out.append(run / n if i >= n - 1 else None)
    return out


def segment_trades(closes, ma_len, band, cost, lo, hi):
    """Trade [lo, hi). The MA may look back before lo -- that is past data, not
    lookahead. Returns per-trade net returns; an open position at hi is marked
    to market so a winning open trade cannot be quietly dropped."""
    ma = sma(closes, ma_len)
    out, long, ent = [], False, None
    for i in range(lo, hi):
        if ma[i] is None:
            continue
        c, up, dn = closes[i], ma[i] * (1 + band), ma[i] * (1 - band)
        if long:
            if c < dn:
                out.append(c / ent - 1.0 - cost)
                long = False
        elif c > up:
            long, ent = True, c
    if long and ent:
        out.append(closes[hi - 1] / ent - 1.0 - cost)
    return out


def growth(rets):
    eq = 1.0
    for r in rets:
        eq *= (1 + r)
    return eq


def pick_params(closes, cost, lo, hi):
    """Best (ma_len, band) on [lo, hi) by terminal growth. Training only."""
    best, best_g = None, -1e9
    for m, b in GRID:
        g = growth(segment_trades(closes, m, b, cost, lo, hi))
        if g > best_g:
            best, best_g = (m, b), g
    return best


def walk_forward(closes, cost, fixed):
    """Returns dict of concatenated OOS results for adaptive / fixed / hold."""
    n = len(closes)
    fold = n // (N_FOLDS + 1)
    if fold < 60:
        return None
    ad, fx, picks = [], [], []
    hold_start, hold_end = None, None
    for k in range(1, N_FOLDS + 1):
        tr_lo, tr_hi = 0, fold * k
        te_lo, te_hi = fold * k, min(fold * (k + 1), n)
        if tr_hi - tr_lo < MIN_TRAIN_BARS or te_hi - te_lo < 30:
            continue
        p = pick_params(closes, cost, tr_lo, tr_hi)
        picks.append(p)
        ad += segment_trades(closes, p[0], p[1], cost, te_lo, te_hi)
        fx += segment_trades(closes, fixed[0], fixed[1], cost, te_lo, te_hi)
        if hold_start is None:
            hold_start = closes[te_lo]
        hold_end = closes[te_hi - 1]
    if not picks:
        return None
    return {
        'adaptive': ad, 'fixed': fx,
        'hold_x': (hold_end / hold_start) if hold_start else 1.0,
        'picks': picks,
    }


def summarize(rets):
    if not rets:
        return {'n': 0, 'x': 1.0, 'exp': 0.0, 'win': 0.0}
    return {'n': len(rets), 'x': growth(rets),
            'exp': sum(rets) / len(rets),
            'win': sum(1 for r in rets if r > 0) / len(rets)}


def run(label, data, symbols, cost, fixed):
    print('=' * 84)
    print(f'{label}  —  {N_FOLDS}-fold walk-forward, parameters chosen on past data only')
    print('=' * 84)
    print(f"{'asset':<8}{'ADAPTIVE':>22}{'FIXED':>22}{'HOLD':>10}   picks (ma/band)")
    print(f"{'':<8}{'x':>8}{'exp%':>7}{'win%':>7}{'x':>8}{'exp%':>7}{'win%':>7}{'x':>10}")
    rows = []
    for sym in symbols:
        bars = data.get(sym)
        if not bars:
            continue
        closes = [b['c'] for b in bars]
        r = walk_forward(closes, cost, fixed)
        if not r:
            continue
        a, f = summarize(r['adaptive']), summarize(r['fixed'])
        rows.append((sym, a, f, r['hold_x'], r['picks']))
        pk = ' '.join(f"{m}/{int(b*100)}" for m, b in r['picks'][:5])
        print(f"{sym:<8}{a['x']:>8.2f}{a['exp']*100:>7.2f}{a['win']*100:>7.0f}"
              f"{f['x']:>8.2f}{f['exp']*100:>7.2f}{f['win']*100:>7.0f}"
              f"{r['hold_x']:>10.2f}   {pk}")
    if not rows:
        print('  (insufficient data)')
        return None

    ax = [r[1]['x'] for r in rows]
    fx = [r[2]['x'] for r in rows]
    hx = [r[3] for r in rows]
    print('-' * 84)
    print(f"{'MEDIAN':<8}{st.median(ax):>8.2f}{'':>14}{st.median(fx):>8.2f}"
          f"{'':>14}{st.median(hx):>10.2f}")
    print(f"\n  ADAPTIVE beat FIXED on {sum(1 for a, f in zip(ax, fx) if a > f)}/{len(rows)} assets")
    print(f"  FIXED    beat HOLD  on {sum(1 for f, h in zip(fx, hx) if f > h)}/{len(rows)} assets")
    print(f"  ADAPTIVE beat HOLD  on {sum(1 for a, h in zip(ax, hx) if a > h)}/{len(rows)} assets")

    # how unstable is the "best" parameter across folds?
    flips = 0
    for _s, _a, _f, _h, picks in rows:
        flips += sum(1 for i in range(1, len(picks)) if picks[i] != picks[i - 1])
    total = sum(len(r[4]) - 1 for r in rows)
    if total:
        print(f"  best-parameter CHANGED between consecutive folds "
              f"{flips}/{total} times ({flips/total*100:.0f}%)")
    return {'adaptive': ax, 'fixed': fx, 'hold': hx, 'rows': rows}


def portfolio_walk_forward(data, symbols, cost, fixed, label):
    """Equal-weight basket, rebalanced daily, walk-forward. Diversification was
    the ONE change ever measured to improve this rule (Sharpe 0.56 -> 1.01), so
    it is the only lever tested here. Each asset is independently long/flat on
    the SAME fixed parameters; the basket return each day is the mean of the
    per-asset daily returns (in-cash assets contribute 0). Cost is charged on the
    days an asset actually switches state, so churn is paid for."""
    series = {}
    for s in symbols:
        bars = data.get(s)
        if bars and len(bars) > 700:
            series[s] = [b['c'] for b in bars]
    if len(series) < 3:
        return None
    n = min(len(v) for v in series.values())
    series = {s: v[-n:] for s, v in series.items()}       # align on the tail
    fold = n // (N_FOLDS + 1)

    ma_len, band = fixed
    state, rets_rule, rets_hold = {}, [], []
    mas = {s: sma(v, ma_len) for s, v in series.items()}
    lo = fold  # first test bar; everything before is training/warmup
    for i in range(lo, n):
        day_rule, day_hold = [], []
        for s, closes in series.items():
            m = mas[s][i]
            prev = state.get(s, False)
            r = closes[i] / closes[i - 1] - 1.0
            day_hold.append(r)
            day_rule.append(r if prev else 0.0)
            if m is not None:
                if prev and closes[i] < m * (1 - band):
                    state[s] = False
                elif not prev and closes[i] > m * (1 + band):
                    state[s] = True
            switched = state.get(s, False) != prev
            if switched:
                day_rule[-1] -= cost / 2.0   # one leg of a round trip
        rets_rule.append(sum(day_rule) / len(day_rule))
        rets_hold.append(sum(day_hold) / len(day_hold))

    def stats(rs):
        eq, peak, dd = 1.0, 1.0, 0.0
        for r in rs:
            eq *= (1 + r); peak = max(peak, eq); dd = min(dd, eq / peak - 1.0)
        mu = sum(rs) / len(rs)
        sd = st.pstdev(rs) or 1e-12
        return eq, dd, mu / sd * math.sqrt(365)

    er, ddr, shr = stats(rets_rule)
    eh, ddh, shh = stats(rets_hold)
    print()
    print('=' * 84)
    print(f'{label} — EQUAL-WEIGHT BASKET of {len(series)}, walk-forward test period only')
    print('=' * 84)
    print(f"{'':<22}{'growth':>10}{'maxDD':>10}{'Sharpe':>9}")
    print(f"{'rule on the basket':<22}{er:>9.2f}x{ddr*100:>9.1f}%{shr:>9.2f}")
    print(f"{'hold the basket':<22}{eh:>9.2f}x{ddh*100:>9.1f}%{shh:>9.2f}")
    print()
    print(f"  drawdown reduction: {(ddh-ddr)*100:+.1f}pp   "
          f"return ratio vs hold: {er/eh:.2f}x   Sharpe {shr:.2f} vs {shh:.2f}")
    return {'rule': (er, ddr, shr), 'hold': (eh, ddh, shh)}


def main() -> int:
    crypto = json.loads(CRYPTO_CACHE.read_text()) if CRYPTO_CACHE.exists() else {}
    equity = json.loads(EQUITY_CACHE.read_text()) if EQUITY_CACHE.exists() else {}
    if not crypto and not equity:
        print('no cached data; run scripts/trend_signal_rr_research.py first', file=sys.stderr)
        return 1

    c = run(f'CRYPTO ({len(crypto)} Coinbase assets, 0.54% round-trip)', crypto,
            CRYPTO or sorted(crypto), COST_CRYPTO, FIXED_CRYPTO) if crypto else None
    print()
    e = run('EQUITIES (70 tickers, 0.10% round-trip)', equity,
            sorted(equity), COST_EQUITY, FIXED_EQUITY) if equity else None

    if crypto:
        portfolio_walk_forward(crypto, sorted(crypto), COST_CRYPTO, FIXED_CRYPTO, 'CRYPTO')
    if equity:
        portfolio_walk_forward(equity, sorted(equity), COST_EQUITY, FIXED_EQUITY, 'EQUITIES')

    print('\n' + '=' * 84)
    print('WHAT THIS MEANS')
    print('=' * 84)
    for label, res in (('crypto', c), ('equities', e)):
        if not res:
            continue
        a, f, h = res['adaptive'], res['fixed'], res['hold']
        n = len(a)
        af = sum(1 for x, y in zip(a, f) if x > y)
        fh = sum(1 for x, y in zip(f, h) if x > y)
        print(f'\n  {label}:')
        print(f'    tuning helped on {af}/{n} assets '
              f'({"tuning is noise -- ship the untuned rule" if af <= n/2 else "tuning added value"})')
        print(f'    the rule beat holding on {fh}/{n} assets '
              f'({"not a return edge" if fh <= n/2 else "a return edge"})')
    print('=' * 84)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

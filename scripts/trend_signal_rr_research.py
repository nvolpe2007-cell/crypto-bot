#!/usr/bin/env python3
"""What risk:reward does the Trend Signal (Honest) rule actually deliver, and
does capping it at a fixed 3R-5R take-profit help?

WHY THIS QUESTION NEEDS A DEFINITION FIRST
    The rule has no stop and no target -- it is long/flat on close vs SMA(100)
    with a 2% band -- so "R" is not defined by construction. The honest way to
    impute one is the distance from the entry close down to the exit band as it
    stood AT ENTRY: that is where the SELL would have fired had price turned
    around immediately, i.e. the risk actually being taken on entry.

        R = entry_close - SMA100_at_entry * (1 - band)

    Every trade's outcome is then expressed in those units, net of cost. This
    is an imputed R, not one the rule enforces: the band MOVES with the SMA, so
    a real loss can land above -1R (the band rose to meet price) or below it
    (a gap through the band). The realised loss distribution is reported so the
    gap between the two is visible rather than assumed.

WHAT IT TESTS
    1. The payoff ratio the rule already delivers (avg win / avg loss in R).
    2. Fixed take-profits at 1R..8R -- exit intrabar the moment price touches
       entry + k*R, otherwise the normal SELL. This is the direct test of
       "I want 1:3 to 1:5", and it is a change to the rule, so it is measured
       against the unchanged rule as control.
    3. A hard -1R stop, alone and combined with the take-profit, since a fixed
       target usually arrives packaged with a fixed stop.

Costs: 0.54% round-trip, charged on every entry/exit including target exits.
Intrabar fills are OPTIMISTIC for the take-profit variants (assumes the target
fills at exactly k*R when the high touches it, with no gap-through and no
slippage). That bias runs in FAVOUR of the thing being tested, so a negative
result is the strong direction.

    python scripts/trend_signal_rr_research.py
"""
from __future__ import annotations

import json
import statistics as st
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

CACHE = ROOT / 'data' / 'research_cache_trend_rr.json'

MA_LEN = 100
BAND = 0.02
COST_RT = 0.0054          # round-trip, matches the shipped backtest
COST_RT_EQ = 0.0010       # equities

CRYPTO = {'BTC': 'BTC/USD', 'ETH': 'ETH/USD', 'SOL': 'SOL/USD'}
EQUITY = ['SPY', 'QQQ', 'GLD']
START_ISO = '2019-01-01T00:00:00Z'


# ── data ──────────────────────────────────────────────────────────────────────

def _fetch_crypto() -> dict[str, list[dict]]:
    import ccxt
    ex = ccxt.coinbase({'enableRateLimit': True})
    out = {}
    for base, pair in CRYPTO.items():
        print(f'[data] {pair} daily ...')
        bars, cursor, stall = [], ex.parse8601(START_ISO), 0
        now_ms = int(time.time() * 1000)
        while cursor < now_ms:
            chunk = ex.fetch_ohlcv(pair, timeframe='1d', since=cursor, limit=300)
            if not chunk:
                stall += 1
                if stall >= 3:
                    break
                cursor += 300 * 86_400_000
                continue
            stall = 0
            for r in chunk:
                bars.append({'t': int(r[0] // 1000), 'h': r[2], 'l': r[3], 'c': r[4]})
            nxt = chunk[-1][0] + 86_400_000
            if nxt <= cursor:
                break
            cursor = nxt
            time.sleep(ex.rateLimit / 1000.0)
        seen = {b['t']: b for b in bars}
        rows = sorted(seen.values(), key=lambda b: b['t'])
        cutoff = int(time.time() // 86400 * 86400)
        out[base] = [b for b in rows if b['t'] < cutoff]
        print(f'       {base}: {len(out[base])} bars')
    return out


def _fetch_equity() -> dict[str, list[dict]]:
    import yfinance as yf
    out = {}
    for sym in EQUITY:
        print(f'[data] {sym} daily ...')
        df = yf.download(sym, start=START_ISO[:10], progress=False, auto_adjust=True)
        if df is None or df.empty:
            print(f'       {sym}: NO DATA, skipped')
            continue
        rows = []
        for ts, r in df.iterrows():
            h, l, c = float(r['High'].iloc[0] if hasattr(r['High'], 'iloc') else r['High']), \
                      float(r['Low'].iloc[0] if hasattr(r['Low'], 'iloc') else r['Low']), \
                      float(r['Close'].iloc[0] if hasattr(r['Close'], 'iloc') else r['Close'])
            rows.append({'t': int(ts.timestamp()), 'h': h, 'l': l, 'c': c})
        out[sym] = rows
        print(f'       {sym}: {len(rows)} bars')
    return out


def load_data() -> dict[str, list[dict]]:
    if CACHE.exists():
        print(f'[data] using cache {CACHE}')
        return json.loads(CACHE.read_text())
    d = _fetch_crypto()
    d.update(_fetch_equity())
    CACHE.write_text(json.dumps(d))
    return d


# ── the rule ──────────────────────────────────────────────────────────────────

def sma(vals: list[float], n: int) -> list[float | None]:
    out, run = [], 0.0
    for i, v in enumerate(vals):
        run += v
        if i >= n:
            run -= vals[i - n]
        out.append(run / n if i >= n - 1 else None)
    return out


def trades(bars: list[dict], cost: float, tp_r: float | None = None,
           stop_r: float | None = None) -> list[dict]:
    """Replay the rule. Returns one record per closed trade.

    tp_r / stop_r are in units of the ENTRY-TIME R (entry close minus the lower
    band at entry). Both fill intrabar on touch -- optimistic on purpose."""
    closes = [b['c'] for b in bars]
    ma = sma(closes, MA_LEN)
    out: list[dict] = []
    long = False
    ent = ent_i = risk = None

    for i in range(len(bars)):
        m = ma[i]
        if m is None:
            continue
        c, hi, lo = bars[i]['c'], bars[i]['h'], bars[i]['l']
        upper, lower = m * (1 + BAND), m * (1 - BAND)

        if long:
            # intrabar target / stop first (same bar ordering caveat: if both are
            # touched we resolve the STOP first, the conservative choice)
            if stop_r is not None and lo <= ent - stop_r * risk:
                out.append(_rec(bars, ent_i, i, ent, ent - stop_r * risk, risk, cost, 'stop'))
                long = False
                continue
            if tp_r is not None and hi >= ent + tp_r * risk:
                out.append(_rec(bars, ent_i, i, ent, ent + tp_r * risk, risk, cost, 'target'))
                long = False
                continue
            if c < lower:                                  # the rule's own SELL
                out.append(_rec(bars, ent_i, i, ent, c, risk, cost, 'signal'))
                long = False
        else:
            if c > upper:
                long, ent, ent_i = True, c, i
                risk = c - lower                           # R, imputed at entry
    return out


def _rec(bars, i0, i1, ent, exit_px, risk, cost, why) -> dict:
    net_ret = (exit_px / ent - 1.0) - cost
    return {'entry_t': bars[i0]['t'], 'exit_t': bars[i1]['t'], 'bars': i1 - i0,
            'entry': ent, 'exit': exit_px, 'risk_pct': risk / ent,
            'net_ret': net_ret, 'r': net_ret * ent / risk, 'why': why}


# ── reporting ─────────────────────────────────────────────────────────────────

def summarise(ts: list[dict]) -> dict:
    if not ts:
        return {'n': 0}
    rs = [t['r'] for t in ts]
    rets = [t['net_ret'] for t in ts]
    wins = [x for x in rs if x > 0]
    losses = [x for x in rs if x <= 0]
    gw, gl = sum(wins), -sum(losses)
    eq = 1.0
    for x in rets:
        eq *= (1 + x)
    return {
        'n': len(ts),
        'win_rate': len(wins) / len(ts),
        'avg_r': sum(rs) / len(rs),
        'median_r': st.median(rs),
        'avg_win_r': (sum(wins) / len(wins)) if wins else 0.0,
        'avg_loss_r': (sum(losses) / len(losses)) if losses else 0.0,
        'payoff': (sum(wins) / len(wins)) / abs(sum(losses) / len(losses))
                  if wins and losses else float('inf'),
        'pf': (gw / gl) if gl > 0 else float('inf'),
        'total_x': eq,
        'best_r': max(rs),
        'worst_r': min(rs),
    }


def main() -> int:
    data = load_data()
    print()

    # ── 1. the R distribution the rule already delivers ──────────────────────
    print('=' * 78)
    print('1. WHAT R:R THE RULE ALREADY DELIVERS  (no target, no stop)')
    print('   R = entry close - lower band at entry (the risk taken on entry)')
    print('=' * 78)
    print(f"{'asset':<6}{'n':>4}{'win%':>7}{'avgR':>8}{'medR':>8}"
          f"{'avgWinR':>9}{'avgLossR':>10}{'payoff':>8}{'PF':>7}{'bestR':>8}{'worstR':>8}")
    pooled: list[dict] = []
    for sym, bars in data.items():
        cost = COST_RT if sym in CRYPTO else COST_RT_EQ
        ts = trades(bars, cost)
        pooled += ts
        s = summarise(ts)
        if not s['n']:
            continue
        print(f"{sym:<6}{s['n']:>4}{s['win_rate']*100:>6.0f}%{s['avg_r']:>8.2f}"
              f"{s['median_r']:>8.2f}{s['avg_win_r']:>9.2f}{s['avg_loss_r']:>10.2f}"
              f"{s['payoff']:>8.2f}{s['pf']:>7.2f}{s['best_r']:>8.1f}{s['worst_r']:>8.2f}")
    p = summarise(pooled)
    print('-' * 78)
    print(f"{'POOL':<6}{p['n']:>4}{p['win_rate']*100:>6.0f}%{p['avg_r']:>8.2f}"
          f"{p['median_r']:>8.2f}{p['avg_win_r']:>9.2f}{p['avg_loss_r']:>10.2f}"
          f"{p['payoff']:>8.2f}{p['pf']:>7.2f}{p['best_r']:>8.1f}{p['worst_r']:>8.2f}")

    # how the winners are distributed -- the thing a fixed target would cut
    rs = sorted((t['r'] for t in pooled), reverse=True)
    big = [x for x in rs if x >= 5]
    print(f"\n   trades reaching >=3R: {sum(1 for x in rs if x >= 3)}/{len(rs)}"
          f"   >=5R: {len(big)}/{len(rs)}   >=10R: {sum(1 for x in rs if x >= 10)}/{len(rs)}")
    gross = sum(x for x in rs if x > 0)
    print(f"   share of ALL gross R produced by trades that ran past 5R: "
          f"{sum(big)/gross*100 if gross else 0:.0f}%")

    # ── 2. does a fixed take-profit help? ────────────────────────────────────
    print('\n' + '=' * 78)
    print('2. FIXED TAKE-PROFIT AT kR  (the "I want 1:3 to 1:5" test)')
    print('   exits intrabar on touch -- optimistic, biased FOR the target')
    print('=' * 78)
    print(f"{'variant':<22}{'n':>5}{'win%':>7}{'avgR':>8}{'payoff':>8}{'PF':>8}{'growth':>12}")
    base = summarise(pooled)
    print(f"{'rule (control)':<22}{base['n']:>5}{base['win_rate']*100:>6.0f}%"
          f"{base['avg_r']:>8.2f}{base['payoff']:>8.2f}{base['pf']:>8.2f}"
          f"{base['total_x']:>11.1f}x")
    for k in (1, 2, 3, 4, 5, 8):
        agg: list[dict] = []
        for sym, bars in data.items():
            agg += trades(bars, COST_RT if sym in CRYPTO else COST_RT_EQ, tp_r=float(k))
        s = summarise(agg)
        print(f"{f'+ take-profit {k}R':<22}{s['n']:>5}{s['win_rate']*100:>6.0f}%"
              f"{s['avg_r']:>8.2f}{s['payoff']:>8.2f}{s['pf']:>8.2f}{s['total_x']:>11.1f}x")

    # ── 3. adding the stop that usually comes with a target ──────────────────
    print('\n' + '=' * 78)
    print('3. ADDING A HARD -1R STOP')
    print('=' * 78)
    print(f"{'variant':<22}{'n':>5}{'win%':>7}{'avgR':>8}{'payoff':>8}{'PF':>8}{'growth':>12}")
    for label, tp, sl in (('+ stop 1R only', None, 1.0),
                          ('+ TP 3R & stop 1R', 3.0, 1.0),
                          ('+ TP 5R & stop 1R', 5.0, 1.0)):
        agg = []
        for sym, bars in data.items():
            agg += trades(bars, COST_RT if sym in CRYPTO else COST_RT_EQ,
                          tp_r=tp, stop_r=sl)
        s = summarise(agg)
        print(f"{label:<22}{s['n']:>5}{s['win_rate']*100:>6.0f}%{s['avg_r']:>8.2f}"
              f"{s['payoff']:>8.2f}{s['pf']:>8.2f}{s['total_x']:>11.1f}x")

    # ── 4. how far the realised loss strays from the imputed -1R ─────────────
    losers = [t for t in pooled if t['r'] <= 0]
    if losers:
        lr = sorted(t['r'] for t in losers)
        print('\n' + '=' * 78)
        print('4. IS THE IMPUTED R HONEST? realised losses, in R')
        print('=' * 78)
        print(f'   {len(losers)} losing trades   median {st.median(lr):+.2f}R   '
              f'worst {min(lr):+.2f}R')
        print(f'   worse than -1R: {sum(1 for x in lr if x < -1)}/{len(lr)}  '
              f'(band moved away / gapped through -- the risk was not capped at 1R)')
        print(f'   better than -1R: {sum(1 for x in lr if x >= -1)}/{len(lr)}  '
              f'(band rose to meet price before the exit fired)')
    print('=' * 78)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

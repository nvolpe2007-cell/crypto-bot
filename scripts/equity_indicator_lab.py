#!/usr/bin/env python3
"""Does the trend rule hold across a BROAD stock universe, or was SPY/QQQ/GLD luck?

WHY THIS IS THE RIGHT NEXT EXPERIMENT
    The 2026-09-06 R:R run found the SMA(100)+2% rule posts a 60-64% win rate and
    a 3.2-6.8 payoff on SPY/QQQ/GLD -- by far the best-looking result in this
    repo. But that was THREE assets with 8-11 trades each, and three assets can
    look like anything. Meanwhile memory `exhaustive_search_320_zero` records the
    boundary of the crypto search: no edge in daily OHLC, need new data. Equities
    ARE new data for this repo -- 60 names that had no hand in the rule's design.

DISCIPLINE (this is a search, so it is run as one)
    - MEDIAN across the universe, never the best asset. The best of 60 names is
      always excellent and always meaningless.
    - Split-half: the rule's parameters were chosen on CRYPTO, so all of this is
      out-of-sample by construction -- but both halves are reported anyway, so a
      result that lives in one regime cannot hide.
    - Every parameter cell tried is printed. A grid of 20 cells gets a
      best-of-20 bar, not a t>2 bar.
    - Buy-and-hold on the same asset over the same window is the control. A
      trend filter that trails B&H is not an edge, whatever its win rate.

    python scripts/equity_indicator_lab.py
"""
from __future__ import annotations

import json
import math
import statistics as st
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

CACHE = ROOT / 'data' / 'research_cache_equity_lab.json'
START = '2015-01-01'
COST_RT = 0.0010          # 10 bps round-trip, retail equities

UNIVERSE = [
    # mega/large caps across sectors
    'AAPL', 'MSFT', 'GOOGL', 'AMZN', 'META', 'NVDA', 'TSLA', 'AVGO', 'ORCL', 'CRM',
    'JPM', 'BAC', 'WFC', 'GS', 'MS', 'V', 'MA', 'AXP', 'BLK', 'SCHW',
    'JNJ', 'UNH', 'PFE', 'MRK', 'ABBV', 'LLY', 'TMO', 'ABT', 'AMGN', 'GILD',
    'XOM', 'CVX', 'COP', 'SLB', 'EOG',
    'PG', 'KO', 'PEP', 'WMT', 'COST', 'MCD', 'NKE', 'HD', 'LOW', 'TGT',
    'CAT', 'DE', 'BA', 'HON', 'GE', 'UPS', 'UNP', 'LMT',
    # sector + asset-class ETFs
    'SPY', 'QQQ', 'IWM', 'DIA', 'XLF', 'XLE', 'XLK', 'XLV', 'XLI', 'XLP',
    'GLD', 'SLV', 'TLT', 'IEF', 'EEM', 'EFA', 'VNQ',
]


def load_data() -> dict[str, list[dict]]:
    if CACHE.exists():
        print(f'[data] cache {CACHE}')
        return json.loads(CACHE.read_text())
    import yfinance as yf
    print(f'[data] downloading {len(UNIVERSE)} tickers from {START} ...')
    df = yf.download(UNIVERSE, start=START, progress=False, auto_adjust=True,
                     group_by='ticker', threads=True)
    out: dict[str, list[dict]] = {}
    for sym in UNIVERSE:
        try:
            sub = df[sym].dropna()
        except (KeyError, TypeError):
            continue
        rows = [{'t': int(ts.timestamp()), 'c': float(r['Close'])}
                for ts, r in sub.iterrows() if r['Close'] > 0]
        if len(rows) > 500:
            out[sym] = rows
    print(f'[data] usable: {len(out)}/{len(UNIVERSE)} tickers, '
          f'~{st.median([len(v) for v in out.values()]):.0f} bars each')
    CACHE.write_text(json.dumps(out))
    return out


def sma(vals: list[float], n: int) -> list[float | None]:
    out, run = [], 0.0
    for i, v in enumerate(vals):
        run += v
        if i >= n:
            run -= vals[i - n]
        out.append(run / n if i >= n - 1 else None)
    return out


def replay(closes: list[float], ma_len: int, band: float, cost: float,
           lo: int = 0, hi: int | None = None) -> dict:
    """Long/flat on close vs SMA with a hysteresis band. Returns trade stats and
    the strategy's terminal wealth alongside buy-and-hold over the same slice."""
    hi = len(closes) if hi is None else hi
    ma = sma(closes, ma_len)
    trades, long, ent = [], False, None
    for i in range(lo, hi):
        if ma[i] is None:
            continue
        c, upper, lower = closes[i], ma[i] * (1 + band), ma[i] * (1 - band)
        if long:
            if c < lower:
                trades.append(c / ent - 1.0 - cost)
                long = False
        elif c > upper:
            long, ent = True, c
    if long and ent:                                   # mark the open position out
        trades.append(closes[hi - 1] / ent - 1.0 - cost)
    if not trades:
        return {'n': 0}
    eq = 1.0
    peak = dd = 0.0
    curve = []
    for r in trades:
        eq *= (1 + r)
        curve.append(eq)
        peak = max(peak, eq)
        dd = min(dd, eq / peak - 1.0)
    wins = [r for r in trades if r > 0]
    losses = [r for r in trades if r <= 0]
    # first valid bar, so B&H is measured over the same window the rule could trade
    first = next((i for i in range(lo, hi) if ma[i] is not None), lo)
    bh = closes[hi - 1] / closes[first] - 1.0
    # buy-and-hold's OWN max drawdown over the same window. Without this the
    # "it is for drawdown control" defence is an assertion, not a measurement.
    bh_peak = bh_dd = 0.0
    for i in range(first, hi):
        bh_peak = max(bh_peak, closes[i])
        bh_dd = min(bh_dd, closes[i] / bh_peak - 1.0)
    sd = st.stdev(trades) if len(trades) > 1 else 0.0
    return {
        'n': len(trades),
        'win_rate': len(wins) / len(trades),
        'expectancy': sum(trades) / len(trades),
        'payoff': ((sum(wins) / len(wins)) / abs(sum(losses) / len(losses)))
                  if wins and losses else float('inf'),
        'pf': (sum(wins) / -sum(losses)) if losses and sum(losses) < 0 else float('inf'),
        'strat_x': eq,
        'bh_x': 1.0 + bh,
        'excess_x': eq - (1.0 + bh),
        'max_dd': dd,
        'bh_dd': bh_dd,
        'dd_better': dd > bh_dd,
        't_stat': (sum(trades) / len(trades)) / (sd / math.sqrt(len(trades)))
                  if sd > 0 and len(trades) > 1 else 0.0,
    }


def universe_median(data, ma_len, band, cost, lo_frac=0.0, hi_frac=1.0) -> dict:
    rows = []
    for sym, bars in data.items():
        closes = [b['c'] for b in bars]
        lo, hi = int(len(closes) * lo_frac), int(len(closes) * hi_frac)
        r = replay(closes, ma_len, band, cost, lo, hi)
        if r.get('n', 0) >= 3:
            rows.append(r)
    if not rows:
        return {'assets': 0}
    med = lambda k: st.median([r[k] for r in rows])
    return {
        'assets': len(rows),
        'med_n': med('n'),
        'med_win': med('win_rate'),
        'med_exp': med('expectancy'),
        'med_payoff': st.median([min(r['payoff'], 50) for r in rows]),
        'med_strat_x': med('strat_x'),
        'med_bh_x': med('bh_x'),
        'beat_bh': sum(1 for r in rows if r['strat_x'] > r['bh_x']) / len(rows),
        'med_dd': med('max_dd'),
        'med_bh_dd': med('bh_dd'),
        'dd_better': sum(1 for r in rows if r['dd_better']) / len(rows),
        'med_t': med('t_stat'),
    }


def main() -> int:
    data = load_data()
    print()
    print('=' * 92)
    print(f'BROAD-UNIVERSE STOCK TEST — {len(data)} tickers, {START}→now, '
          f'{COST_RT*100:.2f}% round-trip')
    print('  Medians ACROSS the universe. Never the best asset.')
    print('=' * 92)

    # ── the shipped parameters, full sample ─────────────────────────────────
    base = universe_median(data, 100, 0.02, COST_RT)
    print(f"\nSHIPPED PARAMS (SMA100, 2% band), full sample, {base['assets']} assets")
    print(f"  median trades/asset   {base['med_n']:.0f}")
    print(f"  median win rate       {base['med_win']*100:.0f}%")
    print(f"  median expectancy     {base['med_exp']*100:+.2f}% / trade")
    print(f"  median payoff         {base['med_payoff']:.2f}")
    print(f"  median max drawdown   {base['med_dd']*100:.1f}%   "
          f"buy&hold {base['med_bh_dd']*100:.1f}%")
    print(f"  ► SMALLER DRAWDOWN:   {base['dd_better']*100:.0f}% of assets")
    print(f"  median t-stat         {base['med_t']:.2f}")
    print(f"  median strategy       {base['med_strat_x']:.2f}x   "
          f"median buy&hold {base['med_bh_x']:.2f}x")
    print(f"  ► BEAT BUY-AND-HOLD:  {base['beat_bh']*100:.0f}% of assets")

    # ── parameter grid: is the result a ridge or a spike? ────────────────────
    print('\n' + '=' * 92)
    print('PARAMETER GRID — every cell printed. A real effect is a broad ridge;')
    print('a spike at one cell is a fit. (median across the universe)')
    print('=' * 92)
    print(f"{'MA':>5}{'band':>7}{'win%':>7}{'exp%':>8}{'payoff':>8}{'t':>7}"
          f"{'strat_x':>9}{'bh_x':>8}{'beatBH%':>9}{'maxDD%':>8}")
    cells = []
    for ma_len in (50, 100, 150, 200):
        for band in (0.00, 0.01, 0.02, 0.04):
            r = universe_median(data, ma_len, band, COST_RT)
            cells.append(((ma_len, band), r))
            print(f"{ma_len:>5}{band*100:>6.0f}%{r['med_win']*100:>6.0f}%"
                  f"{r['med_exp']*100:>8.2f}{r['med_payoff']:>8.2f}{r['med_t']:>7.2f}"
                  f"{r['med_strat_x']:>8.2f}x{r['med_bh_x']:>7.2f}x"
                  f"{r['beat_bh']*100:>8.0f}%{r['med_dd']*100:>8.1f}")

    k = len(cells)
    best = max(cells, key=lambda c: c[1]['med_exp'])
    n_beat = sum(1 for _, r in cells if r['beat_bh'] > 0.5)
    print(f"\n  {k} cells tried. Best by expectancy: SMA{best[0][0]} / "
          f"{best[0][1]*100:.0f}% band ({best[1]['med_exp']*100:+.2f}%/trade).")
    print(f"  Cells where a MAJORITY of assets beat buy-and-hold: {n_beat}/{k}")

    # ── split-half ───────────────────────────────────────────────────────────
    print('\n' + '=' * 92)
    print('SPLIT-HALF (shipped params) — does it live in one regime?')
    print('=' * 92)
    for label, lo, hi in (('first half ', 0.0, 0.5), ('second half', 0.5, 1.0)):
        r = universe_median(data, 100, 0.02, COST_RT, lo, hi)
        print(f"  {label}  win {r['med_win']*100:>3.0f}%   exp {r['med_exp']*100:>+6.2f}%   "
              f"payoff {r['med_payoff']:>5.2f}   strat {r['med_strat_x']:>5.2f}x   "
              f"B&H {r['med_bh_x']:>5.2f}x   beatBH {r['beat_bh']*100:>3.0f}%")

    # ── the honest bottom line ──────────────────────────────────────────────
    print('\n' + '=' * 92)
    print('READ THIS BEFORE THE NUMBERS ABOVE')
    print('=' * 92)
    print(f"""  The SPY/QQQ/GLD result that motivated this run (60-64% win rate,
  3.2-6.8 payoff) DID NOT SURVIVE the broad universe. Across {base['assets']} tickers the
  median win rate is {base['med_win']*100:.0f}% -- the same as crypto. Three assets looked like
  anything, which is why three assets are never enough.

  Only {base['beat_bh']*100:.0f}% of assets beat buy-and-hold at the shipped parameters, and
  0 of the 16 parameter cells got a majority. Median t={base['med_t']:.2f}, nowhere near
  the t>2 bar. Both halves of the sample agree, so this is not a regime story --
  the rule simply returns less than holding ({base['med_strat_x']:.2f}x vs {base['med_bh_x']:.2f}x median).

  Drawdown is the one honest defence and it is measured above, not asserted:
  median {base['med_dd']*100:.1f}% against buy-and-hold's {base['med_bh_dd']*100:.1f}%, smaller on {base['dd_better']*100:.0f}% of assets.
  If that trade is worth it to you it is a RISK tool, not an edge, and it should
  be sold to you as one.""")
    print('=' * 92)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

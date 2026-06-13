"""
Trend-following research — is it a ROBUST edge or a fragile fit?

tsmom (long if close>SMA(N) else cash) is the only live candidate. Memory says
it "rests on ~1 bear." This tests it honestly:
  • real daily history from Kraken (BTC/ETH/SOL, as far back as the API gives)
  • lookahead-free (signal on close[t] → hold t+1's return)
  • honest cost: 0.26%/leg charged on every entry AND exit
  • swept across lookbacks {50,100,150,200} AND coins — the verdict is
    ROBUSTNESS across the whole grid, NOT the single best cell (that's the
    overfit trap the proof bar exists to catch).

Reports per cell: CAGR, Sharpe, maxDD, #round-trips, vs buy-and-hold. Then a
robustness summary: in how many cells does trend beat B&H on BOTH Sharpe and
maxDD? A real edge shows up almost everywhere; a fit shows up in one corner.
Read-only.
"""
from __future__ import annotations
import statistics as st
from math import sqrt

COINS = ['BTC/USD', 'ETH/USD', 'SOL/USD']
LOOKBACKS = [50, 100, 150, 200]
COST_LEG = 0.0026   # one-way taker; a long episode pays this twice (~0.52% RT)
ANN = 365


def fetch_daily(ex, sym, want=720):
    try:
        o = ex.fetch_ohlcv(sym, timeframe='1d', limit=want)
        return [(row[0], row[4]) for row in o if row[4]]  # (ts, close)
    except Exception as e:
        print(f"  [{sym}] fetch failed: {e}")
        return []


def sma(xs, n, i):
    if i + 1 < n:
        return None
    return sum(xs[i + 1 - n:i + 1]) / n


def metrics(rets):
    """rets: list of daily strategy returns (fractions)."""
    if not rets:
        return 0.0, 0.0, 0.0
    eq = 1.0
    peak = 1.0
    mdd = 0.0
    for r in rets:
        eq *= (1 + r)
        peak = max(peak, eq)
        mdd = min(mdd, eq / peak - 1)
    cagr = eq ** (ANN / len(rets)) - 1 if len(rets) > 0 else 0.0
    sd = st.pstdev(rets) if len(rets) > 1 else 0.0
    sharpe = (st.mean(rets) / sd * sqrt(ANN)) if sd > 0 else 0.0
    return cagr, sharpe, mdd


def backtest(closes, n):
    """Long if close>SMA(n) else cash. Returns (strat_rets, n_roundtrips, episodes)."""
    rets = []
    pos = 0          # 0 cash, 1 long
    rts = 0
    episodes = []    # net return of each long episode
    ep_entry_eq = None
    eq = 1.0
    for i in range(len(closes) - 1):
        s = sma(closes, n, i)
        want = 1 if (s is not None and closes[i] > s) else 0
        # cost on transition
        r = 0.0
        if want != pos:
            r -= COST_LEG          # pay a leg on entry or exit
            rts += 0.5             # two legs = one round-trip
            if want == 1:
                ep_entry_eq = eq * (1 - COST_LEG)
            elif pos == 1 and ep_entry_eq:
                episodes.append(eq / ep_entry_eq - 1)
                ep_entry_eq = None
        # next-day return if long
        day = closes[i + 1] / closes[i] - 1
        if want == 1:
            r += day
        eq *= (1 + r)
        rets.append(r)
        pos = want
    return rets, int(rts), episodes


def bh_metrics(closes):
    rets = [closes[i + 1] / closes[i] - 1 for i in range(len(closes) - 1)]
    return metrics(rets)


def main():
    try:
        import ccxt
    except Exception as e:
        print("ccxt unavailable:", e); return
    ex = ccxt.kraken({'enableRateLimit': True})

    data = {}
    for c in COINS:
        series = fetch_daily(ex, c)
        if series:
            data[c] = [px for _t, px in series]
            print(f"  {c}: {len(series)} daily candles")
    if not data:
        print("no data"); return

    print("\n" + "=" * 74)
    print("TREND-FOLLOWING ROBUSTNESS  (long>SMA(N) else cash, honest 0.52% RT cost)")
    print("=" * 74)
    print(f"{'coin':<9}{'N':>5}{'CAGR':>9}{'Sharpe':>8}{'maxDD':>8}{'RTs':>5}"
          f"{'  | B&H CAGR/Sharpe/DD'}")
    print("-" * 74)

    beats = 0
    cells = 0
    portfolio_rets_by_n = {n: [] for n in LOOKBACKS}
    for c in COINS:
        closes = data[c]
        bh_c, bh_s, bh_dd = bh_metrics(closes)
        for n in LOOKBACKS:
            rets, rts, eps = backtest(closes, n)
            cg, sh, dd = metrics(rets)
            cells += 1
            won = (sh > bh_s and dd > bh_dd)   # better risk-adj AND shallower DD
            if won:
                beats += 1
            flag = "  <-- beats B&H" if won else ""
            print(f"{c:<9}{n:>5}{cg*100:>8.0f}%{sh:>8.2f}{dd*100:>7.0f}%{rts:>5}"
                  f"   {bh_c*100:>5.0f}%/{bh_s:.2f}/{bh_dd*100:.0f}%{flag}")
            # accumulate equal-weight portfolio (align by truncating to min len later)
            portfolio_rets_by_n[n].append(rets)
        print()

    # Equal-weight 3-coin portfolio per N (rebalanced daily, same signal per coin)
    print("-" * 74)
    print("EQUAL-WEIGHT BTC/ETH/SOL PORTFOLIO (the tsmom config):")
    for n in LOOKBACKS:
        series = portfolio_rets_by_n[n]
        if len(series) < len(COINS):
            continue
        m = min(len(s) for s in series)
        port = [st.mean([s[-m:][i] for s in series]) for i in range(m)]
        cg, sh, dd = metrics(port)
        print(f"  N={n:<4} CAGR={cg*100:>5.0f}%  Sharpe={sh:>5.2f}  maxDD={dd*100:>4.0f}%")

    print("\n" + "=" * 74)
    print(f"ROBUSTNESS: trend beats buy-&-hold (Sharpe AND maxDD) in {beats}/{cells} cells.")
    if beats >= cells * 0.7:
        print("=> ROBUST across coins & lookbacks — a real candidate edge, not a fit.")
    elif beats >= cells * 0.4:
        print("=> MIXED — works in some cells; treat as fragile, lean on forward proof.")
    else:
        print("=> FRAGILE — only isolated cells; likely a fit. Don't trust it.")
    print("NOTE: ~2yr of daily data (Kraken cap). Proof is still the FORWARD test")
    print("(tsmom_paper, n>=30 vs the scorecard), not this backtest.")


if __name__ == '__main__':
    main()

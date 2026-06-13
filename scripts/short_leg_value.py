"""
Is shorting worth it? — the decision BEFORE wiring Kraken US perps.

Perps unlock the SHORT side of the robust trend winner (tsmom_50). But shorting
isn't free: vs the long-only book you can already trade, the only thing perps add
is the ability to be SHORT in a downtrend instead of sitting in CASH. So the honest
question is narrow:

    Does being SHORT in downtrends beat being FLAT in downtrends, AFTER the
    perp-specific 8h FUNDING cost the spot backtest never charged?

This decomposes tsmom_50 on real BTC/ETH/SOL daily into:
  * long/flat  (pos in {0,+1})  -- what a spot account does today
  * long/short (pos in {-1,+1}) -- what perps would let it do
  * SHORT-LEG VALUE = long/short - long/flat  (pure value of shorting vs cash)
then charges a funding drag on the days spent short and reports the BREAK-EVEN
funding APY at which the short edge vanishes. Run 1x notional (no leverage) -- the
disciplined config; leverage ruin is already settled (doubling_in_a_month). Read-only.
"""
from __future__ import annotations
import numpy as np
import pandas as pd

SMA_N = 50                 # the tournament's robust winner
TRADE_COST_LEG = 0.0005    # perp taker ~5bps/side (much cheaper than spot 0.26%)
NOTIONAL = 1000.0
ANN = 365


def fetch(ex, sym, want=730):
    o = ex.fetch_ohlcv(sym, timeframe='1d', limit=want)
    return pd.DataFrame(o, columns=['ts', 'o', 'h', 'l', 'close', 'v'])


def legs(df):
    c = df['close']
    raw = np.sign(c - c.rolling(SMA_N).mean()).fillna(0)     # -1 / 0 / +1
    ret = c.pct_change().fillna(0)

    def equity(pos):
        held = pos.shift(1).fillna(0)
        gross = held * ret
        cost = held.diff().abs().fillna(0) * TRADE_COST_LEG
        return (1 + gross - cost).cumprod() * NOTIONAL, held

    long_flat = raw.clip(0, 1)                               # short -> cash
    long_short = raw.replace(0, np.nan).ffill().fillna(0)    # always +1/-1 once warm
    eq_lf, _ = equity(long_flat)
    eq_ls, held_ls = equity(long_short)

    days_short = int((held_ls < 0).sum())
    short_value = eq_ls.iloc[-1] - eq_lf.iloc[-1]            # $ added by shorting vs cash
    return dict(eq_lf=eq_lf.iloc[-1], eq_ls=eq_ls.iloc[-1],
                short_value=short_value, days_short=days_short)


def main():
    import ccxt
    ex = ccxt.kraken({'enableRateLimit': True})
    print("=" * 84)
    print(f"IS SHORTING WORTH IT? -- tsmom_{SMA_N}, 1x notional, real daily data, "
          f"perp taker {TRADE_COST_LEG*100:.2f}%/side")
    print("=" * 84)
    print(f"{'coin':<10}{'long/flat':>12}{'long/short':>12}{'short adds':>12}"
          f"{'days short':>12}{'breakeven APY':>16}")
    print("-" * 84)
    agg = {'eq_lf': 0.0, 'eq_ls': 0.0, 'short_value': 0.0, 'days_short': 0}
    for sym in ('BTC/USD', 'ETH/USD', 'SOL/USD'):
        r = legs(fetch(ex, sym))
        # break-even funding APY: short-leg value = NOTIONAL * APY * days_short/365
        be = (r['short_value'] / (NOTIONAL * r['days_short'] / ANN)) * 100 \
            if r['days_short'] else float('nan')
        print(f"{sym:<10}${r['eq_lf']:>10.0f}${r['eq_ls']:>10.0f}"
              f"${r['short_value']:>+10.0f}{r['days_short']:>12}{be:>+15.0f}%")
        for k in agg:
            agg[k] += r[k]
    print("-" * 84)
    be_agg = (agg['short_value'] / (NOTIONAL * agg['days_short'] / ANN)) * 100 \
        if agg['days_short'] else float('nan')
    print(f"{'TOTAL':<10}${agg['eq_lf']:>10.0f}${agg['eq_ls']:>10.0f}"
          f"${agg['short_value']:>+10.0f}{agg['days_short']:>12}{be_agg:>+15.0f}%")

    print("\nSHORT-LEG VALUE under a funding drag (subtract funding paid while short):")
    for f in (0, 10, 25, 50, 100):
        drag = NOTIONAL * (f / 100) * agg['days_short'] / ANN
        net = agg['short_value'] - drag
        verdict = "shorting WINS" if net > 0 else "shorting LOSES (just hold cash)"
        print(f"  funding {f:>3}% APY -> short adds ${net:>+8.0f}   {verdict}")

    print("\nREAD: 'short adds' > 0 means perps beat sitting in cash in downtrends.")
    print("Break-even APY = the funding cost that erases that edge. Kraken's 8h")
    print("funding annualizes to roughly +-10..50% in normal regimes (negative =")
    print("shorts PAY). If break-even >> typical funding, shorting is worth wiring;")
    print("if it's thin, the long-only arms already capture most of the edge.")


if __name__ == '__main__':
    main()

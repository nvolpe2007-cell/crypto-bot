"""
Doubling-in-a-month reality check — can any strategy/confluence turn $1k into
$2k in 30 days? Measured on real BTC/ETH daily (~2yr), honestly, incl. leverage.

Answers: across every rolling 30-day window, what's the BEST/median/WORST month
for (a) buy & hold, (b) the tournament's best robust bot (tsmom_50), and (c)
leveraged versions — and how often did each DOUBLE (>=+100%) vs get RUINED
(a single day's leveraged move <= -100% = liquidation)? Read-only.
"""
from __future__ import annotations
import numpy as np
import pandas as pd

COST_LEG = 0.0025
WIN = 30


def fetch(ex, sym, want=730):
    o = ex.fetch_ohlcv(sym, timeframe='1d', limit=want)
    return pd.DataFrame(o, columns=['ts', 'o', 'h', 'l', 'close', 'v'])


def tsmom50_daily_returns(df):
    c = df['close']
    pos = (np.sign(c - c.rolling(50).mean())).fillna(0)
    held = pos.shift(1).fillna(0)
    gross = held * c.pct_change().fillna(0)
    cost = held.diff().abs().fillna(0) * COST_LEG
    return (gross - cost).fillna(0)


def lever(daily, L):
    """Leveraged daily returns with liquidation: a day worse than -1/L wipes the
    account (equity -> ~0 and stays). Funding/borrow on leverage ignored (generous)."""
    out = []
    alive = True
    for r in daily:
        if not alive:
            out.append(0.0); continue
        lr = L * r
        if lr <= -0.98:        # margin call / liquidation
            out.append(-0.98); alive = False
        else:
            out.append(lr)
    return pd.Series(out, index=daily.index)


def roll_stats(daily, label):
    comp = (1 + daily).rolling(WIN).apply(np.prod, raw=True) - 1
    comp = comp.dropna()
    if comp.empty:
        return
    p_double = (comp >= 1.0).mean() * 100
    p_ruin = (comp <= -0.5).mean() * 100
    print(f"  {label:<26} best {comp.max()*100:>+6.0f}%  median {comp.median()*100:>+5.0f}%"
          f"  worst {comp.min()*100:>+6.0f}%   doubled {p_double:>4.0f}% of months"
          f"   lost>50% {p_ruin:>4.0f}%")


def main():
    import ccxt
    ex = ccxt.kraken({'enableRateLimit': True})
    print("=" * 92)
    print(f"DOUBLING-IN-A-MONTH CHECK -- rolling {WIN}-day returns on real data (~2yr)")
    print("=" * 92)
    for sym in ('BTC/USD', 'ETH/USD'):
        df = fetch(ex, sym)
        bh = df['close'].pct_change().fillna(0)
        ts = tsmom50_daily_returns(df)
        print(f"\n{sym}:")
        roll_stats(bh, "buy & hold (1x)")
        roll_stats(ts, "tsmom_50 (1x, the winner)")
        for L in (3, 5, 10):
            roll_stats(lever(ts, L), f"tsmom_50 @ {L}x leverage")
        roll_stats(lever(bh, 10), "buy & hold @ 10x")
    print("\n" + "=" * 92)
    print("READ: doubling in a month only shows up at high leverage -- the SAME")
    print("leverage whose 'worst month' wipes the account (one -10..-20% day at")
    print("5-10x = liquidation). 'Doubles reliably' does not exist; 'doubles")
    print("sometimes, busts other times' is just a coin-flip with extra steps.")


if __name__ == '__main__':
    main()

"""
ONE-YEAR backtest of the DEPLOYED forward arms — do the things now running on
paper actually have a 1-year edge, or are we forward-testing noise?

Backtests the EXACT strategies live on the VPS (not a fresh search) over the most
recent 365 daily bars on BTC/ETH/SOL, honest cost. Warmup matters: the slow signals
need history BEFORE the window, so we fetch lookback+365 bars, compute positions over
the full series, then score ONLY the last 365 days (no warmup bias, no lookahead).

Arms backtested (matching the live runners):
  * tsmom_long_200  — the original slow trend arm (tsmom_paper.py, SMA200, long/flat)
  * tsmom_long_50   — the fast trend arm (tsmom_fast, SMA50, long/flat)
  * conf_trend_momo — the confluence arm (conf_paper.py: >SMA100 AND 20d momo up)
  * sma_long_75     — long-only candidate
  * tsmom_50_LS     — the L/S perp arm (tsmom_ls_paper.py: +/-1 by SMA50, perp cost+funding)
vs buy & hold. Caveat: 1 year is ~one regime — read alongside the ~2yr tournaments.
Read-only; ccxt Kraken daily.
"""
from __future__ import annotations
import numpy as np
import pandas as pd

SPOT_COST = 0.0054          # Kraken spot round-trip (maker x2 + slippage), matches tsmom_paper
PERP_COST = 0.0015          # perp taker + slippage round-trip, matches tsmom_ls_paper
FUNDING_APY = 0.10          # conservative perp funding drag, matches tsmom_ls_paper
WINDOW = 365
WARMUP = 220                # > SMA200 so the slow arm has signal from day 1 of the window
ANN = 365


def sma(s, n): return s.rolling(n).mean()


def fetch(ex, sym, want=WINDOW + WARMUP + 10):
    o = ex.fetch_ohlcv(sym, timeframe='1d', limit=want)
    return pd.DataFrame(o, columns=['ts', 'o', 'h', 'l', 'close', 'v'])


def positions(df):
    """Position series for each deployed arm (long/flat in [0,1], or +/-1 for L/S)."""
    c = df['close']
    momo20 = c > c.shift(20)
    return {
        'tsmom_long_200':  (c > sma(c, 200)).astype(float),
        'tsmom_long_50':   (c > sma(c, 50)).astype(float),
        'conf_trend_momo': ((c > sma(c, 100)) & momo20).astype(float),
        'sma_long_75':     (c > sma(c, 75)).astype(float),
        'tsmom_50_LS':     np.sign(c - sma(c, 50)).replace(0, np.nan).ffill().fillna(0),
    }


def backtest(df, pos, cost, funding_apy=0.0):
    """Score ONLY the last WINDOW bars. Funding (perp) charged on held notional/day."""
    ret = df['close'].pct_change().fillna(0)
    held = pos.shift(1).fillna(0)
    gross = held * ret
    trade_cost = held.diff().abs().fillna(0) * cost
    fund = held.abs() * (funding_apy / ANN)          # daily funding drag while in a position
    net = (gross - trade_cost - fund).iloc[-WINDOW:]
    eq = (1 + net).cumprod() * 1000
    sd = net.std()
    sharpe = net.mean() / sd * np.sqrt(ANN) if sd > 0 else 0.0
    mdd = ((eq / eq.cummax()) - 1).min()
    trades = int((held.diff().abs() > 0).iloc[-WINDOW:].sum())
    return dict(final=eq.iloc[-1], sharpe=sharpe, mdd=mdd, trades=trades)


def main():
    import ccxt
    ex = ccxt.kraken({'enableRateLimit': True})
    coins = ('BTC/USD', 'ETH/USD', 'SOL/USD')
    data = {c: fetch(ex, c) for c in coins}
    days = min(len(d) for d in data.values())
    print("=" * 84)
    print(f"ONE-YEAR BACKTEST of the DEPLOYED arms -- last {WINDOW}d, BTC/ETH/SOL, honest cost")
    print(f"(fetched ~{days} bars/coin incl. warmup; only the last {WINDOW} scored)")
    print("=" * 84)
    print(f"{'arm':<18}{'$1k->avg':>11}{'Sharpe':>9}{'maxDD':>8}{'trades':>8}  per-coin $1k")
    print("-" * 84)

    arms = ['tsmom_long_200', 'tsmom_long_50', 'conf_trend_momo', 'sma_long_75', 'tsmom_50_LS']
    rows = {}
    for arm in arms:
        finals, shs, mdds, trs = [], [], [], []
        for c, df in data.items():
            pos = positions(df)[arm]
            is_ls = arm.endswith('_LS')
            m = backtest(df, pos, PERP_COST if is_ls else SPOT_COST,
                         FUNDING_APY if is_ls else 0.0)
            finals.append(m['final']); shs.append(m['sharpe'])
            mdds.append(m['mdd']); trs.append(m['trades'])
        rows[arm] = np.mean(finals)
        pc = "  ".join(f"{c.split('/')[0]} ${f:.0f}" for c, f in zip(data, finals))
        print(f"{arm:<18}${np.mean(finals):>9.0f}{np.mean(shs):>9.2f}"
              f"{np.mean(mdds)*100:>7.0f}%{int(np.mean(trs)):>8}  {pc}")

    # buy & hold benchmark over the same window
    bh_f, bh_s, bh_m = [], [], []
    for c, df in data.items():
        m = backtest(df, pd.Series(1.0, index=df.index), 0.0)
        bh_f.append(m['final']); bh_s.append(m['sharpe']); bh_m.append(m['mdd'])
    print("-" * 84)
    print(f"{'[buy & hold]':<18}${np.mean(bh_f):>9.0f}{np.mean(bh_s):>9.2f}"
          f"{np.mean(bh_m)*100:>7.0f}%{1:>8}  "
          + "  ".join(f"{c.split('/')[0]} ${f:.0f}" for c, f in zip(data, bh_f)))

    bh = np.mean(bh_f)
    best = max(rows, key=rows.get)
    print(f"\nBest arm: {best} (${rows[best]:.0f}) vs buy & hold (${bh:.0f}).")
    beat = [a for a, f in rows.items() if f > bh]
    print(f"Arms beating buy & hold this 1y window: {beat if beat else 'NONE'}")
    print("\nREAD: 1 year is ~one regime. An arm that beats B&H here AND in the ~2yr")
    print("tournaments is a real candidate; one that only wins in one window is a fit.")
    print("The forward paper arms remain the actual proof (proof_scorecard, n>=30).")


if __name__ == '__main__':
    main()

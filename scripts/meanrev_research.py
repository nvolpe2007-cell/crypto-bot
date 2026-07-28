#!/usr/bin/env python3
"""
Mean-reversion / buy-the-dip research — a structurally NEW (reversal, not momentum) bet.

Motivated directly by THIS project's own data: the long/short momentum test
(ls_momentum_short_leg_loses) found the SHORT leg loses because beaten-down coins
BOUNCE (squeezes / dead-cat rallies). The flip side of "shorting losers loses" is
"BUYING losers might win." This screens that, long-only spot-executable.

Prior is skeptical: memory market_structure_signals_verdict / strategy_tournament say
"MR dead" — but that was HIGH-FREQUENCY single-coin MR crushed by cost. This tests
LOW-frequency cross-sectional daily reversal + oversold-bounce, cost-netted.

Variants (all long-only, Kraken spot 0.26% taker on turnover):
  * XS_REVERSAL: every H days, rank majors by trailing L-day return, BUY the bottom-N
    (biggest losers), equal-weight, hold H days. (contrarian to xsec momentum)
  * OVERSOLD_BOUNCE: hold a coin while RSI(2) < entry threshold, exit when RSI(2)
    recovers above exit threshold (classic Connors short-term MR), equal-weight sleeves.
Benchmark: equal-weight buy&hold. 720d, OOS=last 40%. Screen, not proof.

    python scripts/meanrev_research.py
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import ccxt

BASKET = ["BTC", "ETH", "SOL", "XRP", "ADA", "LINK", "LTC", "BCH", "AVAX", "DOGE"]
BARS = 720
TAKER = 0.0026
OOS_FRAC = 0.40


def fetch_close(ex, base):
    try:
        o = ex.fetch_ohlcv(f"{base}/USD", timeframe="1d", limit=BARS)
    except Exception:
        return None
    return pd.Series([r[4] for r in o], index=pd.to_datetime([r[0] for r in o], unit="ms")) if o else None


def build_panel(ex):
    cols = {}
    for b in BASKET:
        s = fetch_close(ex, b)
        if s is not None and len(s) > 400:
            cols[b] = s
    return pd.DataFrame(cols).sort_index().dropna()


def rsi_series(px: pd.Series, n=2):
    d = px.diff()
    up = d.clip(lower=0).rolling(n).mean()
    dn = (-d.clip(upper=0)).rolling(n).mean()
    rs = up / dn.replace(0, np.nan)
    return (100 - 100 / (1 + rs)).fillna(50)


def equity_from_weights(prices, W):
    """W: DataFrame of daily target weights (fraction of book, long-only). Cost on turnover."""
    rets = prices.pct_change().fillna(0.0)
    eq = 1.0
    out = pd.Series(1.0, index=prices.index)
    prev = pd.Series(0.0, index=prices.columns)
    for i in range(1, len(prices)):
        w = W.iloc[i - 1]                      # yesterday's weights earn today's return
        eq *= (1.0 + float((w * rets.iloc[i]).sum()))
        turnover = float((W.iloc[i] - prev).abs().sum()) if i < len(prices) else 0.0
        # charge turnover when target changes
        turnover = float((W.iloc[i] - W.iloc[i - 1]).abs().sum())
        eq *= (1.0 - turnover * TAKER)
        prev = w
        out.iloc[i] = eq
    return out


def xs_reversal_weights(prices, lookback, hold, n):
    W = pd.DataFrame(0.0, index=prices.index, columns=prices.columns)
    start = lookback + 1
    cur = pd.Series(0.0, index=prices.columns)
    for i in range(start, len(prices)):
        if (i - start) % hold == 0:
            trail = prices.iloc[i] / prices.iloc[i - lookback] - 1
            losers = trail.sort_values().index[:n]     # biggest losers
            cur = pd.Series(0.0, index=prices.columns)
            for p in losers:
                cur[p] = 1.0 / n
        W.iloc[i] = cur
    return W


def oversold_bounce_weights(prices, rsi_n=2, entry=10, exit=70, max_hold=5):
    W = pd.DataFrame(0.0, index=prices.index, columns=prices.columns)
    rsis = {c: rsi_series(prices[c], rsi_n) for c in prices.columns}
    n = prices.shape[1]
    held = {c: 0 for c in prices.columns}   # days held
    inpos = {c: False for c in prices.columns}
    for i in range(rsi_n + 1, len(prices)):
        for c in prices.columns:
            r = rsis[c].iloc[i]
            if inpos[c]:
                held[c] += 1
                if r >= exit or held[c] >= max_hold:
                    inpos[c] = False
            elif r <= entry:
                inpos[c] = True
                held[c] = 0
        active = [c for c in prices.columns if inpos[c]]
        if active:
            for c in active:
                W.iat[i, W.columns.get_loc(c)] = 1.0 / n  # equal SLOT weight; rest cash
    return W


def stats(eq):
    dr = eq.pct_change().dropna()
    return {"ret": eq.iloc[-1] / eq.iloc[0] - 1,
            "sharpe": (dr.mean() / dr.std() * np.sqrt(365)) if dr.std() > 0 else 0,
            "maxdd": float((eq / eq.cummax() - 1).min())}


def report(name, eq):
    oos_i = int(len(eq) * (1 - OOS_FRAC))
    f, ins, oos = stats(eq), stats(eq.iloc[:oos_i]), stats(eq.iloc[oos_i:] / eq.iloc[oos_i])
    flag = "  <- overfit (IS+/OOS-)" if ins["ret"] > 0 and oos["ret"] < 0 else ""
    print(f"{name:<26} FULL ret={f['ret']*100:>7.1f}% sh={f['sharpe']:>5.2f} dd={f['maxdd']*100:>6.1f}%"
          f"  |  OOS ret={oos['ret']*100:>7.1f}% sh={oos['sharpe']:>5.2f} (IS={ins['ret']*100:>6.1f}%){flag}")


def main():
    ex = ccxt.kraken({"enableRateLimit": True})
    print("Fetching majors basket ...")
    prices = build_panel(ex)
    print(f"basket ({prices.shape[1]}): {', '.join(prices.columns)}  {len(prices)}d "
          f"{prices.index[0].date()}->{prices.index[-1].date()}\n")

    ew = (1 + prices.pct_change().fillna(0).mean(axis=1)).cumprod()
    print("BENCHMARK")
    report("buyhold_equalweight", ew)

    print("\nXS_REVERSAL (buy the biggest L-day losers, hold H days, top-N)")
    for lb in (2, 3, 5, 7):
        for hold in (lb, lb * 2):
            for n in (2, 3):
                report(f"XSrev lb={lb} hold={hold} N={n}",
                       equity_from_weights(prices, xs_reversal_weights(prices, lb, hold, n)))

    print("\nOVERSOLD_BOUNCE (Connors RSI2: buy when RSI2<=entry, exit RSI2>=exit or maxhold)")
    for entry in (5, 10, 15):
        for exit in (60, 70):
            report(f"RSI2 entry<={entry} exit>={exit}",
                   equity_from_weights(prices, oversold_bounce_weights(prices, 2, entry, exit, 5)))

    print("\nNOTE: 2yr screen. Prior is 'MR dead' at cost; this checks whether LOW-freq")
    print("cross-sectional reversal survives, motivated by the 'losers bounce' finding.")


if __name__ == "__main__":
    main()

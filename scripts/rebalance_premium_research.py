#!/usr/bin/env python3
"""
Rebalancing-premium / Shannon's-demon research — the last NON-directional stone.

Every directional idea this project has tested is dead at the cost wall (see
edge_search_verdict, lev_perp_pattern_search, xsec_momentum_verdict,
ls_momentum_short_leg_loses). This tests a PREDICTION-FREE structural edge: does
periodically rebalancing a fixed-weight basket back to target harvest volatility
(sell-the-winner / buy-the-loser) and beat buy-and-hold, NET of turnover cost?

The premium is real for volatile, imperfectly-correlated, mean-reverting assets. It is
KNOWN to be weak/negative when (a) assets are highly correlated (crypto majors ~0.8),
or (b) one asset trends hard (rebalancing sells the winner too early). So the honest
prior is skeptical — this measures whether it survives anyway.

Tests, all long-only spot-executable on Kraken TODAY (no shorting/leverage):
  * buy&hold equal-weight (drift, no rebalance)          <- benchmark
  * rebalanced equal-weight basket @ {daily, weekly, monthly}, cost-netted
  * Shannon's demon: f% basket / (1-f)% cash, rebalanced vs the same drifting
    -> premium = rebalanced_return - buyhold_return (net). Positive = a real edge.

Cost: Kraken spot taker 0.26%/side on turnover. 720d daily, OOS = last 40%.

    python scripts/rebalance_premium_research.py
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import ccxt

# curated liquid majors — a realistic rebalanced portfolio (not microcaps)
BASKET = ["BTC", "ETH", "SOL", "XRP", "ADA", "LINK", "LTC", "BCH", "AVAX", "DOGE"]
BARS = 720
TAKER = 0.0026
OOS_FRAC = 0.40


def fetch_close(ex, base):
    try:
        o = ex.fetch_ohlcv(f"{base}/USD", timeframe="1d", limit=BARS)
    except Exception:
        return None
    if not o:
        return None
    return pd.Series([r[4] for r in o], index=pd.to_datetime([r[0] for r in o], unit="ms"))


def build_panel(ex):
    cols = {}
    for b in BASKET:
        s = fetch_close(ex, b)
        if s is not None and len(s) > 400:
            cols[b] = s
    return pd.DataFrame(cols).sort_index().dropna()


def buyhold_equalweight(prices):
    """Invest 1/N each at t0, let weights drift. Equity = mean of normalized paths."""
    norm = prices / prices.iloc[0]
    return norm.mean(axis=1)


def rebalanced(prices, rebal_d, risky_frac=1.0):
    """Rebalance to equal-weight (risky_frac in basket, rest cash) every rebal_d days.
    Returns cost-netted equity curve (start 1.0)."""
    rets = prices.pct_change().fillna(0.0)
    n = prices.shape[1]
    target = pd.Series(risky_frac / n, index=prices.columns)  # per-asset target weight
    w = target.copy()
    cash_w = 1.0 - risky_frac
    eq = 1.0
    out = pd.Series(1.0, index=prices.index)
    for i in range(1, len(prices)):
        # grow risky weights by returns; cash unchanged
        w = w * (1 + rets.iloc[i])
        day_val = float(w.sum()) + cash_w
        eq *= day_val
        # renormalize weights to fractions of the (grown) book
        w = w / day_val
        cash_w = cash_w / day_val
        if i % rebal_d == 0:
            new_w = target.copy()
            new_cash = 1.0 - risky_frac
            turnover = float((new_w - w).abs().sum()) + abs(new_cash - cash_w)
            eq *= (1.0 - turnover * TAKER)
            w, cash_w = new_w, new_cash
        out.iloc[i] = eq
    return out


def stats(eq):
    dr = eq.pct_change().dropna()
    return {"ret": eq.iloc[-1] / eq.iloc[0] - 1,
            "sharpe": (dr.mean() / dr.std() * np.sqrt(365)) if dr.std() > 0 else 0,
            "maxdd": float((eq / eq.cummax() - 1).min())}


def split(eq):
    oos_i = int(len(eq) * (1 - OOS_FRAC))
    return stats(eq), stats(eq.iloc[:oos_i]), stats(eq.iloc[oos_i:] / eq.iloc[oos_i])


def report(name, eq, base_full=None):
    f, ins, oos = split(eq)
    prem = f"  premium={f['ret']*100 - base_full['ret']*100:>+6.1f}pt" if base_full else ""
    print(f"{name:<28} FULL ret={f['ret']*100:>7.1f}% sh={f['sharpe']:>5.2f} dd={f['maxdd']*100:>6.1f}%"
          f"  |  OOS ret={oos['ret']*100:>7.1f}% (IS={ins['ret']*100:>6.1f}%){prem}")
    return f


def main():
    ex = ccxt.kraken({"enableRateLimit": True})
    print("Fetching liquid-majors basket ...")
    prices = build_panel(ex)
    print(f"basket ({prices.shape[1]}): {', '.join(prices.columns)}  "
          f"{prices.index[0].date()}->{prices.index[-1].date()}, {len(prices)}d")
    corr = prices.pct_change().corr()
    avg_corr = (corr.values[np.triu_indices_from(corr.values, 1)]).mean()
    print(f"avg pairwise daily-return correlation = {avg_corr:.2f} "
          f"({'high -> weak premium expected' if avg_corr > 0.6 else 'moderate'})\n")

    print("FULLY-INVESTED BASKET (100% risky)")
    bh = buyhold_equalweight(prices)
    base = report("buyhold_equalweight (drift)", bh)
    for d, lab in [(1, "daily"), (7, "weekly"), (21, "monthly")]:
        report(f"rebalanced_{lab}", rebalanced(prices, d, 1.0), base)

    print("\nSHANNON'S DEMON (part cash, rebalanced) vs the SAME split drifting")
    for f in (0.5, 0.7):
        print(f"  -- {int(f*100)}% basket / {int((1-f)*100)}% cash --")
        # drifting benchmark: hold f in basket + (1-f) cash, never rebalance
        drift = f * bh + (1 - f) * 1.0
        db = report(f"  buyhold_{int(f*100)}_{int((1-f)*100)} (drift)", drift)
        for d, lab in [(7, "weekly"), (21, "monthly")]:
            report(f"  rebal_{lab}_{int(f*100)}_{int((1-f)*100)}", rebalanced(prices, d, f), db)

    print("\nNOTE: premium column = rebalanced_return - matched_buyhold_return (net of 0.26%")
    print("taker turnover). Positive => volatility harvest beats hold; negative => rebalancing")
    print("sold winners too early / churned cost. 2yr screen; a winner -> forward paper proof.")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
Enhanced rebalancing — refine the ONE edge that works (rebalance_premium_verdict).

The rebalancing premium is real but modest, and it's WEAK because the crypto majors are
~0.70 correlated (little dispersion to harvest) and all fall together in a bear. Two
theory-backed enhancements, both long-only spot-executable on Kraken TODAY:

  1. ADD AN UNCORRELATED ASSET: tokenized gold (PAXG) is ~0 correlated to crypto and has
     its own positive drift. Adding it to a rebalanced basket should (a) raise the
     harvest (more dispersion) and (b) cut drawdown (real diversification) — the whole
     point of rebalancing is strongest across uncorrelated, volatile sleeves.
  2. THRESHOLD REBALANCING: rebalance a sleeve only when its weight drifts more than a
     BAND off target (buy-the-dip / trim-the-rip on moves, not the calendar) — often
     harvests more vol per unit of turnover than fixed-cadence.

Compares crypto-only vs crypto+gold vs crypto+gold+cash, buy&hold vs monthly vs
threshold rebalance, net of 0.26% spot taker. 720d, OOS=last 40%. Screen, not proof.

    python scripts/rebalance_plus_research.py
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import ccxt

CRYPTO = ["BTC", "ETH", "SOL", "XRP", "ADA", "LINK", "LTC", "BCH", "AVAX", "DOGE"]
GOLD = "PAXG"
BARS = 720
TAKER = 0.0026
OOS_FRAC = 0.40


def fetch_close(ex, base):
    try:
        o = ex.fetch_ohlcv(f"{base}/USD", timeframe="1d", limit=BARS)
    except Exception:
        return None
    return pd.Series([r[4] for r in o], index=pd.to_datetime([r[0] for r in o], unit="ms")) if o else None


def build_panel(ex, bases):
    cols = {}
    for b in bases:
        s = fetch_close(ex, b)
        if s is not None and len(s) > 400:
            cols[b] = s
    return pd.DataFrame(cols).sort_index().dropna()


def buyhold(prices, target: pd.Series):
    """Invest target weights at t0, drift. target sums to <=1; remainder is cash."""
    norm = prices / prices.iloc[0]
    return (norm * target).sum(axis=1) + (1.0 - float(target.sum()))


def rebal_calendar(prices, target: pd.Series, rebal_d):
    rets = prices.pct_change().fillna(0.0)
    cash = 1.0 - float(target.sum())
    w = target.copy()
    cash_w = cash
    eq, out = 1.0, pd.Series(1.0, index=prices.index)
    for i in range(1, len(prices)):
        w = w * (1 + rets.iloc[i])
        val = float(w.sum()) + cash_w
        eq *= val
        w, cash_w = w / val, cash_w / val
        if i % rebal_d == 0:
            tw = target.copy()
            turnover = float((tw - w).abs().sum()) + abs(cash - cash_w)
            eq *= (1 - turnover * TAKER)
            w, cash_w = tw, cash
        out.iloc[i] = eq
    return out


def rebal_threshold(prices, target: pd.Series, band):
    """Rebalance to target only when any sleeve drifts > band (relative) off target."""
    rets = prices.pct_change().fillna(0.0)
    cash = 1.0 - float(target.sum())
    w = target.copy()
    cash_w = cash
    eq, out = 1.0, pd.Series(1.0, index=prices.index)
    for i in range(1, len(prices)):
        w = w * (1 + rets.iloc[i])
        val = float(w.sum()) + cash_w
        eq *= val
        w, cash_w = w / val, cash_w / val
        drift = ((w - target).abs() / target.replace(0, np.nan)).max()
        if drift > band:
            tw = target.copy()
            turnover = float((tw - w).abs().sum()) + abs(cash - cash_w)
            eq *= (1 - turnover * TAKER)
            w, cash_w = tw, cash
        out.iloc[i] = eq
    return out


def stats(eq):
    dr = eq.pct_change().dropna()
    return {"ret": eq.iloc[-1] / eq.iloc[0] - 1,
            "sharpe": (dr.mean() / dr.std() * np.sqrt(365)) if dr.std() > 0 else 0,
            "maxdd": float((eq / eq.cummax() - 1).min())}


def report(name, eq):
    oos_i = int(len(eq) * (1 - OOS_FRAC))
    f, ins, oos = stats(eq), stats(eq.iloc[:oos_i]), stats(eq.iloc[oos_i:] / eq.iloc[oos_i])
    print(f"{name:<30} FULL ret={f['ret']*100:>7.1f}% sh={f['sharpe']:>5.2f} dd={f['maxdd']*100:>6.1f}%"
          f"  |  OOS ret={oos['ret']*100:>7.1f}% sh={oos['sharpe']:>5.2f} (IS={ins['ret']*100:>6.1f}%)")


def main():
    ex = ccxt.kraken({"enableRateLimit": True})
    print("Fetching crypto + gold ...")
    prices = build_panel(ex, CRYPTO + [GOLD])
    has_gold = GOLD in prices.columns
    crypto_cols = [c for c in prices.columns if c != GOLD]
    print(f"panel ({prices.shape[1]}): {', '.join(prices.columns)}  {len(prices)}d")
    if has_gold:
        c = prices.pct_change()
        gc = c[crypto_cols].mean(axis=1).corr(c[GOLD])
        print(f"gold(PAXG) vs crypto-basket daily-return correlation = {gc:.2f}")
    print()

    n_c = len(crypto_cols)
    ew_crypto = pd.Series(0.0, index=prices.columns)
    ew_crypto[crypto_cols] = 1.0 / n_c

    print("A) CRYPTO-ONLY (equal-weight)")
    report("crypto buyhold", buyhold(prices, ew_crypto))
    report("crypto rebal monthly", rebal_calendar(prices, ew_crypto, 21))
    report("crypto rebal threshold 25%", rebal_threshold(prices, ew_crypto, 0.25))

    if has_gold:
        # crypto+gold: give gold a full equal sleeve (1/(n_c+1) each)
        ewg = pd.Series(1.0 / (n_c + 1), index=prices.columns)
        print("\nB) CRYPTO + GOLD (equal-weight incl. PAXG sleeve)")
        report("crypto+gold buyhold", buyhold(prices, ewg))
        report("crypto+gold rebal monthly", rebal_calendar(prices, ewg, 21))
        report("crypto+gold rebal threshold 25%", rebal_threshold(prices, ewg, 0.25))

        # heavier gold / cash defensive mixes
        print("\nC) DEFENSIVE MIXES (rebalanced monthly)")
        for gw, cashw, lab in [(0.30, 0.0, "40%crypto/30%gold/30%cash approx"),
                               (0.25, 0.25, "50%crypto/25%gold/25%cash")]:
            tgt = pd.Series(0.0, index=prices.columns)
            cw = (1.0 - gw - cashw) / n_c
            tgt[crypto_cols] = cw
            tgt[GOLD] = gw
            report(f"{lab}", rebal_calendar(prices, tgt, 21))

    print("\nNOTE: the honest comparison is same-target buyhold vs rebalanced, and whether")
    print("adding gold RAISES Sharpe / CUTS drawdown vs crypto-only. Long-only, spot today.")


if __name__ == "__main__":
    main()

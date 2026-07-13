#!/usr/bin/env python3
"""
Cross-sectional / DUAL momentum research — a structurally NEW candidate.

Everything tested before is either time-series timing (is THIS coin above its SMA),
funding arb, or pairs. This tests a different axis: RELATIVE strength across a broad
universe (which coins are strongest), long-only, monthly rebalance — low turnover so
it can clear the ~0.3-0.5% cost wall a US Kraken-spot account pays.

Two forms, both long-only spot-executable TODAY (no shorting / leverage / perps):
  * RELATIVE momentum: each month rank the liquid universe by trailing return,
    hold the top-N equal-weight.
  * DUAL momentum (Antonacci): same ranking, BUT a coin is only held if its own
    trailing return also beats CASH (absolute filter) — otherwise that slot goes to
    cash. This is the piece that dodges the short-beta / "least-bad loser" trap: in a
    broad bear it sits in cash instead of holding the best of a bad lot.

Cost model: Kraken spot taker 0.26%/side, charged on the TURNOVER at each rebalance
(sell exited names + buy new names). Monthly cadence keeps turnover — and cost — low.

Benchmarks: equal-weight buy&hold of the universe, and BTC buy&hold.
Honest: 720d daily (2yr, one macro cycle-ish), OOS = last 40%. Screen, not proof.

    python scripts/xsec_momentum_research.py
"""
from __future__ import annotations

import sys
import numpy as np
import pandas as pd
import ccxt

TOP_LIQUID = 30          # universe = top-N USD spot by 24h quote volume
BARS = 720
TAKER = 0.0026           # Kraken spot taker per side
REBALANCE_D = 21         # ~monthly
SKIP_D = 5               # skip most-recent week (avoid short-term reversal) in the lookback
OOS_FRAC = 0.40
STABLES = {"USDT", "USDC", "DAI", "USD", "EUR", "GBP", "PYUSD", "USDG", "EURT",
           "TUSD", "USTS", "USDR", "GUSD", "GHO", "GBPT", "AUD", "CAD", "CHF", "JPY"}


def liquid_universe(ex, n=TOP_LIQUID) -> list[str]:
    ex.load_markets()
    tick = ex.fetch_tickers()
    rows = []
    for sym, t in tick.items():
        m = ex.markets.get(sym, {})
        if not (m.get("quote") == "USD" and m.get("spot") and m.get("active")):
            continue
        if m.get("base") in STABLES:
            continue
        qv = t.get("quoteVolume") or 0
        if qv:
            rows.append((m["base"], sym, qv))
    rows.sort(key=lambda r: r[2], reverse=True)
    return rows[:n]


def fetch_close(ex, sym) -> pd.Series | None:
    try:
        o = ex.fetch_ohlcv(sym, timeframe="1d", limit=BARS)
    except Exception:
        return None
    if not o:
        return None
    idx = pd.to_datetime([r[0] for r in o], unit="ms")
    return pd.Series([r[4] for r in o], index=idx)


def build_panel(ex, universe) -> pd.DataFrame:
    cols = {}
    for base, sym, _ in universe:
        s = fetch_close(ex, sym)
        if s is not None and len(s) > 200:
            cols[base] = s
    df = pd.DataFrame(cols).sort_index()
    return df


def backtest(prices: pd.DataFrame, lookback: int, hold_n: int, dual: bool) -> pd.Series:
    """Returns a daily equity curve (start 1.0). Long-only, equal-weight top-N,
    monthly rebalance, turnover-costed. dual=True adds the absolute (>cash) filter."""
    rets = prices.pct_change().fillna(0.0)
    dates = prices.index
    equity = pd.Series(1.0, index=dates)
    weights = pd.Series(0.0, index=prices.columns)  # current holdings (fraction of book)
    eq = 1.0
    start_i = lookback + SKIP_D + 1

    for i in range(start_i, len(dates)):
        # apply today's return on yesterday's weights
        day_ret = float((weights * rets.iloc[i]).sum())
        eq *= (1.0 + day_ret)

        # rebalance on cadence
        if (i - start_i) % REBALANCE_D == 0:
            past = prices.iloc[i - lookback - SKIP_D]
            recent = prices.iloc[i - SKIP_D]
            mom = (recent / past - 1.0).dropna()
            mom = mom[np.isfinite(mom)]
            ranked = mom.sort_values(ascending=False)
            picks = list(ranked.index[:hold_n])
            if dual:
                picks = [p for p in picks if ranked[p] > 0.0]  # must beat cash
            new_w = pd.Series(0.0, index=prices.columns)
            if picks:
                for p in picks:
                    new_w[p] = 1.0 / hold_n   # equal-weight the SLOTS; empty slots = cash
            # turnover cost
            turnover = float((new_w - weights).abs().sum())
            eq *= (1.0 - turnover * TAKER)
            weights = new_w
        equity.iloc[i] = eq
    return equity.iloc[start_i:]


def curve_stats(eq: pd.Series) -> dict:
    if len(eq) < 5:
        return {"ret": 0, "cagr": 0, "sharpe": 0, "maxdd": 0}
    total = eq.iloc[-1] / eq.iloc[0] - 1
    days = (eq.index[-1] - eq.index[0]).days or 1
    cagr = (eq.iloc[-1] / eq.iloc[0]) ** (365.0 / days) - 1
    dr = eq.pct_change().dropna()
    sharpe = (dr.mean() / dr.std() * np.sqrt(365)) if dr.std() > 0 else 0
    dd = float((eq / eq.cummax() - 1).min())
    return {"ret": total, "cagr": cagr, "sharpe": sharpe, "maxdd": dd}


def bh_equalweight(prices: pd.DataFrame) -> pd.Series:
    rets = prices.pct_change().fillna(0.0)
    ew = rets.mean(axis=1)  # daily equal-weight return
    return (1 + ew).cumprod()


def split_report(name: str, eq: pd.Series):
    n = len(eq)
    oos_i = int(n * (1 - OOS_FRAC))
    full = curve_stats(eq)
    ins = curve_stats(eq.iloc[:oos_i])
    oos = curve_stats(eq.iloc[oos_i:] / eq.iloc[oos_i])
    print(f"{name:<26} FULL ret={full['ret']*100:>7.1f}% cagr={full['cagr']*100:>6.1f}% "
          f"sh={full['sharpe']:>5.2f} dd={full['maxdd']*100:>6.1f}%  |  "
          f"OOS ret={oos['ret']*100:>7.1f}% sh={oos['sharpe']:>5.2f} (IS ret={ins['ret']*100:>6.1f}%)")


def main():
    ex = ccxt.kraken({"enableRateLimit": True})
    print(f"Selecting top {TOP_LIQUID} liquid USD spot pairs by 24h volume ...")
    universe = liquid_universe(ex)
    print("universe:", ", ".join(b for b, _, _ in universe))
    print("Fetching daily history ...")
    prices = build_panel(ex, universe)
    prices = prices.dropna(axis=1, thresh=int(len(prices) * 0.8))  # need decent history
    print(f"panel: {prices.shape[1]} coins x {prices.shape[0]} days "
          f"({prices.index[0].date()} -> {prices.index[-1].date()})\n")

    print("BENCHMARKS")
    ew = bh_equalweight(prices)
    split_report("buyhold_equalweight", ew)
    if "BTC" in prices.columns:
        split_report("buyhold_BTC", (1 + prices["BTC"].pct_change().fillna(0)).cumprod())
    print()

    print("CROSS-SECTIONAL MOMENTUM (long-only, monthly rebalance, 0.26% taker turnover)")
    print("relative = hold top-N by trailing return | dual = same + must beat cash (else slot->cash)\n")
    grid = [(lb, hn) for lb in (60, 90, 120) for hn in (3, 5)]
    for dual in (False, True):
        tag = "DUAL " if dual else "REL  "
        for lb, hn in grid:
            eq = backtest(prices, lookback=lb, hold_n=hn, dual=dual)
            split_report(f"{tag}lb={lb} topN={hn}", eq)
        print()

    print("NOTE: 2yr screen, one macro regime, monthly rebalance => ~24 rebalances.")
    print("Spot-executable on Kraken TODAY (long-only, no leverage). A winner earns a")
    print("forward paper test on a separate ledger before any real funding.")


if __name__ == "__main__":
    main()

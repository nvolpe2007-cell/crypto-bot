#!/usr/bin/env python3
"""
LONG/SHORT momentum research — testing the one hypothesis every backtest in this
project points to: you can only profit in a down market by SHORTING.

Long-only dual momentum (xsec_momentum_verdict) protects capital by sitting in cash in
bears but can't PROFIT there. This adds the short side. Perp/paper only (US Kraken-spot
can't short with real funds yet — Kraken US perps / Bitnomial not integrated; same
status as the lev_perp arm).

Variants (all rebalanced monthly, perp-costed):
  * XS_MKT_NEUTRAL : long top-N by trailing return, SHORT bottom-N. Dollar-neutral —
    pure cross-sectional spread, ~zero market beta.
  * TS_LONGSHORT   : per coin, LONG if its own trailing return > 0 else SHORT, equal
    weight. Net-directional — shorts the WHOLE basket in a broad bear (the "dual
    momentum but short instead of cash" the session pointed at).
  * TS_LS_TRENDCONF: same as TS but a coin only takes a side if trailing return AND
    price-vs-SMA50 agree (else that sleeve is flat) — the confirmation filter.

Costs (perp, cheaper than spot): TAKER 0.06%/side on turnover + FUNDING_APY drag on
GROSS exposure per day. Long & short leg P&L are attributed separately (honesty: the
long-the-strongest leg is known to lose — see xsec_momentum_verdict — so any edge must
come from the short leg).

Benchmarks: equal-weight buy&hold, and long-only dual momentum (best cash-filter cell).
Honest: 720d daily (2yr), OOS = last 40%. Screen, not proof. Short candidates limited
to the top-liquid set so the shorts are at least plausibly borrowable/perp-listed.

    python scripts/ls_momentum_research.py
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import ccxt

TOP_LIQUID = 30
BARS = 720
TAKER = 0.0006           # perp taker per side
FUNDING_APY = 0.15       # conservative funding/borrow drag on gross exposure
REBALANCE_D = 21
SKIP_D = 5
OOS_FRAC = 0.40
STABLES = {"USDT", "USDC", "DAI", "USD", "EUR", "GBP", "PYUSD", "USDG", "EURT",
           "TUSD", "USTS", "USDR", "GUSD", "GHO", "GBPT", "AUD", "CAD", "CHF", "JPY"}


def liquid_universe(ex, n=TOP_LIQUID):
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


def fetch_close(ex, sym):
    try:
        o = ex.fetch_ohlcv(sym, timeframe="1d", limit=BARS)
    except Exception:
        return None
    if not o:
        return None
    return pd.Series([r[4] for r in o], index=pd.to_datetime([r[0] for r in o], unit="ms"))


def build_panel(ex, universe):
    cols = {}
    for base, sym, _ in universe:
        s = fetch_close(ex, sym)
        if s is not None and len(s) > 200:
            cols[base] = s
    return pd.DataFrame(cols).sort_index()


def _sma(col: pd.Series, i: int, n: int):
    if i < n:
        return None
    return col.iloc[i - n:i].mean()


def backtest(prices: pd.DataFrame, lookback: int, hold_n: int, mode: str):
    """Returns (equity, long_leg_pnl$, short_leg_pnl$) as fraction-of-book.
    mode in {xs_neutral, ts_ls, ts_ls_trend}."""
    rets = prices.pct_change().fillna(0.0)
    dates = prices.index
    equity = pd.Series(1.0, index=dates)
    w = pd.Series(0.0, index=prices.columns)   # signed weights (fraction of book)
    eq = 1.0
    long_pnl = short_pnl = 0.0
    start_i = max(lookback + SKIP_D + 1, 55)

    for i in range(start_i, len(dates)):
        r = rets.iloc[i]
        long_pnl += eq * float((w[w > 0] * r[w > 0]).sum())
        short_pnl += eq * float((w[w < 0] * r[w < 0]).sum())
        day_ret = float((w * r).sum())
        # funding/borrow drag on gross exposure
        gross = float(w.abs().sum())
        day_ret -= gross * FUNDING_APY / 365.0
        eq *= (1.0 + day_ret)

        if (i - start_i) % REBALANCE_D == 0:
            past = prices.iloc[i - lookback - SKIP_D]
            recent = prices.iloc[i - SKIP_D]
            mom = (recent / past - 1.0).replace([np.inf, -np.inf], np.nan).dropna()
            ranked = mom.sort_values(ascending=False)
            new_w = pd.Series(0.0, index=prices.columns)

            if mode == "xs_neutral":
                longs = list(ranked.index[:hold_n])
                shorts = list(ranked.index[-hold_n:])
                for p in longs:
                    new_w[p] = 0.5 / hold_n
                for p in shorts:
                    new_w[p] = -0.5 / hold_n
            elif mode in ("ts_ls", "ts_ls_trend"):
                names = list(mom.index)
                for p in names:
                    side = 1 if mom[p] > 0 else -1
                    if mode == "ts_ls_trend":
                        col = prices[p]
                        s = _sma(col, i, 50)
                        if s is None:
                            continue
                        trend = 1 if col.iloc[i] >= s else -1
                        if trend != side:
                            continue  # disagreement -> flat this sleeve
                    new_w[p] = side / max(1, len(names))
            turnover = float((new_w - w).abs().sum())
            eq *= (1.0 - turnover * TAKER)
            w = new_w
        equity.iloc[i] = eq
    return equity.iloc[start_i:], long_pnl, short_pnl


def stats(eq):
    if len(eq) < 5:
        return {"ret": 0, "sharpe": 0, "maxdd": 0}
    dr = eq.pct_change().dropna()
    return {"ret": eq.iloc[-1] / eq.iloc[0] - 1,
            "sharpe": (dr.mean() / dr.std() * np.sqrt(365)) if dr.std() > 0 else 0,
            "maxdd": float((eq / eq.cummax() - 1).min())}


def report(name, eq, lp=None, sp=None):
    n = len(eq)
    oos_i = int(n * (1 - OOS_FRAC))
    f, ins, oos = stats(eq), stats(eq.iloc[:oos_i]), stats(eq.iloc[oos_i:] / eq.iloc[oos_i])
    leg = f"  [long {lp*100:+.0f}% short {sp*100:+.0f}%]" if lp is not None else ""
    print(f"{name:<24} FULL ret={f['ret']*100:>7.1f}% sh={f['sharpe']:>5.2f} dd={f['maxdd']*100:>6.1f}%"
          f"  |  OOS ret={oos['ret']*100:>7.1f}% sh={oos['sharpe']:>5.2f} (IS={ins['ret']*100:>6.1f}%){leg}")


def main():
    ex = ccxt.kraken({"enableRateLimit": True})
    print(f"Top {TOP_LIQUID} liquid USD spot by volume ...")
    universe = liquid_universe(ex)
    prices = build_panel(ex, universe)
    prices = prices.dropna(axis=1, thresh=int(len(prices) * 0.8))
    print("universe:", ", ".join(prices.columns))
    print(f"panel {prices.shape[1]} coins x {prices.shape[0]}d "
          f"({prices.index[0].date()}->{prices.index[-1].date()})\n")

    ew = (1 + prices.pct_change().fillna(0).mean(axis=1)).cumprod()
    print("BENCHMARK")
    report("buyhold_equalweight", ew)
    print()

    print("LONG/SHORT MOMENTUM (perp, monthly rebalance, 0.06% taker + 15%APY funding drag)")
    print("leg attribution shows where P&L comes from; long-strongest leg is a known loser\n")
    for lb in (60, 90):
        for hn in (3, 5):
            eq, lp, sp = backtest(prices, lb, hn, "xs_neutral")
            report(f"XS_NEUTRAL lb={lb} N={hn}", eq, lp, sp)
    print()
    for lb in (60, 90, 120):
        eq, lp, sp = backtest(prices, lb, 0, "ts_ls")
        report(f"TS_LONGSHORT lb={lb}", eq, lp, sp)
    print()
    for lb in (60, 90, 120):
        eq, lp, sp = backtest(prices, lb, 0, "ts_ls_trend")
        report(f"TS_LS_TRENDCONF lb={lb}", eq, lp, sp)

    print("\nNOTE: perp/paper only (short side not real-money executable on Kraken-spot yet).")
    print("Shorts limited to top-liquid names. 2yr screen; winner -> forward paper proof.")


if __name__ == "__main__":
    main()

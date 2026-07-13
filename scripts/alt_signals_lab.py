#!/usr/bin/env python3
"""
ALT-SIGNALS LAB — new DIMENSIONS beyond price-trend: sentiment, calendar, lead-lag, vol.

Directional price signals are exhausted (strategy_lab: 0/118). This attacks axes never
searched here, each a different DATA SOURCE or STRUCTURE that could hold real edge:

  * FEAR & GREED contrarian (alternative.me daily index, real behavioral data): hold when
    the crowd is fearful, step aside when greedy. The most-cited crypto sentiment signal.
  * CALENDAR/SEASONALITY: day-of-week and turn-of-month effects (a temporal axis, not a
    price signal) — documented in equities & crypto.
  * LEAD-LAG: does BTC's move yesterday predict the alt basket today? Cross-asset info flow.
  * VOL-REGIME: hold only when realized volatility is low (vol is far more predictable than
    returns — the most robust fact in finance); rotate to cash/gold when vol spikes.

All long-only, spot-executable on a $1,000 book (0.26% taker). Same gauntlet as
strategy_lab: profitable full + OOS + Monte-Carlo P(profit)>=95% + Sharpe > expected-max
of N trials (best-of-N luck bar). Honest: 2yr screen.

    python scripts/alt_signals_lab.py
"""
from __future__ import annotations

import math
import json
import urllib.request
import numpy as np
import pandas as pd
import ccxt

BASKET = ["BTC", "ETH", "SOL", "XRP", "ADA", "LINK", "LTC", "BCH", "AVAX", "DOGE"]
GOLD = "PAXG"
BARS = 720
TAKER = 0.0026
BOOK = 1000.0
OOS_FRAC = 0.40
MC_RESAMPLES, MC_BLOCK = 2000, 10
rng = np.random.default_rng(11)


def fetch_close(ex, base):
    try:
        o = ex.fetch_ohlcv(f"{base}/USD", timeframe="1d", limit=BARS)
    except Exception:
        return None
    return pd.Series([r[4] for r in o], index=pd.to_datetime([r[0] for r in o], unit="ms")) if o else None


def build_panel(ex):
    cols = {}
    for b in BASKET + [GOLD]:
        s = fetch_close(ex, b)
        if s is not None and len(s) > 500:
            cols[b] = s
    return pd.DataFrame(cols).sort_index().dropna()


def fetch_fng():
    with urllib.request.urlopen("https://api.alternative.me/fng/?limit=0&format=json", timeout=30) as r:
        d = json.loads(r.read())
    idx = pd.to_datetime([int(x["timestamp"]) for x in d["data"]], unit="s")
    return pd.Series([int(x["value"]) for x in d["data"]], index=idx).sort_index()


def backtest(prices, W):
    rets = prices.pct_change().fillna(0.0)
    gross = (W.shift(1) * rets).sum(axis=1)
    turn = (W - W.shift(1)).abs().sum(axis=1).fillna(0.0)
    net = gross - turn * TAKER
    eq = BOOK * (1 + net).cumprod()
    return eq, net


def metrics(eq, net, split_i):
    tot = eq.iloc[-1] / BOOK - 1
    dr = net.dropna()
    sh = (dr.mean() / dr.std() * math.sqrt(365)) if dr.std() > 0 else 0.0
    dd = float((eq / eq.cummax() - 1).min())
    oos = eq.iloc[split_i:].iloc[-1] / eq.iloc[split_i] - 1
    return tot, sh, dd, oos


def mc_prob(net):
    a = net.dropna().values
    L = len(a)
    if L < 30:
        return 0.0
    nb = math.ceil(L / MC_BLOCK)
    fin = np.empty(MC_RESAMPLES)
    for k in range(MC_RESAMPLES):
        st = rng.integers(0, L - MC_BLOCK + 1, size=nb)
        fin[k] = np.prod(1 + np.concatenate([a[s:s + MC_BLOCK] for s in st])[:L]) - 1
    return float((fin > 0).mean())


def _z(p):
    import scipy.stats as st
    return float(st.norm.ppf(p))


def expected_max_sharpe(n, years):
    n = max(n, 2)
    e = (1 - np.euler_gamma) * _z(1 - 1.0 / n) + np.euler_gamma * _z(1 - 1.0 / (n * math.e))
    return e / math.sqrt(years)


def gen(prices, fng):
    crypto = [c for c in prices.columns if c != GOLD]
    idx, cols = prices.index, prices.columns
    ew_row = pd.Series(0.0, index=cols); ew_row[crypto] = 1.0 / len(crypto)
    out = {}

    def hold_when(mask, to_gold=False):
        """mask: bool Series over idx -> hold equal-weight basket that day else cash/gold."""
        W = pd.DataFrame(0.0, index=idx, columns=cols)
        W.loc[mask.values] = ew_row.values
        if to_gold and GOLD in cols:
            W.loc[~mask.values, GOLD] = 1.0
        return W

    def add(name, W): out[name] = W

    # benchmark
    add("BENCH_basket_buyhold", pd.DataFrame(np.tile(ew_row.values, (len(idx), 1)), index=idx, columns=cols))

    # 1) Fear & Greed contrarian
    f = fng.reindex(idx).ffill()
    for entry, exit_ in ((25, 55), (30, 60), (20, 70), (35, 65), (40, 75)):
        state = pd.Series(False, index=idx); cur = False
        for i in range(len(idx)):
            v = f.iloc[i]
            if not cur and v <= entry: cur = True
            elif cur and v >= exit_: cur = False
            state.iloc[i] = cur
        add(f"FNG_contra_{entry}_{exit_}", hold_when(state))
        add(f"FNG_contra_{entry}_{exit_}_goldelse", hold_when(state, to_gold=True))
    add("FNG_below_median44", hold_when(f < 44))
    add("FNG_below_median44_goldelse", hold_when(f < 44, to_gold=True))

    # 2) calendar: single day-of-week held
    dow = pd.Series(idx.dayofweek, index=idx)
    for d in range(7):
        add(f"CAL_dow{d}_only", hold_when(dow == d))
    add("CAL_weekend_frisun", hold_when(dow.isin([4, 5, 6])))
    add("CAL_weekdays", hold_when(dow.isin([0, 1, 2, 3, 4])))
    # turn-of-month: last 2 + first 3 calendar days
    dom = pd.Series(idx.day, index=idx)
    is_month_end = pd.Series(idx.is_month_end, index=idx)
    tom = (dom <= 3) | (dom >= 27)
    add("CAL_turn_of_month", hold_when(tom))

    # 3) lead-lag: BTC yesterday's sign drives basket today
    btc_ret = prices["BTC"].pct_change()
    add("LL_btc_up_yesterday", hold_when((btc_ret.shift(1) > 0).fillna(False)))
    add("LL_btc_up_yesterday_goldelse", hold_when((btc_ret.shift(1) > 0).fillna(False), to_gold=True))
    add("LL_btc_down_yesterday", hold_when((btc_ret.shift(1) < 0).fillna(False)))  # reversal
    # BTC 3-day momentum gate
    add("LL_btc_3d_up", hold_when((prices["BTC"] > prices["BTC"].shift(3)).fillna(False)))

    # 4) vol-regime: hold when basket realized vol below its rolling median
    bret = prices[crypto].pct_change().mean(axis=1)
    rv = bret.rolling(20).std()
    med = rv.rolling(60).median()
    add("VOL_low_below_median", hold_when((rv < med).fillna(False)))
    add("VOL_low_below_median_goldelse", hold_when((rv < med).fillna(False), to_gold=True))
    for thr in (0.02, 0.03):
        add(f"VOL_below_{thr}", hold_when((rv < thr).fillna(False)))
    return out


def main():
    ex = ccxt.kraken({"enableRateLimit": True})
    print("Fetching prices + Fear&Greed ...")
    prices = build_panel(ex)
    fng = fetch_fng()
    n = len(prices)
    years = (prices.index[-1] - prices.index[0]).days / 365.0
    split_i = int(n * (1 - OOS_FRAC))
    print(f"{prices.shape[1]} assets x {n}d, {years:.2f}yr; OOS=last {n-split_i}. "
          f"F&G merged ({fng.reindex(prices.index).notna().sum()} days).\n")

    strats = gen(prices, fng)
    emax = expected_max_sharpe(len(strats), years)
    print(f"{len(strats)} configs. Multiple-testing bar: expected max Sharpe = {emax:.2f}\n")

    rows = []
    for name, W in strats.items():
        eq, net = backtest(prices, W)
        tot, sh, dd, oos = metrics(eq, net, split_i)
        rows.append((name, eq.iloc[-1], tot, oos, sh, dd, net))
    rows.sort(key=lambda r: r[4], reverse=True)

    print("=" * 100)
    print(f"{'strategy':<34} {'final$':>8} {'ret%':>7} {'OOS%':>7} {'Sharpe':>7} {'maxDD%':>7} {'MC P':>6}")
    print("-" * 100)
    survivors = []
    for name, fin, tot, oos, sh, dd, net in rows:
        cand = (tot > 0 and oos > 0 and not name.startswith("BENCH"))
        pp = mc_prob(net) if cand else 0.0
        passes = cand and pp >= 0.95 and sh > emax
        tag = "  <== PASSES" if passes else ""
        mark = f"{pp*100:>5.0f}%" if cand else "   -"
        print(f"{name:<34} {fin:>8.0f} {tot*100:>6.1f}% {oos*100:>6.1f}% {sh:>7.2f} {dd*100:>6.1f}% {mark}{tag}")
        if passes:
            survivors.append((name, tot, oos, sh, pp))

    print("=" * 100)
    if survivors:
        print(f"\nSURVIVORS ({len(survivors)}):")
        for name, tot, oos, sh, pp in survivors:
            print(f"  * {name}: ${BOOK*(1+tot):.0f} ({tot*100:+.1f}%), OOS {oos*100:+.1f}%, Sharpe {sh:.2f}, MC {pp*100:.0f}%")
    else:
        print("\nSURVIVORS: NONE cleared profitable + OOS + Monte-Carlo + best-of-N luck bar.")
    print("\nCaveat: 2yr screen; a survivor earns a forward paper test, not real money.")


if __name__ == "__main__":
    main()

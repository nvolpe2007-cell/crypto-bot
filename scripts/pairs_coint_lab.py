#!/usr/bin/env python3
"""
COINTEGRATION PAIRS LAB — market-neutral spread mean-reversion (a NON-directional bet).

Every strategy tested so far carries market beta, and beta is what kills them (crypto
falls, long-only can't profit, shorting gets squeezed). A cointegrated PAIR is different:
you long one leg / short the other, so the market direction cancels and you harvest the
mean-reversion of the SPREAD. If a real edge exists on this account, non-directional
stat-arb is the most likely place — it's the classic hedge-fund play and has NOT been
rigorously searched here.

Method (Engle-Granger, implemented on numpy/scipy — statsmodels absent):
  1. For every pair, fit hedge ratio beta by OLS on the IN-SAMPLE window only.
  2. spread = logA - beta*logB. ADF test (manual) on the in-sample spread -> is it
     stationary/cointegrated? Require ADF t < -3.0 (stricter than the -2.86 5% level,
     because we test ~171 pairs = heavy multiple-testing).
  3. Half-life of mean reversion from an OU fit; keep only tradeable pairs (2-60 days).
  4. Backtest a z-score reversion strategy on a $1000 dollar-neutral book: enter when
     |z|>ENTRY, exit when |z|<EXIT, hard stop |z|>STOP. z uses a rolling mean/std (no
     lookahead). Cost: perp taker 0.075%/side on BOTH legs each turn + funding drag.

GAUNTLET (same as strategy_lab): profitable full + OOS + Monte-Carlo P(profit)>=95% +
Sharpe > expected-max-Sharpe of N pairs tried (best-of-N luck bar). With ~171 pairs the
luck bar is high on purpose.

PAPER/PERP: shorting a leg is not real-money executable on US Kraken-spot today (needs
Kraken US perps, same status as the lev_perp arm). Flagged, not hidden.

    python scripts/pairs_coint_lab.py
"""
from __future__ import annotations

import math
import itertools
import numpy as np
import pandas as pd
import ccxt

UNIVERSE = ["BTC", "ETH", "SOL", "XRP", "ADA", "LINK", "LTC", "BCH", "AVAX", "DOGE",
            "DOT", "UNI", "ATOM", "XLM", "ETC", "AAVE", "NEAR", "ALGO", "FIL"]
BARS = 720
LEG_COST = 0.00075        # perp taker per side per leg
FUNDING_APY = 0.15
BOOK = 1000.0
OOS_FRAC = 0.40
Z_ENTRY, Z_EXIT, Z_STOP = 2.0, 0.5, 4.0
Z_WIN = 30                # rolling window for z-score
ADF_MAX_T = -3.0          # cointegration threshold (stricter than -2.86)
MC_RESAMPLES, MC_BLOCK = 1500, 10
rng = np.random.default_rng(7)


def fetch_close(ex, base):
    try:
        o = ex.fetch_ohlcv(f"{base}/USD", timeframe="1d", limit=BARS)
    except Exception:
        return None
    return pd.Series([r[4] for r in o], index=pd.to_datetime([r[0] for r in o], unit="ms")) if o else None


def build_panel(ex):
    cols = {}
    for b in UNIVERSE:
        s = fetch_close(ex, b)
        if s is not None and len(s) > 500:
            cols[b] = s
    return pd.DataFrame(cols).sort_index().dropna()


def adf_tstat(y: np.ndarray, lags=1) -> float:
    """Manual Augmented Dickey-Fuller t-stat on the lagged-level coefficient."""
    y = np.asarray(y, float)
    dy = np.diff(y)
    n = len(dy)
    if n < lags + 10:
        return 0.0
    X = [y[lags:-1] if False else y[lags:n]]  # level lag y_{t-1}
    ylag = y[lags:n]
    rows = n - lags
    Z = [np.ones(rows), ylag]
    for i in range(1, lags + 1):
        Z.append(dy[lags - i:n - i])
    Z = np.column_stack(Z)
    target = dy[lags:n]
    beta, *_ = np.linalg.lstsq(Z, target, rcond=None)
    resid = target - Z @ beta
    dof = rows - Z.shape[1]
    if dof <= 0:
        return 0.0
    s2 = (resid @ resid) / dof
    xtx_inv = np.linalg.pinv(Z.T @ Z)
    se_gamma = math.sqrt(max(s2 * xtx_inv[1, 1], 1e-18))
    return beta[1] / se_gamma       # t-stat on gamma (level lag); very negative = stationary


def half_life(spread: np.ndarray) -> float:
    s = np.asarray(spread, float)
    ds = np.diff(s)
    slag = s[:-1]
    A = np.column_stack([np.ones(len(slag)), slag])
    b, *_ = np.linalg.lstsq(A, ds, rcond=None)
    lam = b[1]
    if lam >= 0:
        return 1e9
    return -math.log(2) / lam


def backtest_pair(logA, logB, beta, split_i):
    spread = logA - beta * logB
    m = spread.rolling(Z_WIN).mean()
    sd = spread.rolling(Z_WIN).std()
    z = (spread - m) / sd
    pos = np.zeros(len(spread))       # +1 long spread (long A short B), -1 short spread
    cur = 0.0
    for i in range(len(z)):
        zi = z.iloc[i]
        if np.isnan(zi):
            pos[i] = cur; continue
        if cur == 0:
            if zi > Z_ENTRY: cur = -1
            elif zi < -Z_ENTRY: cur = 1
        else:
            if abs(zi) < Z_EXIT or abs(zi) > Z_STOP:
                cur = 0
        pos[i] = cur
    pos = pd.Series(pos, index=spread.index)
    # spread daily return approx = position * change in spread (log) ; dollar-neutral book
    dspread = spread.diff().fillna(0.0)
    gross = pos.shift(1).fillna(0.0) * dspread          # per $1 notional on spread
    turn = pos.diff().abs().fillna(0.0)
    cost = turn * (2 * LEG_COST)                        # both legs each turn
    funding = pos.abs().shift(1).fillna(0.0) * (FUNDING_APY / 365.0)
    net = gross - cost - funding
    eq = BOOK * (1 + net).cumprod()
    return eq, net, pos


def mc_prob(net):
    a = net.dropna().values
    L = len(a)
    if L < 30:
        return 0.0
    nb = math.ceil(L / MC_BLOCK)
    fin = np.empty(MC_RESAMPLES)
    for k in range(MC_RESAMPLES):
        st = rng.integers(0, L - MC_BLOCK + 1, size=nb)
        samp = np.concatenate([a[s:s + MC_BLOCK] for s in st])[:L]
        fin[k] = np.prod(1 + samp) - 1
    return float((fin > 0).mean())


def _z(p):
    import scipy.stats as st
    return float(st.norm.ppf(p))


def expected_max_sharpe(n, years):
    n = max(n, 2)
    e = (1 - np.euler_gamma) * _z(1 - 1.0 / n) + np.euler_gamma * _z(1 - 1.0 / (n * math.e))
    return e / math.sqrt(years)


def main():
    ex = ccxt.kraken({"enableRateLimit": True})
    print("Fetching universe ...")
    prices = build_panel(ex)
    logp = np.log(prices)
    n = len(prices)
    years = (prices.index[-1] - prices.index[0]).days / 365.0
    split_i = int(n * (1 - OOS_FRAC))
    print(f"{prices.shape[1]} assets x {n}d, {years:.2f}yr; IS=first {split_i}, OOS=last {n-split_i}\n")

    pairs = list(itertools.combinations(prices.columns, 2))
    print(f"Testing {len(pairs)} pairs for cointegration (ADF t < {ADF_MAX_T}, "
          f"half-life 2-60d) on IN-SAMPLE window ...\n")

    coint = []
    for a, b in pairs:
        la_is, lb_is = logp[a].iloc[:split_i].values, logp[b].iloc[:split_i].values
        # OLS hedge ratio on in-sample
        X = np.column_stack([np.ones(len(lb_is)), lb_is])
        beta_full, *_ = np.linalg.lstsq(X, la_is, rcond=None)
        beta = beta_full[1]
        if beta <= 0:
            continue
        spread_is = la_is - beta * lb_is
        t = adf_tstat(spread_is)
        hl = half_life(spread_is)
        if t < ADF_MAX_T and 2 <= hl <= 60:
            coint.append((a, b, beta, t, hl))

    coint.sort(key=lambda x: x[3])   # most negative ADF first
    print(f"{len(coint)} pairs are cointegrated in-sample. Backtesting through the gauntlet ...\n")

    emax = expected_max_sharpe(len(pairs), years)
    print(f"Multiple-testing bar: expected MAX Sharpe of {len(pairs)} pair-trials = {emax:.2f}\n")

    print("=" * 104)
    print(f"{'pair':<14} {'beta':>6} {'ADF_t':>7} {'HLd':>5} {'final$':>8} {'ret%':>7} "
          f"{'OOS%':>7} {'Sharpe':>7} {'maxDD%':>7} {'MC P':>6}")
    print("-" * 104)
    survivors = []
    for a, b, beta, t, hl in coint:
        eq, net, pos = backtest_pair(logp[a], logp[b], beta, split_i)
        tot = eq.iloc[-1] / BOOK - 1
        dr = net.dropna()
        sh = (dr.mean() / dr.std() * math.sqrt(365)) if dr.std() > 0 else 0.0
        dd = float((eq / eq.cummax() - 1).min())
        oos = eq.iloc[split_i:].iloc[-1] / eq.iloc[split_i] - 1
        p_prof = mc_prob(net) if (tot > 0 and oos > 0) else 0.0
        passes = tot > 0 and oos > 0 and p_prof >= 0.95 and sh > emax
        tag = "  <== PASSES" if passes else ""
        print(f"{a+'/'+b:<14} {beta:>6.2f} {t:>7.2f} {hl:>5.0f} {eq.iloc[-1]:>8.0f} "
              f"{tot*100:>6.1f}% {oos*100:>6.1f}% {sh:>7.2f} {dd*100:>6.1f}% {p_prof*100:>5.0f}%{tag}")
        if passes:
            survivors.append((a, b, tot, oos, sh, p_prof))

    print("=" * 104)
    if survivors:
        print(f"\nSURVIVORS ({len(survivors)}): market-neutral pairs profitable full+OOS+MC+luck-bar:")
        for a, b, tot, oos, sh, pp in survivors:
            print(f"  * {a}/{b}: ${BOOK*(1+tot):.0f} ({tot*100:+.1f}%), OOS {oos*100:+.1f}%, "
                  f"Sharpe {sh:.2f}, MC {pp*100:.0f}%")
    else:
        print("\nSURVIVORS: NONE cleared the full gauntlet.")
    print("\nPAPER/PERP: shorting a leg isn't real-money executable on US Kraken-spot yet.")
    print("Caveat: in-sample hedge ratio, 2yr screen. Survivor -> forward paper proof.")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
STRATEGY LAB — generate ~100 strategy configs, backtest on a $1,000 book with real
Kraken spot costs, and put survivors through a Monte-Carlo + multiple-testing gauntlet.

WHY THE GAUNTLET MATTERS: testing 100 strategies and reporting "the best" is how people
fool themselves — with 100 trials, ~5 clear a 95% bar by luck alone. So a config only
counts here if it clears ALL of:
  1. PROFITABLE   : net full-period return > 0 on $1,000 after 0.26% taker costs.
  2. OOS-ROBUST   : also profitable on the out-of-sample tail (last 40%, unseen).
  3. MONTE CARLO  : block-bootstrap of its daily returns is profitable in >=95% of
                    resamples (edge is robust to path, not one lucky ordering).
  4. NOT-LUCK     : its annualized Sharpe exceeds the EXPECTED MAX Sharpe under the null
                    across N trials (multiple-testing / "best of 100" correction).

Long-only, spot-executable on Kraken TODAY (no leverage/shorting) so "$1,000" is real.
720 daily bars (~2yr, one macro cycle). A survivor here has earned a forward paper test,
not a green light — a 2yr screen is still a screen.

    python scripts/strategy_lab.py
"""
from __future__ import annotations

import math
import numpy as np
import pandas as pd
import ccxt

UNIVERSE = ["BTC", "ETH", "SOL", "XRP", "ADA", "LINK", "LTC", "BCH", "AVAX", "DOGE",
            "DOT", "UNI", "ATOM", "XLM", "ETC", "AAVE", "NEAR", "ALGO", "FIL"]
GOLD = "PAXG"
BARS = 720
TAKER = 0.0026
BOOK = 1000.0
OOS_FRAC = 0.40
MC_RESAMPLES = 2000
MC_BLOCK = 10
rng = np.random.default_rng(42)


# ── data ────────────────────────────────────────────────────────────────────────
def fetch_close(ex, base):
    try:
        o = ex.fetch_ohlcv(f"{base}/USD", timeframe="1d", limit=BARS)
    except Exception:
        return None
    return pd.Series([r[4] for r in o], index=pd.to_datetime([r[0] for r in o], unit="ms")) if o else None


def build_panel(ex):
    cols = {}
    for b in UNIVERSE + [GOLD]:
        s = fetch_close(ex, b)
        if s is not None and len(s) > 500:
            cols[b] = s
    return pd.DataFrame(cols).sort_index().dropna()


# ── vectorized indicators ─────────────────────────────────────────────────────────
def sma(df, n): return df.rolling(n).mean()
def ema(df, n): return df.ewm(span=n, adjust=False).mean()
def roc(df, n): return df / df.shift(n) - 1
def rsi(df, n):
    d = df.diff()
    up = d.clip(lower=0).rolling(n).mean()
    dn = (-d.clip(upper=0)).rolling(n).mean()
    return (100 - 100 / (1 + up / dn.replace(0, np.nan))).fillna(50)


def _norm_long(mask: pd.DataFrame) -> pd.DataFrame:
    """Equal-weight the True cells each row; empty rows -> all cash (0)."""
    cnt = mask.sum(axis=1).replace(0, np.nan)
    w = mask.div(cnt, axis=0).fillna(0.0)
    return w


# ── strategy generators: each returns (name, weights_df) ──────────────────────────
def gen_strategies(prices: pd.DataFrame):
    crypto = [c for c in prices.columns if c != GOLD]
    P = prices[crypto]
    idx, cols = prices.index, prices.columns
    out = {}

    def add(name, w):
        out[name] = w.reindex(columns=cols).fillna(0.0)

    # 0) benchmarks
    add("BENCH_buyhold_BTC", pd.DataFrame({c: (1.0 if c == "BTC" else 0.0) for c in cols}, index=idx))
    ew = pd.DataFrame(0.0, index=idx, columns=cols); ew[crypto] = 1.0 / len(crypto)
    add("BENCH_buyhold_equalweight", ew)
    add("BENCH_cash", pd.DataFrame(0.0, index=idx, columns=cols))
    g6040 = pd.DataFrame(0.0, index=idx, columns=cols)
    g6040[crypto] = 0.6 / len(crypto); g6040[GOLD] = 0.4
    add("BENCH_60crypto_40gold", g6040)

    # 1) SMA trend long/cash
    for n in (20, 30, 50, 75, 100, 150, 200):
        add(f"TREND_sma{n}_longcash", _norm_long(P > sma(P, n)))
    # 2) EMA cross long/cash
    for f, s in ((10, 30), (20, 50), (12, 26), (50, 100)):
        add(f"TREND_emacross_{f}_{s}", _norm_long(ema(P, f) > ema(P, s)))
    # 3) tsmom long/cash
    for n in (20, 30, 50, 90, 120, 200):
        add(f"TREND_tsmom{n}_longcash", _norm_long(P > P.shift(n)))
    # 4) Donchian breakout long/cash
    for n in (20, 30, 50):
        add(f"TREND_donchian{n}", _norm_long(P >= P.rolling(n).max().shift(1)))
    # 5) MACD long/cash
    macd = ema(P, 12) - ema(P, 26)
    add("TREND_macd_signal", _norm_long(macd > macd.ewm(span=9, adjust=False).mean()))
    add("TREND_macd_zero", _norm_long(macd > 0))

    # 6) cross-sectional momentum top-N (rel + dual) monthly
    def xsec(lb, N, dual):
        r = roc(P, lb)
        W = pd.DataFrame(0.0, index=idx, columns=cols)
        cur = pd.Series(0.0, index=cols)
        start = lb + 1
        for i in range(start, len(idx)):
            if (i - start) % 21 == 0:
                row = r.iloc[i].dropna().sort_values(ascending=False)
                picks = list(row.index[:N])
                if dual:
                    picks = [p for p in picks if row[p] > 0]
                cur = pd.Series(0.0, index=cols)
                for p in picks:
                    cur[p] = 1.0 / N
            W.iloc[i] = cur
        return W
    for lb in (30, 60, 90, 120):
        for N in (3, 5, 8):
            add(f"XSEC_rel_lb{lb}_N{N}", xsec(lb, N, False))
            add(f"XSEC_dual_lb{lb}_N{N}", xsec(lb, N, True))

    # 7) mean-reversion RSI2 oversold-bounce
    def rsi2_bounce(entry, exit_):
        r = rsi(P, 2)
        inpos = r < entry
        hold = inpos.where(inpos).ffill(limit=5).fillna(False) & (r < exit_)
        return _norm_long(hold.astype(bool))
    for e, x in ((5, 60), (10, 60), (10, 70), (15, 70)):
        add(f"MR_rsi2_{e}_{x}", rsi2_bounce(e, x))
    # z-score reversal: buy when > Z below 20d mean
    for z in (1.5, 2.0):
        m, s = sma(P, 20), P.rolling(20).std()
        zscore = (P - m) / s
        add(f"MR_zscore_below{z}", _norm_long(zscore < -z))

    # 8) rebalanced allocations (weight-target constant -> daily reset = rebalanced)
    def rebal_target(target: pd.Series, cadence):
        # emulate periodic rebalancing by holding constant target on rebalance days,
        # letting weights drift between (approximate with drift via cumulative returns)
        rets = prices.pct_change().fillna(0.0)
        W = pd.DataFrame(0.0, index=idx, columns=cols)
        w = target.copy()
        for i in range(len(idx)):
            if i > 0:
                w = w * (1 + rets.iloc[i])
                tot = w.sum() + max(0.0, 1 - float(target.sum()))
                w = w / tot
            if i % cadence == 0:
                w = target.copy()
            W.iloc[i] = w
        return W
    ewt = pd.Series(0.0, index=cols); ewt[crypto] = 1.0 / len(crypto)
    for cad in (7, 21, 63):
        add(f"ALLOC_ew_rebal{cad}", rebal_target(ewt, cad))
    # crypto+gold+cash defensive mixes, monthly
    for gc, gg, lab in ((0.5, 0.25, "50c25g25cash"), (0.4, 0.3, "40c30g30cash"),
                        (0.6, 0.2, "60c20g20cash"), (0.34, 0.33, "34c33g33cash"),
                        (0.7, 0.3, "70c30g0cash"), (1.0, 0.0, "100c")):
        t = pd.Series(0.0, index=cols)
        t[crypto] = gc / len(crypto)
        if GOLD in cols:
            t[GOLD] = gg
        add(f"ALLOC_{lab}_rebal21", rebal_target(t, 21))
    # inverse-vol (risk-parity-ish) rebalanced monthly
    vol = prices.pct_change().rolling(30).std()
    invvol = (1.0 / vol[crypto]).replace([np.inf, -np.inf], np.nan)
    rp = invvol.div(invvol.sum(axis=1), axis=0).fillna(0.0)
    rp_month = rp.copy()
    rp_month[:] = rp.values
    # sample monthly
    Wrp = pd.DataFrame(0.0, index=idx, columns=cols)
    last = None
    for i in range(len(idx)):
        if i % 21 == 0 or last is None:
            last = rp.iloc[i].reindex(cols).fillna(0.0)
        Wrp.iloc[i] = last
    add("ALLOC_riskparity_rebal21", Wrp)

    # 9) dual momentum with GOLD fallback (trend on crypto, else rotate to gold/cash)
    def dualmom_gold(n):
        up = P > sma(P, n)
        W = _norm_long(up)
        # any book not deployed -> gold
        if GOLD in cols:
            deployed = W[crypto].sum(axis=1)
            W[GOLD] = (1.0 - deployed).clip(lower=0.0)
        return W
    for n in (50, 100, 150):
        add(f"DUAL_trend{n}_goldfallback", dualmom_gold(n))

    # 10) vol-targeted equal-weight trend (scale exposure by inverse realized vol)
    def voltarget_trend(n, target_vol=0.02):
        up = P > sma(P, n)
        base = _norm_long(up)
        rv = prices.pct_change().rolling(20).std().mean(axis=1)
        scale = (target_vol / rv).clip(0.2, 1.0)
        return base.mul(scale, axis=0).fillna(0.0)
    for n in (50, 100):
        add(f"VOLT_trend{n}_target2pct", voltarget_trend(n))

    # ── EXPANSION to exceed 100 configs ──────────────────────────────────────────
    for n in (10, 40, 60, 125, 250):
        add(f"TREND_sma{n}_longcash", _norm_long(P > sma(P, n)))
    for f, s in ((5, 20), (8, 21), (20, 100), (30, 60), (9, 50)):
        add(f"TREND_emacross_{f}_{s}", _norm_long(ema(P, f) > ema(P, s)))
    for n in (10, 15, 40, 60, 150, 250):
        add(f"TREND_tsmom{n}_longcash", _norm_long(P > P.shift(n)))
    for n in (10, 40, 60):
        add(f"TREND_donchian{n}", _norm_long(P >= P.rolling(n).max().shift(1)))
    # Bollinger / Keltner breakout (upper-band = trend continuation)
    for k in (1.5, 2.0):
        m, sd = sma(P, 20), P.rolling(20).std()
        add(f"BRK_bollinger_up{k}", _norm_long(P > m + k * sd))
    tr = (P.diff().abs()).rolling(20).mean()
    for k in (1.5, 2.0):
        add(f"BRK_keltner_up{k}", _norm_long(P > ema(P, 20) + k * tr))
    # extra cross-sectional lookbacks
    for lb in (20, 150, 180):
        for N in (3, 5):
            add(f"XSEC_rel_lb{lb}_N{N}", xsec(lb, N, False))
            add(f"XSEC_dual_lb{lb}_N{N}", xsec(lb, N, True))
    # mean-reversion: Bollinger lower-band buy
    for k in (1.5, 2.0, 2.5):
        m, sd = sma(P, 20), P.rolling(20).std()
        add(f"MR_bollinger_low{k}", _norm_long(P < m - k * sd))
    # extra allocation weights / cadences
    for gc, gg, lab in ((0.45, 0.35, "45c35g20cash"), (0.55, 0.15, "55c15g30cash"),
                        (0.8, 0.2, "80c20g0cash")):
        t = pd.Series(0.0, index=cols); t[crypto] = gc / len(crypto)
        if GOLD in cols:
            t[GOLD] = gg
        add(f"ALLOC_{lab}_rebal21", rebal_target(t, 21))
    for cad in (14, 42):
        add(f"ALLOC_ew_rebal{cad}", rebal_target(ewt, cad))
    # dual momentum gold-fallback extra + trend+RSI combo filters
    for n in (75, 125):
        add(f"DUAL_trend{n}_goldfallback", dualmom_gold(n))
    for n in (50, 100):
        combo = (P > sma(P, n)) & (rsi(P, 14) < 70)
        add(f"COMBO_trend{n}_rsi_notoverbought", _norm_long(combo))

    return out


# ── backtest engine ($1,000 book, taker cost on turnover) ─────────────────────────
def backtest(prices, W):
    rets = prices.pct_change().fillna(0.0)
    gross = (W.shift(1) * rets).sum(axis=1)
    turnover = (W - W.shift(1)).abs().sum(axis=1).fillna(0.0)
    net = gross - turnover * TAKER
    eq = BOOK * (1 + net).cumprod()
    return eq, net


def curve_metrics(eq, net):
    total = eq.iloc[-1] / BOOK - 1
    dr = net.dropna()
    sharpe = (dr.mean() / dr.std() * np.sqrt(365)) if dr.std() > 0 else 0.0
    maxdd = float((eq / eq.cummax() - 1).min())
    return total, sharpe, maxdd


# ── monte carlo (block bootstrap) + multiple-testing bar ──────────────────────────
def mc_prob_profit(net: pd.Series):
    a = net.dropna().values
    L = len(a)
    if L < 30:
        return 0.0, 0.0
    nb = math.ceil(L / MC_BLOCK)
    finals = np.empty(MC_RESAMPLES)
    for k in range(MC_RESAMPLES):
        starts = rng.integers(0, L - MC_BLOCK + 1, size=nb)
        samp = np.concatenate([a[s:s + MC_BLOCK] for s in starts])[:L]
        finals[k] = np.prod(1 + samp) - 1
    return float((finals > 0).mean()), float(np.percentile(finals, 5)) * 100


def expected_max_sharpe(n_trials, years):
    """Approx expected max annualized Sharpe of n_trials zero-edge strategies."""
    if n_trials < 2:
        n_trials = 2
    e_max_z = (1 - np.euler_gamma) * _z(1 - 1.0 / n_trials) + \
              np.euler_gamma * _z(1 - 1.0 / (n_trials * math.e))
    return e_max_z / math.sqrt(years)


def _z(p):
    # inverse normal cdf (Acklam approx)
    a = [-3.969683028665376e+01, 2.209460984245205e+02, -2.759285104469687e+02,
         1.383577518672690e+02, -3.066479806614716e+01, 2.506628277459239e+00]
    b = [-5.447609879822406e+01, 1.615858368580409e+02, -1.556989798598866e+02,
         6.680131188771972e+01, -1.328068155288572e+01]
    c = [-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e+00,
         -2.549732539343734e+00, 4.374664141464968e+00, 2.938163982698783e+00]
    d = [7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e+00,
         3.754408661907416e+00]
    pl = 0.02425
    if p < pl:
        q = math.sqrt(-2 * math.log(p))
        return (((((c[0]*q+c[1])*q+c[2])*q+c[3])*q+c[4])*q+c[5]) / ((((d[0]*q+d[1])*q+d[2])*q+d[3])*q+1)
    if p <= 1 - pl:
        q = p - 0.5; r = q*q
        return (((((a[0]*r+a[1])*r+a[2])*r+a[3])*r+a[4])*r+a[5])*q / (((((b[0]*r+b[1])*r+b[2])*r+b[3])*r+b[4])*r+1)
    q = math.sqrt(-2 * math.log(1 - p))
    return -(((((c[0]*q+c[1])*q+c[2])*q+c[3])*q+c[4])*q+c[5]) / ((((d[0]*q+d[1])*q+d[2])*q+d[3])*q+1)


def main():
    ex = ccxt.kraken({"enableRateLimit": True})
    print("Fetching universe ...")
    prices = build_panel(ex)
    n = len(prices)
    years = (prices.index[-1] - prices.index[0]).days / 365.0
    oos_i = int(n * (1 - OOS_FRAC))
    print(f"{prices.shape[1]} assets x {n} days ({prices.index[0].date()}->{prices.index[-1].date()}), "
          f"{years:.2f}yr; OOS = last {n-oos_i}d\n")

    strats = gen_strategies(prices)
    print(f"Generated {len(strats)} strategy configs. Backtesting on ${BOOK:.0f} book "
          f"@ {TAKER*100:.2f}% taker ...\n")

    rows = []
    for name, W in strats.items():
        eq, net = backtest(prices, W)
        tot, sh, dd = curve_metrics(eq, net)
        eq_oos = eq.iloc[oos_i:] / eq.iloc[oos_i]
        oos_tot = eq_oos.iloc[-1] - 1
        rows.append({"name": name, "final$": eq.iloc[-1], "ret": tot, "sharpe": sh,
                     "maxdd": dd, "oos_ret": oos_tot, "net": net})
    df = pd.DataFrame(rows).sort_values("sharpe", ascending=False).reset_index(drop=True)

    emax_sh = expected_max_sharpe(len(strats), years)
    print(f"Multiple-testing bar: expected MAX Sharpe of {len(strats)} zero-edge "
          f"strategies over {years:.2f}yr = {emax_sh:.2f}")
    print("A config must beat THIS Sharpe to be more than best-of-N luck.\n")

    # candidate filter: profitable full + OOS, then run MC on those
    cand = df[(df.ret > 0) & (df.oos_ret > 0) & (~df.name.str.startswith("BENCH"))].copy()
    print(f"{len(cand)} configs are profitable BOTH full-period AND out-of-sample "
          f"(before MC / luck correction).\n")

    print("=" * 100)
    print(f"{'strategy':<32} {'final$':>8} {'ret%':>7} {'OOS%':>7} {'Sharpe':>7} {'maxDD%':>7} "
          f"{'MC P(profit)':>12} {'MC 5%ret':>9}")
    print("-" * 100)
    survivors = []
    for _, r in cand.iterrows():
        p_prof, p5 = mc_prob_profit(r["net"])
        passes = (p_prof >= 0.95) and (r["sharpe"] > emax_sh)
        tag = "  <== PASSES ALL" if passes else ""
        print(f"{r['name']:<32} {r['final$']:>8.0f} {r['ret']*100:>6.1f}% {r['oos_ret']*100:>6.1f}% "
              f"{r['sharpe']:>7.2f} {r['maxdd']*100:>6.1f}% {p_prof*100:>11.1f}% {p5:>8.1f}%{tag}")
        if passes:
            survivors.append((r, p_prof, p5))

    print("=" * 100)
    # reference: where the benchmarks landed
    print("\nBENCHMARKS for context:")
    for _, r in df[df.name.str.startswith("BENCH")].iterrows():
        print(f"  {r['name']:<30} final=${r['final$']:.0f} ret={r['ret']*100:+.1f}% "
              f"OOS={r['oos_ret']*100:+.1f}% Sharpe={r['sharpe']:.2f} maxDD={r['maxdd']*100:.1f}%")

    print("\n" + "=" * 100)
    if survivors:
        print(f"SURVIVORS ({len(survivors)}) — profitable full + OOS + MC P>=95% + Sharpe>best-of-{len(strats)}-luck:")
        for r, pp, p5 in survivors:
            print(f"  * {r['name']}: ${r['final$']:.0f} on $1000 ({r['ret']*100:+.1f}%), "
                  f"OOS {r['oos_ret']*100:+.1f}%, Sharpe {r['sharpe']:.2f}, MC P(profit)={pp*100:.0f}%")
    else:
        print("SURVIVORS: NONE. No config cleared profitable + OOS + Monte Carlo + the")
        print("best-of-N luck bar. Consistent with the honest finding: no directional edge")
        print("survives cost; only structural allocation reduces RISK, it doesn't manufacture return.")
    print("=" * 100)
    print("\nCaveat: 2yr / one macro cycle screen. A survivor earns a FORWARD paper test, not real money.")


if __name__ == "__main__":
    main()

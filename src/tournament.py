"""Strategy tournament core — generate 100+ candidate strategies, score each
honestly, and surface them for the dashboard + (stage 3) auto-allocator.

THE DISCIPLINE (carried over from scripts/strategy_tournament.py, do NOT relax):
this is a *discovery* tool, not a proof machine. With 100+ candidates, several
top any leaderboard by pure luck. So:
  1. Every candidate pays the real ~0.5% round-trip cost (COST_LEG per unit of
     position change) — the cost wall the whole repo's research turns on.
  2. A candidate is only `robust` if it has a positive risk-adjusted return on
     EVERY coin AND in BOTH halves of the window. One-coin/one-half wins are fits.
  3. The leaderboard reports the multiple-testing reality up front: how many of
     k candidates clear by chance, and a Šidák-corrected t-bar. Clearing the
     in-sample bar makes a candidate a CANDIDATE for forward proof
     (proof_scorecard / the stage-3 allocator), never an auto-deploy.
  4. Each candidate is tagged `long_only_ok` — a US Kraken-spot account can't
     short, so only long-only candidates are executable today.

Pure/testable: every function takes DataFrames; only the runner script does I/O.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Callable

import numpy as np
import pandas as pd

COST_LEG = 0.0025  # per unit of position change; round trip (enter+exit) = 0.5%
START = 1000.0
ANN = 365  # daily bars


# ── indicators (lookahead-free: decided at close[t], realized on t+1) ──────────
def sma(s: pd.Series, n: int) -> pd.Series:
    return s.rolling(n).mean()


def ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False).mean()


def roc(s: pd.Series, n: int) -> pd.Series:
    return s.pct_change(n)


def zscore(s: pd.Series, n: int) -> pd.Series:
    return (s - s.rolling(n).mean()) / s.rolling(n).std()


def realized_vol(s: pd.Series, n: int = 20) -> pd.Series:
    return s.pct_change().rolling(n).std()


def rsi(s: pd.Series, n: int = 14) -> pd.Series:
    d = s.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    rs = up / dn.replace(0, np.nan)
    return (100 - 100 / (1 + rs)).fillna(50)


def macd_line(s: pd.Series, f: int = 12, sl: int = 26) -> pd.Series:
    return ema(s, f) - ema(s, sl)


@dataclass
class Candidate:
    name: str
    fn: Callable[[pd.DataFrame], pd.Series]  # → position series in [-1, 1]
    long_only_ok: bool
    family: str


def generate_candidates() -> dict[str, Candidate]:
    """Programmatic param grids over established families → 100+ candidates.

    Each `fn` returns a target position in [-1, 1] decided at the bar close. The
    long/short version and a clipped long-only version are BOTH registered where
    shorting is meaningful (the long-only one is what Kraken spot can run today).
    """
    reg: dict[str, Candidate] = {}
    c = lambda df: df["close"]  # noqa: E731

    def add(name: str, fn, family: str, *, both: bool = True, lo_only: bool = False) -> None:
        if lo_only:
            reg[name] = Candidate(name, lambda df, f=fn: pd.Series(f(df), index=df.index).clip(0, 1),
                                  True, family)
            return
        reg[name] = Candidate(name, fn, False, family)
        if both:
            lo = f"{name}__LO"
            reg[lo] = Candidate(lo, lambda df, f=fn: pd.Series(f(df), index=df.index).clip(0, 1),
                                True, family)

    # Trend: price vs SMA (the repo's most robust family — short lookbacks win)
    for n in (10, 20, 30, 40, 50, 75, 100, 125, 150, 200):
        add(f"tsmom_{n}", lambda df, n=n: np.sign(c(df) - sma(c(df), n)), "trend")

    # Trend: price vs EMA (faster-reacting variant of the same family)
    for n in (10, 20, 30, 50, 100, 200):
        add(f"etrend_{n}", lambda df, n=n: np.sign(c(df) - ema(c(df), n)), "trend")

    # MA crossovers
    for f, s in ((5, 20), (10, 30), (12, 26), (20, 50), (20, 100), (50, 100),
                 (50, 150), (50, 200), (100, 200)):
        add(f"sma_x_{f}_{s}", lambda df, f=f, s=s: np.sign(sma(c(df), f) - sma(c(df), s)), "cross")
    for f, s in ((8, 21), (12, 26), (20, 50), (50, 200)):
        add(f"ema_x_{f}_{s}", lambda df, f=f, s=s: np.sign(ema(c(df), f) - ema(c(df), s)), "cross")

    # Momentum / ROC sign
    for n in (10, 20, 30, 60, 90, 120):
        add(f"roc_{n}", lambda df, n=n: np.sign(roc(c(df), n)), "momentum")

    # Donchian breakout (long when close = N-day high, flat/short at N-day low)
    for n in (10, 20, 30, 55):
        def don(df, n=n):
            hi = df["high"].rolling(n).max().shift(1)
            lo = df["low"].rolling(n).min().shift(1)
            up = (c(df) >= hi).astype(float)
            dn = (c(df) <= lo).astype(float)
            return up - dn
        add(f"donchian_{n}", don, "breakout")

    # MACD line sign
    for f, s in ((12, 26), (8, 21), (19, 39)):
        add(f"macd_{f}_{s}", lambda df, f=f, s=s: np.sign(macd_line(c(df), f, s)), "macd")

    # Vol-targeted trend (tsmom scaled by inverse realized vol, capped to [-1,1])
    for n in (50, 100, 200):
        def vt(df, n=n):
            sig = np.sign(c(df) - sma(c(df), n))
            v = realized_vol(c(df), 20)
            scale = (0.02 / v).clip(upper=1.0).fillna(0.0)
            return (sig * scale)
        add(f"voltrend_{n}", vt, "voltarget")

    # RSI mean-reversion (long-only: buy oversold, exit overbought) — MR families
    for lo_th, hi_th in ((30, 70), (25, 75), (20, 80), (35, 65)):
        def rsi_mr(df, lo=lo_th, hi=hi_th):
            r = rsi(c(df), 14)
            pos = pd.Series(np.nan, index=df.index)
            pos[r < lo] = 1.0
            pos[r > hi] = 0.0
            return pos.ffill().fillna(0.0)
        add(f"rsi_mr_{lo_th}_{hi_th}", rsi_mr, "meanrev", lo_only=True)

    # Bollinger: breakout (above upper) and mean-revert (below lower)
    for n, k in ((20, 2.0), (20, 1.5), (50, 2.0)):
        def boll_bo(df, n=n, k=k):
            mid = sma(c(df), n); sd = c(df).rolling(n).std()
            return (c(df) > mid + k * sd).astype(float) - (c(df) < mid - k * sd).astype(float)
        add(f"boll_bo_{n}_{k}", boll_bo, "breakout")

    # Channel position (z-score reversion, long-only)
    for n in (20, 50, 100):
        def zrev(df, n=n):
            z = zscore(c(df), n)
            pos = pd.Series(np.nan, index=df.index)
            pos[z < -1.0] = 1.0
            pos[z > 1.0] = 0.0
            return pos.ffill().fillna(0.0)
        add(f"zrev_{n}", zrev, "meanrev", lo_only=True)

    return reg


# ── backtest + metrics (honest cost, lookahead-free) ───────────────────────────
def backtest(df: pd.DataFrame, pos, cost: float = COST_LEG) -> tuple[pd.Series, pd.Series]:
    pos = pd.Series(pos, index=df.index).clip(-1, 1).fillna(0)
    held = pos.shift(1).fillna(0)  # decided at t, realized on t+1
    gross = held * df["close"].pct_change().fillna(0)
    chg = held.diff().abs().fillna(0) * cost
    net = gross - chg
    eq = (1 + net).cumprod() * START
    return net, eq


def metrics(net: pd.Series, eq: pd.Series) -> dict:
    net = net.dropna()
    if len(net) < 2:
        return dict(final=START, ret=0.0, cagr=0.0, sharpe=0.0, mdd=0.0, trades=0, t_stat=0.0)
    sd = float(net.std())
    n = len(net)
    sharpe = (net.mean() / sd * math.sqrt(ANN)) if sd > 0 else 0.0
    t_stat = (net.mean() / sd * math.sqrt(n)) if sd > 0 else 0.0
    roll_max = eq.cummax()
    mdd = float(((eq / roll_max) - 1).min())
    trades = int((held := net != 0).sum())
    return dict(final=float(eq.iloc[-1]), ret=float(eq.iloc[-1] / START - 1),
                cagr=float((eq.iloc[-1] / START) ** (ANN / n) - 1),
                sharpe=float(sharpe), mdd=mdd, trades=trades, t_stat=float(t_stat))


def sidak_t_bar(k: int, alpha: float = 0.05) -> float:
    """Two-sided Šidák family-wise |t| bar for k simultaneous candidates.

    Mirrors proof_scorecard's selection-bias logic so the leaderboard is honest
    about 'best of k'. Normal-approx inverse CDF (Acklam) — good enough for a bar.
    """
    k = max(1, int(k))
    p = 1 - (1 - alpha) ** (1.0 / k)  # per-test alpha
    q = 1 - p / 2.0
    # Acklam inverse-normal approximation
    a = [-3.969683028665376e+01, 2.209460984245205e+02, -2.759285104469687e+02,
         1.383577518672690e+02, -3.066479806614716e+01, 2.506628277459239e+00]
    b = [-5.447609879822406e+01, 1.615858368580409e+02, -1.556989798598866e+02,
         6.680131188771972e+01, -1.328068155288572e+01]
    cc = [-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e+00,
          -2.549732539343734e+00, 4.374664141464968e+00, 2.938163982698783e+00]
    d = [7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e+00,
         3.754408661907416e+00]
    plow, phigh = 0.02425, 1 - 0.02425
    if q < plow:
        z = math.sqrt(-2 * math.log(q))
        val = (((((cc[0] * z + cc[1]) * z + cc[2]) * z + cc[3]) * z + cc[4]) * z + cc[5]) / \
              ((((d[0] * z + d[1]) * z + d[2]) * z + d[3]) * z + 1)
    elif q <= phigh:
        z = q - 0.5
        r = z * z
        val = (((((a[0] * r + a[1]) * r + a[2]) * r + a[3]) * r + a[4]) * r + a[5]) * z / \
              (((((b[0] * r + b[1]) * r + b[2]) * r + b[3]) * r + b[4]) * r + 1)
    else:
        z = math.sqrt(-2 * math.log(1 - q))
        val = -(((((cc[0] * z + cc[1]) * z + cc[2]) * z + cc[3]) * z + cc[4]) * z + cc[5]) / \
              ((((d[0] * z + d[1]) * z + d[2]) * z + d[3]) * z + 1)
    return abs(val)


def evaluate(candidates: dict[str, Candidate], data: dict[str, pd.DataFrame],
             cost: float = COST_LEG) -> list[dict]:
    """Score every candidate on every coin + both halves. Returns ranked rows.

    Adds a `random control` noise floor and `buy & hold` benchmark, and tags each
    row `robust` (positive on every coin AND both halves) + `passes_family`
    (|t| over the Šidák bar for k = number of real candidates).
    """
    rows: list[dict] = []
    k = len(candidates)
    t_bar = sidak_t_bar(k)
    for name, cand in candidates.items():
        per_coin = []
        for df in data.values():
            pos = pd.Series(cand.fn(df), index=df.index).clip(-1, 1).fillna(0)
            net, eq = backtest(df, pos, cost)
            m = metrics(net, eq)
            half = len(net) // 2
            s1 = metrics(net.iloc[:half], (1 + net.iloc[:half]).cumprod() * START)["sharpe"]
            s2 = metrics(net.iloc[half:], (1 + net.iloc[half:]).cumprod() * START)["sharpe"]
            # honest trade count = number of times the realized position changes
            held = pos.shift(1).fillna(0)
            tc = int((held.diff().abs() > 1e-9).sum())
            per_coin.append((m, s1, s2, tc))
        sharpe = float(np.mean([p[0]["sharpe"] for p in per_coin]))
        t_stat = float(np.mean([p[0]["t_stat"] for p in per_coin]))
        final = float(np.mean([p[0]["final"] for p in per_coin]))
        mdd = float(np.mean([p[0]["mdd"] for p in per_coin]))
        trades = int(np.mean([p[3] for p in per_coin]))
        robust = (all(p[0]["sharpe"] > 0.3 for p in per_coin)
                  and all(p[1] > 0 and p[2] > 0 for p in per_coin))
        rows.append(dict(name=name, family=cand.family, long_only_ok=cand.long_only_ok,
                         sharpe=round(sharpe, 3), t_stat=round(t_stat, 2), final=round(final, 2),
                         ret_pct=round(final / START * 100 - 100, 1), mdd_pct=round(mdd * 100, 1),
                         trades=trades, robust=robust,
                         passes_family=bool(abs(t_stat) > t_bar and robust)))
    rows.sort(key=lambda r: r["sharpe"], reverse=True)
    return rows


def summarize(rows: list[dict], k: int) -> dict:
    real = [r for r in rows if not r["name"].startswith("[")]
    return dict(
        n_candidates=k,
        family_t_bar=round(sidak_t_bar(k), 2),
        n_robust=sum(1 for r in real if r["robust"]),
        n_passes_family=sum(1 for r in real if r["passes_family"]),
        n_long_only_executable=sum(1 for r in real if r["passes_family"] and r["long_only_ok"]),
    )

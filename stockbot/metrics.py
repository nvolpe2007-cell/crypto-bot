"""
Proof metrics for the stock backtest — the same honesty bar as the crypto bot's
proof_scorecard, ported standalone (no crypto imports).

A green total is NOT proof. What IS: net-of-cost per-trade expectancy, a t-stat
(is the mean distinguishable from zero?), Sharpe, win rate, max drawdown, profit
factor. The PRE-REGISTERED bar — do not relax it to chase a pass:

    PROVEN ⟺ n >= 30 AND expectancy > 0 AND t_stat > 2

CRITICAL CAVEAT baked into the verdict text: a backtest is IN-SAMPLE. Clearing
this bar on one historical run is necessary, not sufficient — walk-forward / out-
of-sample + a correction for however many parameter sets you tried (the family-
wise / deflated-Sharpe problem) come before real money. This module flags it.
"""
from __future__ import annotations

import math
import statistics as st
from typing import List

N_MIN, T_MIN = 30, 2.0
DSR_MIN = 0.95
_EULER = 0.5772156649015329


def summary(net_rets: List[float]) -> dict:
    n = len(net_rets)
    if n == 0:
        return dict(n=0, total=0.0, expectancy=0.0, win_rate=0.0, t_stat=0.0,
                    sharpe=0.0, max_dd=0.0, profit_factor=0.0, skew=0.0, kurt=3.0)
    total = sum(net_rets)
    mean = total / n
    sd = st.pstdev(net_rets) if n < 2 else st.stdev(net_rets)
    t_stat = (mean / (sd / math.sqrt(n))) if (n >= 2 and sd > 0) else 0.0
    sharpe = (mean / sd) if sd > 0 else 0.0          # per-trade (not annualised)
    wins = [r for r in net_rets if r > 0]
    losses = [r for r in net_rets if r < 0]
    win_rate = len(wins) / n
    gross_win, gross_loss = sum(wins), -sum(losses)
    profit_factor = (gross_win / gross_loss) if gross_loss > 0 else float("inf")
    sd_pop = st.pstdev(net_rets)
    if n >= 2 and sd_pop > 0:
        m3 = sum((r - mean) ** 3 for r in net_rets) / n
        m4 = sum((r - mean) ** 4 for r in net_rets) / n
        skew, kurt = m3 / sd_pop ** 3, m4 / sd_pop ** 4
    else:
        skew, kurt = 0.0, 3.0
    cum = peak = dd = 0.0
    for r in net_rets:
        cum += r
        peak = max(peak, cum)
        dd = min(dd, cum - peak)
    return dict(n=n, total=total, expectancy=mean, win_rate=win_rate, t_stat=t_stat,
                sharpe=sharpe, max_dd=dd, profit_factor=profit_factor,
                skew=skew, kurt=kurt)


def expected_max_sharpe(sharpes: List[float]) -> float:
    """Expected MAX per-trade Sharpe across N unskilled trials (False Strategy
    Theorem) — the benchmark a grid-selected edge must beat, not zero. Rises with
    the number of param sets tried and their Sharpe spread. N<2 / no variance → 0."""
    N = len(sharpes)
    if N < 2:
        return 0.0
    var = st.pvariance(sharpes)
    if var <= 0:
        return 0.0
    sd = math.sqrt(var)
    Z = st.NormalDist()
    return sd * ((1.0 - _EULER) * Z.inv_cdf(1.0 - 1.0 / N)
                 + _EULER * Z.inv_cdf(1.0 - 1.0 / (N * math.e)))


def deflated_sharpe(sharpe: float, n: int, skew: float, kurt: float, sr0: float) -> float:
    """P(true Sharpe > sr0), correcting for trials (via sr0) AND non-normal returns
    (skew/kurtosis). The honest test that a backtest Sharpe isn't a multiple-testing
    fluke. Robust when > DSR_MIN."""
    if n < 2:
        return 0.0
    denom = 1.0 - skew * sharpe + ((kurt - 1.0) / 4.0) * sharpe * sharpe
    if denom <= 0:
        return 0.0
    z = (sharpe - sr0) * math.sqrt(n - 1.0) / math.sqrt(denom)
    return st.NormalDist().cdf(z)


def verdict(s: dict) -> str:
    if s["n"] < N_MIN:
        return f"NOT PROVEN — only {s['n']} trades (need {N_MIN}+)"
    if s["expectancy"] <= 0:
        return f"FAILED — negative expectancy ({s['expectancy']:+.5f}/trade net of cost)"
    if s["t_stat"] <= T_MIN:
        return f"NOT PROVEN — t={s['t_stat']:.2f} (need >{T_MIN})"
    return (f"PROVEN (in-sample) ✓ exp={s['expectancy']:+.5f}/trade  t={s['t_stat']:.2f} "
            f"— but IN-SAMPLE only; walk-forward + multiple-testing correction before real money")


def render(s: dict, label: str = "ORB intraday") -> str:
    if s["n"] == 0:
        return f"{label}: no trades."
    pf = "inf" if s["profit_factor"] == float("inf") else f"{s['profit_factor']:.2f}"
    return (
        f"{label}\n"
        f"  trades={s['n']}  net_sum={s['total']*100:+.2f}%  "
        f"exp={s['expectancy']*100:+.3f}%/trade  win={s['win_rate']*100:.0f}%\n"
        f"  t-stat={s['t_stat']:.2f}  sharpe/trade={s['sharpe']:.2f}  "
        f"maxDD={s['max_dd']*100:+.2f}%  profit_factor={pf}\n"
        f"  → {verdict(s)}"
    )

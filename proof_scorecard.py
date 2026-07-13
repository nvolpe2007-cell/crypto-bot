#!/usr/bin/env python3
"""
Proof scorecard — the honest "is this actually an edge?" instrument.

A green P&L number is not proof. This computes, per strategy/arm, the things
that ARE proof: net-of-all-cost P&L, trade count, win rate, per-trade
expectancy, a t-statistic (is the mean return distinguishable from zero?),
Sharpe, and max drawdown. It also flags whether each arm is EXECUTABLE for a
US retail Kraken-spot account or FANTASY (geo-blocked / needs shorting /
assumed fills), and borrow-corrects the aggressive arm.

The verdict per arm is deliberately strict and PRE-REGISTERED so nobody can
move the goalposts after seeing the number:

    PROVEN  ⟺  executable AND n>=30 closed trades AND expectancy>0
                AND t_stat>2 (≈95% the edge isn't noise)

Anything else is NOT PROVEN — not "bad", just not yet evidence. Most arms will
read NOT PROVEN, which is the honest state of this system.

Run on the VPS where the live ledgers are:
    ssh crypto-bot-vps "cd /opt/crypto-bot && python3 proof_scorecard.py"
"""
from __future__ import annotations

import csv
import json
import math
import statistics as st
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
try:
    from arbitrage.funding_arb_paper import (
        _base_symbol, MAJOR_SYMBOLS, BORROW_APY_MAJOR, BORROW_APY_ALT,
        FUNDING_CYCLE_HOURS,
    )
except Exception:  # standalone fallback — keep the constants in sync with the module
    MAJOR_SYMBOLS = {'BTC', 'ETH', 'SOL', 'BNB', 'XRP', 'DOGE', 'ADA', 'AVAX',
                     'LINK', 'LTC', 'DOT', 'TRX', 'BCH', 'NEAR', 'ATOM', 'UNI'}
    BORROW_APY_MAJOR, BORROW_APY_ALT, FUNDING_CYCLE_HOURS = 10.0, 50.0, 8
    def _base_symbol(s):
        s = s.upper().replace('PF_', '')
        for q in ('USDT', 'USDC', 'USD'):
            if s.endswith(q):
                return s[:-len(q)]
        return s

DATA = Path(__file__).parent / 'data'

# n_min / t_min are the PRE-REGISTERED proof bar — do not relax to chase a pass.
N_MIN, T_MIN = 30, 2.0


def _family_t_bar(k: int, alpha: float = 0.05) -> float:
    """Šidák-corrected two-sided t-bar for judging k arms simultaneously.

    A per-arm t>2 read off the BEST of k strategies is far weaker evidence than
    a single pre-registered t>2 — this is the multiple-comparisons trap, the
    exact selection-bias / overfitting failure that blows up retail bots
    (RESEARCH_strategies_and_filters.md §1). The Šidák correction tightens the
    per-arm threshold so the FAMILY-wise false-positive rate stays at `alpha`:
    per-arm alpha' = 1 - (1-alpha)^(1/k). Never returns below the pre-registered
    T_MIN, so k<=1 reproduces the original bar exactly (no regression)."""
    if k <= 1:
        return T_MIN
    alpha_family = 1.0 - (1.0 - alpha) ** (1.0 / k)
    z = st.NormalDist().inv_cdf(1.0 - alpha_family / 2.0)
    return max(T_MIN, z)


# Deflated Sharpe Ratio acceptance bar (Bailey & López de Prado): the probability
# the arm's Sharpe is real must exceed this. 0.95 ≈ "95% it isn't a fluke of
# multiple testing + non-normal returns".
DSR_MIN = 0.95
_EULER_MASCHERONI = 0.5772156649015329


def _expected_max_sharpe(sharpes: list[float]) -> float:
    """Expected MAXIMUM per-trade Sharpe among N unskilled trials (López de Prado's
    False Strategy Theorem) — the benchmark a real edge must beat. Rises with the
    number of arms tried (N) and the spread of their Sharpes, so 'best of many'
    has to clear a higher bar. N<2 or zero variance → 0 (no deflation)."""
    N = len(sharpes)
    if N < 2:
        return 0.0
    var = st.pvariance(sharpes)
    if var <= 0:
        return 0.0
    sd = math.sqrt(var)
    Z = st.NormalDist()
    return sd * ((1.0 - _EULER_MASCHERONI) * Z.inv_cdf(1.0 - 1.0 / N)
                 + _EULER_MASCHERONI * Z.inv_cdf(1.0 - 1.0 / (N * math.e)))


def _deflated_sharpe(sharpe: float, n_eff: float, skew: float, kurt: float,
                     sr0: float) -> float:
    """Deflated Sharpe Ratio = P(true Sharpe > sr0), correcting BOTH for the number
    of trials (via sr0 from _expected_max_sharpe) AND for non-normal returns
    (skew/kurtosis widen the Sharpe's error band). `sharpe` is per-trade; `n_eff`
    is the correlation-adjusted sample size. Returns a probability in [0,1]; an
    edge is deflated-robust when it exceeds DSR_MIN."""
    if n_eff < 2:
        return 0.0
    denom = 1.0 - skew * sharpe + ((kurt - 1.0) / 4.0) * sharpe * sharpe
    if denom <= 0:
        return 0.0
    z = (sharpe - sr0) * math.sqrt(n_eff - 1.0) / math.sqrt(denom)
    return st.NormalDist().cdf(z)


def _design_effect_eff_n(nets: list[float], clusters: list) -> float:
    """Kish design effect → EFFECTIVE sample size for correlated trades.

    A per-trade t-stat assumes independent trades. But a long-only momentum book
    across 16 majors enters and wins/loses TOGETHER (one market regime → many
    correlated trades), so the true information content is < n. Trades sharing a
    cluster (here: entry-week) are treated as correlated; the intra-cluster
    correlation (ICC) is estimated by one-way ANOVA and converted to a design
    effect DEFF = 1 + (m̄-1)·ICC. eff_n = n / DEFF. Uncorrelated trades (ICC≈0)
    give eff_n≈n; highly-correlated trades shrink it. Only positive correlation
    (the optimistic-bias direction) is penalised; ICC is floored at 0.
    """
    n = len(nets)
    groups: dict = {}
    for x, c in zip(nets, clusters):
        groups.setdefault(c, []).append(x)
    k = len(groups)
    if n < 2 or k < 2 or k == n:        # nothing to cluster / all-singleton
        return float(n)
    grand = sum(nets) / n
    m_bar = n / k
    ss_between = sum(len(g) * (sum(g) / len(g) - grand) ** 2 for g in groups.values())
    ss_within = sum((x - sum(g) / len(g)) ** 2 for g in groups.values() for x in g)
    ms_between = ss_between / (k - 1)
    ms_within = ss_within / (n - k) if n > k else 0.0
    denom = ms_between + (m_bar - 1) * ms_within
    icc = (ms_between - ms_within) / denom if denom > 0 else 0.0
    icc = max(0.0, min(1.0, icc))       # penalise only positive (optimistic) corr
    deff = 1.0 + (m_bar - 1) * icc
    return n / deff if deff > 0 else float(n)


def _stats(nets: list[float], clusters: list | None = None) -> dict:
    """Per-trade summary stats for a list of net P&Ls (one per closed trade).

    If `clusters` (one key per trade) is given, also computes a correlation-
    adjusted t-stat on the EFFECTIVE sample size, which the verdict uses so a
    correlated universe can't manufacture significance. Without clusters the
    adjusted t-stat equals the raw one."""
    n = len(nets)
    total = sum(nets)
    if n == 0:
        return dict(n=0, total=0.0, win_rate=0.0, expectancy=0.0,
                    t_stat=0.0, t_clustered=0.0, eff_n=0.0, sharpe=0.0, max_dd=0.0,
                    skew=0.0, kurt=3.0)
    wins = sum(1 for x in nets if x > 0)
    mean = total / n
    sd = st.pstdev(nets) if n < 2 else st.stdev(nets)
    t_stat = (mean / (sd / math.sqrt(n))) if (n >= 2 and sd > 0) else 0.0
    eff_n = _design_effect_eff_n(nets, clusters) if clusters else float(n)
    t_clustered = (mean / (sd / math.sqrt(eff_n))) if (eff_n >= 2 and sd > 0) else 0.0
    sharpe = (mean / sd) if sd > 0 else 0.0          # per-trade Sharpe (not annualised)
    # Skewness + (raw) kurtosis of per-trade returns, for the Deflated Sharpe Ratio.
    # Population moments off the population std; normal defaults (skew 0, kurt 3)
    # when there's too little spread to estimate them.
    sd_pop = st.pstdev(nets)
    if n >= 2 and sd_pop > 0:
        m3 = sum((x - mean) ** 3 for x in nets) / n
        m4 = sum((x - mean) ** 4 for x in nets) / n
        skew = m3 / sd_pop ** 3
        kurt = m4 / sd_pop ** 4
    else:
        skew, kurt = 0.0, 3.0
    # max drawdown on the cumulative equity curve
    cum = peak = dd = 0.0
    for x in nets:
        cum += x
        peak = max(peak, cum)
        dd = min(dd, cum - peak)
    return dict(n=n, total=total, win_rate=wins / n, expectancy=mean,
                t_stat=t_stat, t_clustered=t_clustered, eff_n=eff_n,
                sharpe=sharpe, max_dd=dd, skew=skew, kurt=kurt)


def _borrow_owed(p: dict) -> float:
    if p.get('direction') != 'SHORT_SPOT_LONG_PERP':
        return 0.0
    apy = BORROW_APY_MAJOR if _base_symbol(p['symbol']) in MAJOR_SYMBOLS else BORROW_APY_ALT
    return (apy / 100.0) * p['size_usd'] * (p.get('cycles_collected', 0)
                                            * FUNDING_CYCLE_HOURS / (24.0 * 365.0))


def _arm(label: str, fname: str, executable: bool, borrow_correct: bool) -> dict | None:
    path = DATA / fname
    if not path.exists():
        return None
    d = json.loads(path.read_text())
    closed = d.get('closed', [])

    def net(p, corrected):
        owed = _borrow_owed(p) if corrected else p.get('borrow_cost', 0.0)
        return p.get('funding_collected', 0.0) - p.get('entry_cost', 0.0) - owed

    # order by close time so the drawdown curve is chronological
    closed = sorted(closed, key=lambda p: p.get('close_time_iso') or '')
    booked = _stats([net(p, False) for p in closed])
    out = dict(label=label, executable=executable, **booked)
    if borrow_correct:
        out['corrected_total'] = sum(net(p, True) for p in closed)
    return out


def _swing_forward() -> dict | None:
    """The long-only majors swing strategy's FORWARD paper record — the one
    built to actually clear the bar. Reads swing_paper.py's state file."""
    path = DATA / 'swing_paper_state.json'
    if not path.exists():
        return None
    d = json.loads(path.read_text())
    closed = sorted(d.get('closed', []), key=lambda p: p.get('exit_ts') or '')
    nets = [float(p['pnl']) for p in closed]

    # Cluster by ENTRY WEEK: the 16 majors are highly correlated, so trades that
    # open the same week share market risk and aren't independent bets. The
    # clustered t-stat is what the verdict judges (see _design_effect_eff_n).
    def _week(p) -> str:
        try:
            dt = datetime.utcfromtimestamp(int(p.get('entry_ts')))
            iso = dt.isocalendar()
            return f"{iso[0]}-W{iso[1]:02d}"
        except (TypeError, ValueError):
            return 'unknown'
    clusters = [_week(p) for p in closed]
    s = _stats(nets, clusters)
    return dict(label='Swing (long majors, FORWARD)', executable=True, **s)


def _tsmom_forward() -> dict | None:
    """Trend-following ALLOCATION forward record (tsmom_paper.py). Long/cash on
    BTC/ETH/SOL by SMA200+band — the low-turnover candidate that survives the cost
    wall. Judged on the same pre-registered bar; entry-week clustered like swing
    (the 3 majors co-trend, so episodes aren't fully independent bets)."""
    path = DATA / 'tsmom_paper_state.json'
    if not path.exists():
        return None
    d = json.loads(path.read_text())
    closed = sorted(d.get('closed', []), key=lambda p: p.get('exit_ts') or '')
    nets = [float(p['pnl']) for p in closed]

    def _week(p) -> str:
        try:
            dt = datetime.utcfromtimestamp(int(p.get('entry_ts')))
            iso = dt.isocalendar()
            return f"{iso[0]}-W{iso[1]:02d}"
        except (TypeError, ValueError):
            return 'unknown'
    s = _stats(nets, [_week(p) for p in closed])
    return dict(label='Trend-follow BTC/ETH/SOL (FORWARD)', executable=True, **s)


def _tsmom_fast_forward() -> dict | None:
    """Fast-lookback (SMA50) long-only trend — the 37-strategy tournament's robust
    executable winner (tsmom_long_50 backtested ~$1470 vs the SMA200 control's
    ~$922). Same pre-registered code, faster lookback; run as a parallel forward
    arm so the proof bar judges fast-vs-slow head to head. Week-clustered."""
    path = DATA / 'tsmom_fast_state.json'
    if not path.exists():
        return None
    d = json.loads(path.read_text())
    closed = sorted(d.get('closed', []), key=lambda p: p.get('exit_ts') or '')
    nets = [float(p['pnl']) for p in closed]

    def _week(p) -> str:
        try:
            dt = datetime.utcfromtimestamp(int(p.get('entry_ts')))
            iso = dt.isocalendar()
            return f"{iso[0]}-W{iso[1]:02d}"
        except (TypeError, ValueError):
            return 'unknown'
    s = _stats(nets, [_week(p) for p in closed])
    return dict(label='Trend-follow FAST SMA50 (FORWARD)', executable=True, **s)


def _conf_forward() -> dict | None:
    """Confluence trend allocation forward record (conf_paper.py): long-only on
    BTC/ETH/SOL only when price is BOTH above SMA100 AND 20d momentum is positive —
    the long-only tournament's best DRAWDOWN-adjusted spot-executable bot. A distinct
    SIGNAL (a two-condition conjunction), not just another lookback. Same pre-registered
    bar; entry-week clustered like the other trend arms (the 3 majors co-trend)."""
    path = DATA / 'conf_paper_state.json'
    if not path.exists():
        return None
    d = json.loads(path.read_text())
    closed = sorted(d.get('closed', []), key=lambda p: p.get('exit_ts') or '')
    nets = [float(p['pnl']) for p in closed]

    def _week(p) -> str:
        try:
            dt = datetime.utcfromtimestamp(int(p.get('entry_ts')))
            iso = dt.isocalendar()
            return f"{iso[0]}-W{iso[1]:02d}"
        except (TypeError, ValueError):
            return 'unknown'
    s = _stats(nets, [_week(p) for p in closed])
    return dict(label='Trend+momo confluence (FORWARD)', executable=True, **s)


def _btc_trend_forward() -> dict | None:
    """Focused BTC-only trend allocation forward record (btc_trend_paper.py): the
    confluence signal (>SMA100 AND 20d momentum up) on BTC alone with the WHOLE book —
    the owner's 'one simple strategy for BTC'. Same pre-registered bar as the other
    trend arms; week-clustered. NOTE: one asset trades ~4-10x/yr, so n>=30 is a ~5-year
    clock — expect a qualitative (drawdown vs B&H) read long before the t-test can fire."""
    path = DATA / 'btc_trend_state.json'
    if not path.exists():
        return None
    d = json.loads(path.read_text())
    closed = sorted(d.get('closed', []), key=lambda p: p.get('exit_ts') or '')
    nets = [float(p['pnl']) for p in closed]

    def _week(p) -> str:
        try:
            dt = datetime.utcfromtimestamp(int(p.get('entry_ts')))
            iso = dt.isocalendar()
            return f"{iso[0]}-W{iso[1]:02d}"
        except (TypeError, ValueError):
            return 'unknown'
    s = _stats(nets, [_week(p) for p in closed])
    return dict(label='BTC trend (focused, FORWARD)', executable=True, **s)


def _kelly_trend_forward() -> dict | None:
    """Conviction-scaled fractional-Kelly COMPOUNDING on the SAME BTC trend signal
    (kelly_trend_paper.py). Identical entries/exits to BTC trend (focused) — the only
    difference is sizing (bet a conviction-scaled fraction of CURRENT equity, no
    leverage). The head-to-head test of whether compounding+conviction sizing beats
    flat. Same pre-registered bar; week-clustered."""
    path = DATA / 'kelly_trend_state.json'
    if not path.exists():
        return None
    d = json.loads(path.read_text())
    closed = sorted(d.get('closed', []), key=lambda p: p.get('exit_ts') or '')
    nets = [float(p['pnl']) for p in closed]

    def _week(p) -> str:
        try:
            dt = datetime.utcfromtimestamp(int(p.get('entry_ts')))
            iso = dt.isocalendar()
            return f"{iso[0]}-W{iso[1]:02d}"
        except (TypeError, ValueError):
            return 'unknown'
    s = _stats(nets, [_week(p) for p in closed])
    return dict(label='BTC trend Kelly-compound (FORWARD)', executable=True, **s)


def _tsmom_ls_forward() -> dict | None:
    """Trend LONG/SHORT perp allocation forward record (tsmom_ls_paper.py): tsmom_50
    that goes SHORT in downtrends instead of cash, via paper perps, 1x, charged a
    conservative funding drag. The paper proof for the short side Kraken US perps will
    unlock (short_leg_value.py found it ETH-carried & funding-fragile — this earns or
    kills it on the forward clock). Executable once US perps are enabled (matches the
    regime arm's treatment). Entry-week clustered like the other trend arms."""
    path = DATA / 'tsmom_ls_state.json'
    if not path.exists():
        return None
    d = json.loads(path.read_text())
    closed = sorted(d.get('closed', []), key=lambda p: p.get('exit_ts') or '')
    nets = [float(p['pnl']) for p in closed]

    def _week(p) -> str:
        try:
            dt = datetime.utcfromtimestamp(int(p.get('entry_ts')))
            iso = dt.isocalendar()
            return f"{iso[0]}-W{iso[1]:02d}"
        except (TypeError, ValueError):
            return 'unknown'
    s = _stats(nets, [_week(p) for p in closed])
    return dict(label='Trend L/S perp (FORWARD, paper)', executable=True, **s)


def _brain_forward() -> dict | None:
    """Claude-driven discretionary arm forward record (brain_paper.py): the brain
    decides long/short/flat per coin daily from the full market picture, on its own
    paper perp account. Judged on the SAME pre-registered bar as the mechanical arms —
    the honest test of whether a 'thinking' brain beats rules. Entry-week clustered."""
    path = DATA / 'brain_paper_state.json'
    if not path.exists():
        return None
    d = json.loads(path.read_text())
    closed = sorted(d.get('closed', []), key=lambda p: p.get('exit_ts') or '')
    nets = [float(p['pnl']) for p in closed]

    def _week(p) -> str:
        try:
            dt = datetime.utcfromtimestamp(int(p.get('entry_ts')))
            iso = dt.isocalendar()
            return f"{iso[0]}-W{iso[1]:02d}"
        except (TypeError, ValueError):
            return 'unknown'
    s = _stats(nets, [_week(p) for p in closed])
    return dict(label='AI brain discretionary (FORWARD, paper)', executable=True, **s)


def _microstructure_forward() -> dict | None:
    """Maker-only microstructure scalper forward record (the OFI+CVD+OBI gate on
    real Kraken tick data, post-only fills). The 90-day re-test of the engine that
    previously FAILED at taker cost on 2s-REST snapshots — judged on the SAME bar.
    Clustered by ENTRY-MINUTE (scalper trades within a minute are highly correlated,
    so they aren't independent bets). Executable: maker-only LONGS on Kraken spot are
    US-retail-executable (short trades, if any, would not be)."""
    path = DATA / 'micro_paper_state.json'
    if not path.exists():
        return None
    d = json.loads(path.read_text())
    closed = sorted(d.get('closed', []), key=lambda p: p.get('exit_ts') or '')
    nets = [float(p['pnl']) for p in closed]

    def _minute(p) -> str:
        try:
            dt = datetime.utcfromtimestamp(int(float(p.get('entry_ts'))))
            return dt.strftime('%Y-%m-%dT%H:%M')
        except (TypeError, ValueError):
            return 'unknown'
    s = _stats(nets, [_minute(p) for p in closed])
    return dict(label='Microstructure maker-only (FORWARD, Kraken tick)',
                executable=True, **s)


def _lev_perp_forward() -> dict | None:
    """Leveraged perp arm forward record (lev_perp_paper.py): opens a 3x perp in the
    trend direction and exits on a FIXED TAKE-PROFIT ('sell in profit'), with a
    realistic liquidation as the downside. Its own paper book, judged on the SAME
    pre-registered bar — the honest test of whether leverage+take-profit beats the
    1x rules, or just liquidates faster (memory doubling_in_a_month_verdict). PAPER
    only (US Kraken-spot can't trade perps yet). Entry-week clustered."""
    path = DATA / 'lev_perp_state.json'
    if not path.exists():
        return None
    d = json.loads(path.read_text())
    closed = sorted(d.get('closed', []), key=lambda p: p.get('exit_ts') or '')
    nets = [float(p['pnl']) for p in closed]

    def _week(p) -> str:
        try:
            dt = datetime.utcfromtimestamp(int(p.get('entry_ts')))
            iso = dt.isocalendar()
            return f"{iso[0]}-W{iso[1]:02d}"
        except (TypeError, ValueError):
            return 'unknown'
    s = _stats(nets, [_week(p) for p in closed])
    return dict(label='Leveraged perp 3x + take-profit (FORWARD, paper)', executable=True, **s)


def _lev_perp_v2_forward() -> dict | None:
    """V2 of the leveraged perp arm (lev_perp_v2_paper.py): IDENTICAL entries
    (SMA-50 direction, same four filters, vol-targeted leverage) but TRAILED
    exits — ATR(14) chandelier ratcheting from the peak — instead of v1's fixed
    +5% take-profit. Pre-specified A/B on the exit engine only: does letting
    winners run beat capping them, judged on the same pre-registered bar.
    Entry-week clustered like v1."""
    path = DATA / 'lev_perp_v2_state.json'
    if not path.exists():
        return None
    d = json.loads(path.read_text())
    closed = sorted(d.get('closed', []), key=lambda p: p.get('exit_ts') or '')
    nets = [float(p['pnl']) for p in closed]

    def _week(p) -> str:
        try:
            dt = datetime.utcfromtimestamp(int(p.get('entry_ts')))
            iso = dt.isocalendar()
            return f"{iso[0]}-W{iso[1]:02d}"
        except (TypeError, ValueError):
            return 'unknown'
    s = _stats(nets, [_week(p) for p in closed])
    return dict(label='Leveraged perp vol-tgt + ATR trail (FORWARD, paper)', executable=True, **s)


def _lev_perp_agg_variant(fname: str, label: str) -> dict | None:
    """Aggressive-config twins of the two lev_perp arms: SAME code, env-raised
    risk (LEV_PERP_VOL_TARGET=3.0, LEV_PERP_LEVERAGE=5 vs the 2.0/3x originals),
    own state files. Pre-specified 2026-07-02 as a 2x2 (exit engine x risk
    level) so leverage is judged as its own variable on the forward clock —
    the 21-month replay favored 3%/5x (+45% vs +30%, maxDD -10%, 0 liqs) but
    that sweep is exactly the post-hoc selection this scorecard exists to
    discipline. Entry-week clustered like the originals."""
    path = DATA / fname
    if not path.exists():
        return None
    d = json.loads(path.read_text())
    closed = sorted(d.get('closed', []), key=lambda p: p.get('exit_ts') or '')
    nets = [float(p['pnl']) for p in closed]

    def _week(p) -> str:
        try:
            dt = datetime.utcfromtimestamp(int(p.get('entry_ts')))
            iso = dt.isocalendar()
            return f"{iso[0]}-W{iso[1]:02d}"
        except (TypeError, ValueError):
            return 'unknown'
    s = _stats(nets, [_week(p) for p in closed])
    return dict(label=label, executable=True, **s)


def _pairs_forward() -> dict | None:
    """Market-neutral pairs arm forward record (pairs_paper.py): a DOLLAR-NEUTRAL
    relative-value trade — long the cheap leg, short the rich leg of a major pair when
    their price ratio stretches >ENTRY_Z, profiting from convergence regardless of market
    direction. The honest hedge-fund staple; PAPER (short leg simulated w/ funding drag)
    until Kraken US perps land. Judged on the SAME pre-registered bar — the cost wall is
    ~4 legs of fees per round-trip, so this earns or kills the neutral edge on the forward
    clock. Entry-week clustered like the other arms."""
    path = DATA / 'pairs_paper_state.json'
    if not path.exists():
        return None
    d = json.loads(path.read_text())
    closed = sorted(d.get('closed', []), key=lambda p: p.get('exit_ts') or '')
    nets = [float(p['pnl']) for p in closed]

    def _week(p) -> str:
        try:
            dt = datetime.utcfromtimestamp(int(p.get('entry_ts')))
            iso = dt.isocalendar()
            return f"{iso[0]}-W{iso[1]:02d}"
        except (TypeError, ValueError):
            return 'unknown'
    s = _stats(nets, [_week(p) for p in closed])
    return dict(label='Market-neutral pairs (FORWARD, paper)', executable=True, **s)


def _rebalance_forward() -> dict | None:
    """Rebalanced-allocation forward record (rebalance_paper.py): the ONE positive,
    robust-OOS result of the ~320-strategy search (memory exhaustive_search_320_zero /
    rebalance_premium_verdict). A diversified 50% crypto / 25% gold / 25% cash book
    rebalanced monthly — prediction-free, fully SPOT-EXECUTABLE on Kraken (long-only, no
    leverage/short), booking each monthly holding period as one record. NOT alpha (can't
    profit in a full bear, only lose far less); the bar judges whether the structural
    vol-harvest + diversification clears on the forward clock. Low turnover => ~12 obs/yr,
    so n>=30 is a multi-year timeline by design. Each record also carries the rebalancing
    PREMIUM vs a never-rebalanced hold of the same target."""
    path = DATA / 'rebalance_paper_state.json'
    if not path.exists():
        return None
    d = json.loads(path.read_text())
    closed = sorted(d.get('closed', []), key=lambda p: p.get('exit_ts') or '')
    nets = [float(p['pnl']) for p in closed]
    s = _stats(nets)
    return dict(label='Rebalanced allocation (crypto+gold+cash, FORWARD)', executable=True, **s)


def _directional() -> dict | None:
    csvf = DATA / 'trade_journal.csv'
    if not csvf.exists():
        return None
    rows = list(csv.DictReader(open(csvf)))
    real = [r for r in rows
            if not (r['trade_id'].startswith('id_') or r['trade_id'].startswith('BTC_17000'))
            and r.get('prob_win') not in (None, '', '0.0')]   # drop seed/synthetic rows
    nets = []
    for r in real:
        try:
            nets.append(float(r['pnl']))
        except (ValueError, KeyError):
            continue
    s = _stats(nets)
    return dict(label='Directional (long-only majors)', executable=True, **s)


def _regime_forward() -> dict | None:
    """Intraday regime-following arm (regime_arm.py): LONG in uptrends / SHORT in
    downtrends on an intraday clock, cost-gated, shorts via paper perps. The only
    arm that TRADES (shorts) in a downtrend rather than sitting in cash. Clustered
    by entry DAY — intraday same-session trades co-move, so they aren't independent."""
    path = DATA / 'regime_arm_state.json'
    if not path.exists():
        return None
    d = json.loads(path.read_text())
    closed = sorted(d.get('closed', []), key=lambda p: p.get('exit_ts') or '')
    nets = [float(p['pnl']) for p in closed]

    def _day(p) -> str:
        try:
            dt = datetime.utcfromtimestamp(int(p.get('entry_ts')))
            return dt.strftime('%Y-%m-%d')
        except (TypeError, ValueError):
            return 'unknown'
    s = _stats(nets, [_day(p) for p in closed])
    return dict(label='Regime intraday L/S (FORWARD)', executable=True, **s)


def _verdict(a: dict, t_family: float = T_MIN, k: int = 1) -> str:
    if not a['executable']:
        return 'FANTASY (not executable on a US Kraken-spot account)'
    if a['n'] < N_MIN:
        return f'NOT PROVEN — only {a["n"]} trades (need {N_MIN}+)'
    if a['expectancy'] <= 0:
        return f'FAILED — negative expectancy ({a["expectancy"]:+.4f}/trade)'
    # Judge on the CORRELATION-ADJUSTED t-stat: a correlated universe can post a
    # raw t>2 on far fewer independent bets. t_clustered==t_stat when uncorrelated.
    t = a.get('t_clustered', a['t_stat'])
    if t <= T_MIN:
        return (f'NOT PROVEN — clustered t={t:.2f} (need >{T_MIN}; '
                f'raw t={a["t_stat"]:.2f} but only ~{a.get("eff_n", a["n"]):.0f} '
                f'independent bets across correlated majors)')
    # Family-wise bar: clearing the single-arm T_MIN is NOT enough when k arms are
    # judged at once — the best of k inflates significance. Only an arm that also
    # clears the Šidák-corrected family bar is called PROVEN.
    if t <= t_family:
        return (f'PROVEN (single) — NOT family-wise robust: clustered t={t:.2f} '
                f'< family bar {t_family:.2f} ({k} strategies tested). A CANDIDATE, '
                f'not proof — best-of-{k} can clear a single t>2 by chance.')
    return (f'PROVEN ✓ exp={a["expectancy"]:+.4f}/trade  '
            f'clustered t={t:.2f} (eff_n≈{a.get("eff_n", a["n"]):.0f}) — '
            f'survives family-wise bar t>{t_family:.2f} across {k} tested')


def _swing_attribution() -> None:
    """P&L attribution for the swing forward record, broken down per symbol,
    timeframe, UTC session, and volatility tercile. Attribution is how we found
    the edge was 4h-majors-only (2026-06-08); this keeps that lens live as
    trades accumulate. Read-only — never gates anything."""
    path = DATA / 'swing_paper_state.json'
    if not path.exists():
        return
    closed = json.loads(path.read_text()).get('closed', [])
    if not closed:
        print('\n' + '-' * 78)
        print('SWING ATTRIBUTION: no closed trades yet — breakdown activates once '
              'trades accumulate.')
        return

    def _show(title: str, keyfn) -> None:
        groups: dict = {}
        for p in closed:
            try:
                k = keyfn(p)
            except (TypeError, ValueError, KeyError):
                k = 'unknown'
            if k is not None:
                groups.setdefault(k, []).append(float(p.get('pnl', 0.0)))
        if not groups:
            return
        print(f'\n  by {title}:')
        for k in sorted(groups, key=lambda g: sum(groups[g]), reverse=True):
            v = groups[k]
            wins = sum(1 for x in v if x > 0)
            print(f"    {str(k):<14} n={len(v):<3} net=${sum(v):+8.2f} "
                  f"win={wins/len(v)*100:3.0f}% exp=${sum(v)/len(v):+.4f}")

    # volatility tercile thresholds from the entry_atr_pct distribution
    vols = sorted(float(p['entry_atr_pct']) for p in closed if 'entry_atr_pct' in p)
    def _vol_bucket(p):
        if not vols or 'entry_atr_pct' not in p:
            return None
        v = float(p['entry_atr_pct']); lo = vols[len(vols)//3]; hi = vols[2*len(vols)//3]
        return 'low-vol' if v <= lo else ('high-vol' if v >= hi else 'mid-vol')
    def _session(p):
        h = p.get('entry_hour')
        if h is None:
            return None
        h = int(h)
        return ('Asia (0-7h)' if h < 8 else 'EU (8-15h)' if h < 16 else 'US (16-23h)')

    print('\n' + '-' * 78)
    print(f'SWING ATTRIBUTION  ({len(closed)} closed trades)')
    _show('symbol', lambda p: p.get('symbol'))
    _show('timeframe', lambda p: f"{p.get('tf')}m")
    _show('session', _session)
    _show('volatility', _vol_bucket)
    _show('VP zone', lambda p: p.get('vp_zone'))
    # Did the session gate's ratings actually predict P&L? Breaks the realised
    # record down by the SessionEdge verdict each trade was tagged with at entry.
    # If FAVORABLE buckets out-earn UNFAVORABLE ones, the time-of-day gate has a
    # real, measured edge and SESSION_FILTER_HARD=1 is justified. (Only populated
    # once trades opened after the session-tagging change accumulate.)
    if any(p.get('session_verdict') for p in closed):
        _show('session verdict', lambda p: p.get('session_verdict'))
    # Did TD Sequential alignment predict P&L? Long-only swing is a trend follower,
    # so the key question is whether longs taken after a recent TD sell-setup
    # (uptrend exhaustion) underperform. Measure-first — never gated. (Only
    # populated once trades opened after the TD-tagging change accumulate.)
    if any(p.get('td_signal') for p in closed):
        _show('TD signal', lambda p: p.get('td_signal'))


def main():
    arms = [a for a in [
        _arm('Aggressive funding', 'funding_arb_state.json', executable=False, borrow_correct=True),
        _arm('Majors funding',     'funding_arb_majors_state.json', executable=False, borrow_correct=False),
        _arm('Kraken funding',     'funding_arb_kraken_state.json', executable=True, borrow_correct=False),
        _swing_forward(),
        _tsmom_forward(),
        _tsmom_fast_forward(),
        _conf_forward(),
        _btc_trend_forward(),
        _kelly_trend_forward(),
        _tsmom_ls_forward(),
        _brain_forward(),
        _regime_forward(),
        _microstructure_forward(),
        _lev_perp_forward(),
        _lev_perp_v2_forward(),
        _lev_perp_agg_variant('lev_perp_agg_state.json',
                              'Leveraged perp 5x vol-tgt3 + take-profit (FORWARD, paper)'),
        _lev_perp_agg_variant('lev_perp_v2_agg_state.json',
                              'Leveraged perp 5x vol-tgt3 + ATR trail (FORWARD, paper)'),
        _pairs_forward(),
        _rebalance_forward(),
        _directional(),
    ] if a]

    k = len(arms)
    t_family = _family_t_bar(k)
    # Deflated-Sharpe benchmark: expected max per-trade Sharpe across the arms we
    # actually tried (n>=2). A real edge's Sharpe must beat THIS, not zero.
    sr0 = _expected_max_sharpe([a['sharpe'] for a in arms if a['n'] >= 2])
    dsr_of = lambda a: _deflated_sharpe(a['sharpe'], a.get('eff_n', a['n']),
                                        a.get('skew', 0.0), a.get('kurt', 3.0), sr0)
    print('=' * 78)
    print(f'PROOF SCORECARD  (bar: executable & n>={N_MIN} & expectancy>0 & t>{T_MIN})')
    print(f'  family-wise bar (Šidák, {k} arms judged): clustered t must exceed '
          f'{t_family:.2f} to be PROVEN — guards against best-of-{k} selection bias')
    print(f'  deflated-Sharpe bar: DSR>{DSR_MIN:.2f} vs expected-max-Sharpe sr0={sr0:.3f} '
          f'(corrects for {k} trials + non-normal returns)')
    print('=' * 78)
    for a in arms:
        print(f"\n▌ {a['label']}   [{'EXECUTABLE' if a['executable'] else 'FANTASY'}]")
        print(f"   trades={a['n']:<4} net=${a['total']:+8.2f}  win={a['win_rate']*100:4.0f}%  "
              f"exp=${a['expectancy']:+.4f}/trade")
        _tc = a.get('t_clustered', a['t_stat'])
        _en = a.get('eff_n', a['n'])
        _cline = (f"  clustered_t={_tc:5.2f} (eff_n≈{_en:.0f})"
                  if abs(_en - a['n']) > 0.5 else "")
        print(f"   t-stat={a['t_stat']:5.2f}{_cline}  sharpe(per-trade)={a['sharpe']:5.2f}  "
              f"maxDD=${a['max_dd']:+.2f}")
        _dsr = dsr_of(a)
        print(f"   DSR={_dsr:.2f} {'✓' if _dsr > DSR_MIN else '✗'} (skew={a.get('skew',0.0):+.2f} "
              f"kurt={a.get('kurt',3.0):.1f})")
        if 'corrected_total' in a:
            print(f"   borrow-corrected net=${a['corrected_total']:+8.2f}  "
                  f"(unpaid-carry illusion = ${a['total'] - a['corrected_total']:+.2f})")
        print(f"   → {_verdict(a, t_family, k)}")

    _swing_attribution()

    # Robustly proven = clears the pre-registered family-wise bar ('PROVEN ✓') AND
    # the Deflated Sharpe bar (multiple-testing + non-normality). DSR is reported
    # for every arm above; it tightens the final tally without moving the
    # pre-registered bar itself.
    proven = [a for a in arms if _verdict(a, t_family, k).startswith('PROVEN ✓')]
    proven_dsr = [a for a in proven if dsr_of(a) > DSR_MIN]
    print('\n' + '=' * 78)
    if proven_dsr:
        print('VERDICT: ' + ', '.join(a['label'] for a in proven_dsr)
              + ' cleared the bar (family-wise t AND DSR).')
    elif proven:
        print('VERDICT: ' + ', '.join(a['label'] for a in proven)
              + ' cleared t but NOT the deflated-Sharpe bar — not robust yet.')
    else:
        print('VERDICT: NO strategy has cleared the proof bar. Do not fund any of them yet.')
    print('=' * 78)


if __name__ == '__main__':
    main()

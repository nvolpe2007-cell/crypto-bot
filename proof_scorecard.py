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
                    t_stat=0.0, t_clustered=0.0, eff_n=0.0, sharpe=0.0, max_dd=0.0)
    wins = sum(1 for x in nets if x > 0)
    mean = total / n
    sd = st.pstdev(nets) if n < 2 else st.stdev(nets)
    t_stat = (mean / (sd / math.sqrt(n))) if (n >= 2 and sd > 0) else 0.0
    eff_n = _design_effect_eff_n(nets, clusters) if clusters else float(n)
    t_clustered = (mean / (sd / math.sqrt(eff_n))) if (eff_n >= 2 and sd > 0) else 0.0
    sharpe = (mean / sd) if sd > 0 else 0.0          # per-trade Sharpe (not annualised)
    # max drawdown on the cumulative equity curve
    cum = peak = dd = 0.0
    for x in nets:
        cum += x
        peak = max(peak, cum)
        dd = min(dd, cum - peak)
    return dict(n=n, total=total, win_rate=wins / n, expectancy=mean,
                t_stat=t_stat, t_clustered=t_clustered, eff_n=eff_n,
                sharpe=sharpe, max_dd=dd)


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


def _verdict(a: dict) -> str:
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
    return (f'PROVEN ✓ exp={a["expectancy"]:+.4f}/trade  '
            f'clustered t={t:.2f} (eff_n≈{a.get("eff_n", a["n"]):.0f})')


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


def main():
    arms = [a for a in [
        _arm('Aggressive funding', 'funding_arb_state.json', executable=False, borrow_correct=True),
        _arm('Majors funding',     'funding_arb_majors_state.json', executable=False, borrow_correct=False),
        _arm('Kraken funding',     'funding_arb_kraken_state.json', executable=True, borrow_correct=False),
        _swing_forward(),
        _directional(),
    ] if a]

    print('=' * 78)
    print(f'PROOF SCORECARD  (bar: executable & n>={N_MIN} & expectancy>0 & t>{T_MIN})')
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
        if 'corrected_total' in a:
            print(f"   borrow-corrected net=${a['corrected_total']:+8.2f}  "
                  f"(unpaid-carry illusion = ${a['total'] - a['corrected_total']:+.2f})")
        print(f"   → {_verdict(a)}")

    _swing_attribution()

    proven = [a for a in arms if _verdict(a).startswith('PROVEN')]
    print('\n' + '=' * 78)
    if proven:
        print('VERDICT: ' + ', '.join(a['label'] for a in proven) + ' cleared the bar.')
    else:
        print('VERDICT: NO strategy has cleared the proof bar. Do not fund any of them yet.')
    print('=' * 78)


if __name__ == '__main__':
    main()

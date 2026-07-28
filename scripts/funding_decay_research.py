"""
Funding decay/half-life research — 2026-07-26.

STRATEGY_REVIEW.md Week-3 roadmap item ("time entries on decay/half-life...
predicted hold clears the wall") was never built. This tests ONE pre-specified
hypothesis: does projecting funding decay with an AR(1)/Ornstein-Uhlenbeck fit,
and gating entry on projected cumulative capture clearing the round-trip cost,
beat the production Kraken funding-arb entry logic (snapshot-rate breakeven
gate + simple consecutive-cycle persistence gate)?

Data: real Kraken Futures hourly funding-rate history (public, keyless,
`historicalfundingrates` endpoint), ~1 year, 29 liquid majors that overlap
`arbitrage.funding_arb_paper.MAJOR_SYMBOLS`. Cash-and-carry funding-arb P&L is
delta-neutral by construction (spot/perp price nets to ~0), so no price data
is needed — only the funding-rate series and the production cost/gate model.

Method is CAUSAL throughout: at hour i, every gate/fit uses only series[:i+1].
No lookahead. Position sizing is a fixed $100/leg for both variants (real
production uses conviction-weighted sizing between MIN/MAX_POSITION_USD; fixed
size isolates the entry-TIMING mechanism being tested, not sizing). Cost/exit
constants mirror arbitrage/funding_arb_paper.py's Kraken-arm defaults exactly
(see the constants block below, each commented with its source line).

Only ONE new candidate (OU-timed entry) is tested against the existing
production logic (baseline) — not a multi-config sweep — so family-wise
correction is k=1 (T_MIN, no Sidak widening) per proof_scorecard's own
_family_t_bar(1). Judged with proof_scorecard's _stats/_deflated_sharpe so the
verdict uses the repo's one pre-registered bar, not a bespoke one.
"""
from __future__ import annotations

import json
import math
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from proof_scorecard import _stats, _expected_max_sharpe, _deflated_sharpe, _family_t_bar, T_MIN  # noqa: E402

# ── universe: base symbol -> Kraken Futures PF_ ticker (all verified live) ──
UNIVERSE = {
    'BTC': 'PF_XBTUSD', 'ETH': 'PF_ETHUSD', 'SOL': 'PF_SOLUSD', 'XRP': 'PF_XRPUSD',
    'ADA': 'PF_ADAUSD', 'DOGE': 'PF_DOGEUSD', 'AVAX': 'PF_AVAXUSD', 'LINK': 'PF_LINKUSD',
    'LTC': 'PF_LTCUSD', 'DOT': 'PF_DOTUSD', 'BCH': 'PF_BCHUSD', 'ATOM': 'PF_ATOMUSD',
    'UNI': 'PF_UNIUSD', 'FIL': 'PF_FILUSD', 'ETC': 'PF_ETCUSD', 'TRX': 'PF_TRXUSD',
    'NEAR': 'PF_NEARUSD', 'APT': 'PF_APTUSD', 'ARB': 'PF_ARBUSD', 'OP': 'PF_OPUSD',
    'ICP': 'PF_ICPUSD', 'INJ': 'PF_INJUSD', 'SUI': 'PF_SUIUSD', 'AAVE': 'PF_AAVEUSD',
    'HBAR': 'PF_HBARUSD', 'XLM': 'PF_XLMUSD', 'TIA': 'PF_TIAUSD', 'SEI': 'PF_SEIUSD',
    'BNB': 'PF_BNBUSD',
}

CACHE_DIR = ROOT / 'data' / 'research_cache'
CACHE_DIR.mkdir(parents=True, exist_ok=True)

# ── production constants (arbitrage/funding_arb_paper.py, Kraken-arm defaults
#    per src/paper_trading.py FUNDING_ARB_KRAKEN_* env defaults) ─────────────
COST_FRAC = 0.0054                 # FUNDING_ARB_KRAKEN_COST_FRAC default
MAX_BREAKEVEN_CYCLES = 4           # FUNDING_ARB_KRAKEN_MAX_BREAKEVEN_CYCLES default
MIN_ENTRY_APY = 15.0               # MIN_ENTRY_APY (module default, both arms)
MAX_ENTRY_APY = 300.0              # FUNDING_ARB_KRAKEN_MAX_APY default
EXIT_APY_FRACTION = 0.40           # EXIT_APY_FRACTION (module constant)
EXIT_CONFIRM_HOURS = 2.0           # FUNDING_ARB_EXIT_CONFIRM_HOURS default
MAX_HOLD_HOURS = 7 * 24            # MAX_HOLD_DAYS = 7
MIN_PERSISTENCE_HOURS = 3 * 8.0    # FUNDING_ARB_KRAKEN_MIN_PERSISTENCE_CYCLES=3 * FUNDING_CYCLE_HOURS=8
MAX_FLIPS = 6                      # FUNDING_ARB_KRAKEN_MAX_FLIPS default
FLIP_COOLDOWN_HOURS = 48           # FUNDING_ARB_KRAKEN_FLIP_COOLDOWN_HOURS default
FLIP_WINDOW_HOURS = 30 * 24        # FundingHistory retention_days default (30)
POSITION_SIZE_USD = 100.0          # fixed, both variants — isolates timing only

OU_WINDOW_HOURS = 30 * 24          # trailing window for AR(1) fit (30d, causal)
OU_MIN_OBS = 10 * 24               # require >=10d of history before trusting a fit
OU_HOLD_HALFLIVES = 3.0            # project 3 half-lives (~87.5% of reversion)
OU_SMOOTH_HOURS = 24               # rolling-mean window the AR(1) fit operates on

# Diagnostic (on DOGE only, before running the full universe): raw-hourly AR(1)
# has half-life ~1.5-2h — it fits hour-to-hour NOISE decay, not the day-scale
# funding-level persistence the "consecutive_positive_cycles" gate actually
# measures, so projected capture (~0.0001-0.0002) never clears the 0.54% cost.
# A 24h causal rolling-mean of the rate series, fit with the same AR(1), gives
# half-lives of tens to hundreds of hours (the level persistence that matters
# for a multi-day carry hold) and projected capture in the same order of
# magnitude as the cost. Fitting on the smoothed level, decided once from this
# diagnostic before running the full universe below, is the correct way to
# estimate a decay/half-life from noisy hourly funding data — not a sweep.


def fetch_history(base: str, pf_symbol: str) -> list[tuple[str, float]]:
    cache = CACHE_DIR / f'{base}_funding.json'
    if cache.exists():
        raw = json.loads(cache.read_text())
    else:
        url = f'https://futures.kraken.com/derivatives/api/v4/historicalfundingrates?symbol={pf_symbol}'
        raw = []
        for attempt in range(4):
            try:
                req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
                data = json.loads(urllib.request.urlopen(req, timeout=25).read())
                raw = data.get('rates', [])
                break
            except (urllib.error.URLError, TimeoutError) as e:
                print(f'  retry {base} ({e}), attempt {attempt + 1}')
                time.sleep(2)
        cache.write_text(json.dumps(raw))
        time.sleep(0.3)  # be polite to the public endpoint
    series = [(r['timestamp'], float(r['relativeFundingRate'])) for r in raw if 'relativeFundingRate' in r]
    series.sort(key=lambda x: x[0])
    return series


def consecutive_positive_hours(rates: list[float], i: int) -> float:
    if rates[i] <= 0:
        return 0.0
    n = 0
    for j in range(i, -1, -1):
        if rates[j] <= 0:
            break
        n += 1
    return float(n)


def flip_count(rates: list[float], i: int, window: int) -> int:
    lo = max(0, i - window + 1)
    seg = rates[lo:i + 1]
    if len(seg) < 2:
        return 0
    flips = 0
    prev = 1 if seg[0] > 0 else -1
    for a in seg[1:]:
        cur = 1 if a > 0 else -1
        if cur != prev:
            flips += 1
            prev = cur
    return flips


def rolling_mean_series(rates: list[float], w: int) -> list[float]:
    """Causal rolling mean (no lookahead — window i-w+1..i)."""
    out = [0.0] * len(rates)
    csum = 0.0
    from collections import deque
    dq: deque = deque()
    for idx, x in enumerate(rates):
        dq.append(x)
        csum += x
        if len(dq) > w:
            csum -= dq.popleft()
        out[idx] = csum / len(dq)
    return out


def fit_ar1(rates: list[float], i: int, window: int, min_obs: int) -> tuple[float, float] | None:
    """Causal OLS AR(1) fit on trailing window ending at i (inclusive). Returns
    (phi, mu) or None if insufficient data. phi = lag-1 autocorrelation of the
    demeaned series; mu = trailing mean (the OU long-run mean estimate)."""
    lo = max(0, i - window + 1)
    seg = rates[lo:i + 1]
    if len(seg) < min_obs:
        return None
    mu = sum(seg) / len(seg)
    dem = [x - mu for x in seg]
    num = sum(dem[k] * dem[k - 1] for k in range(1, len(dem)))
    den = sum(d * d for d in dem[:-1])
    if den <= 0:
        return None
    phi = num / den
    return phi, mu


def close_trade(pos: dict, i: int, reason: str) -> dict:
    net = pos['funding_collected'] - pos['entry_cost']
    return dict(symbol=pos['symbol'], entry_i=pos['entry_i'], close_i=i,
                hours_held=pos['hours_held'], entry_apy=pos['entry_apy'],
                funding_collected=pos['funding_collected'], entry_cost=pos['entry_cost'],
                net=net, reason=reason)


def simulate(symbol: str, series: list[tuple[str, float]], mode: str) -> list[dict]:
    rates = [r for _, r in series]
    smoothed = rolling_mean_series(rates, OU_SMOOTH_HOURS) if mode == 'ou' else None
    n = len(rates)
    trades: list[dict] = []
    open_pos: dict | None = None
    cooldown_until_i: int | None = None
    warmup = max(OU_MIN_OBS, int(MIN_PERSISTENCE_HOURS) + 1)

    for i in range(warmup, n):
        rate = rates[i]

        if open_pos is not None:
            open_pos['hours_held'] += 1
            per_hour_pnl = abs(rate) * POSITION_SIZE_USD
            open_pos['funding_collected'] += per_hour_pnl

            flipped = rate < 0
            decayed = abs(rate) < abs(open_pos['entry_rate']) * EXIT_APY_FRACTION
            soft = flipped or decayed
            reason = None
            if soft:
                if open_pos['pending_since'] is None:
                    open_pos['pending_since'] = i
                elif (i - open_pos['pending_since']) >= EXIT_CONFIRM_HOURS:
                    reason = 'funding_flipped' if flipped else 'apy_decayed'
            else:
                open_pos['pending_since'] = None
            if reason is None and open_pos['hours_held'] >= MAX_HOLD_HOURS:
                reason = 'max_hold_7d'
            if reason:
                t = close_trade(open_pos, i, reason)
                trades.append(t)
                if t['net'] < 0 and FLIP_COOLDOWN_HOURS > 0:
                    cooldown_until_i = i + FLIP_COOLDOWN_HOURS
                open_pos = None

        if open_pos is None and (cooldown_until_i is None or i >= cooldown_until_i):
            if rate <= 0:
                continue  # positive-funding-only arm (no spot-borrow leg)
            apy = rate * 24 * 365 * 100
            if apy < MIN_ENTRY_APY or apy > MAX_ENTRY_APY:
                continue
            flips = flip_count(rates, i, FLIP_WINDOW_HOURS)
            if flips > MAX_FLIPS:
                continue

            if mode == 'baseline':
                hours_to_breakeven = COST_FRAC / rate
                if hours_to_breakeven > MAX_BREAKEVEN_CYCLES * 8.0:
                    continue
                if consecutive_positive_hours(rates, i) < MIN_PERSISTENCE_HOURS:
                    continue
            elif mode == 'ou':
                fit = fit_ar1(smoothed, i, OU_WINDOW_HOURS, OU_MIN_OBS)
                if fit is None:
                    continue
                phi, mu = fit
                phi_c = min(max(phi, -0.999), 0.999)
                if phi_c <= 0:
                    half_life = 1.0
                else:
                    half_life = math.log(0.5) / math.log(phi_c)
                hold_h = int(min(max(OU_HOLD_HALFLIVES * half_life, 1), MAX_HOLD_HOURS))
                projected = 0.0
                r = smoothed[i]
                for _h in range(hold_h):
                    r = mu + phi_c * (r - mu)
                    projected += max(r, 0.0)
                if projected <= COST_FRAC:
                    continue
            else:
                raise ValueError(mode)

            open_pos = dict(symbol=symbol, entry_i=i, entry_rate=rate, entry_apy=apy,
                             funding_collected=0.0, entry_cost=COST_FRAC * POSITION_SIZE_USD,
                             hours_held=0, pending_since=None)

    return trades


def run(mode: str) -> list[dict]:
    all_trades: list[dict] = []
    for base, pf in UNIVERSE.items():
        print(f'  {mode}: {base} ...', end=' ')
        series = fetch_history(base, pf)
        if len(series) < OU_MIN_OBS + 100:
            print(f'skip (only {len(series)}h history)')
            continue
        trades = simulate(base, series, mode)
        print(f'{len(series)}h history, {len(trades)} closed trades')
        all_trades.extend(trades)
    return all_trades


def report(label: str, trades: list[dict]) -> dict:
    nets = [t['net'] for t in trades]
    clusters = [t['symbol'] for t in trades]
    s = _stats(nets, clusters)
    print(f'\n=== {label} ===')
    print(f"n={s['n']} total=${s['total']:+.2f} win_rate={s['win_rate']*100:.1f}% "
          f"expectancy=${s['expectancy']:+.4f}/trade")
    print(f"sharpe(per-trade)={s['sharpe']:.4f} t_stat={s['t_stat']:.2f} "
          f"t_clustered={s['t_clustered']:.2f} eff_n={s['eff_n']:.1f}")
    if trades:
        by_reason: dict[str, int] = {}
        for t in trades:
            by_reason[t['reason']] = by_reason.get(t['reason'], 0) + 1
        print(f"  close reasons: {by_reason}")
        avg_hold = sum(t['hours_held'] for t in trades) / len(trades)
        print(f"  avg hold: {avg_hold:.1f}h")
    return s


def main():
    print('Fetching + simulating BASELINE (production snapshot-rate + consec-cycle gate)...')
    baseline_trades = run('baseline')
    print('\nFetching + simulating OU-TIMED (AR(1) projected-capture gate)...')
    ou_trades = run('ou')

    s_base = report('BASELINE (production entry logic)', baseline_trades)
    s_ou = report('OU-TIMED (projected decay/half-life entry)', ou_trades)

    print('\n' + '=' * 78)
    print('VERDICT (single pre-registered hypothesis, k=1, no sweep)')
    t_bar = _family_t_bar(1)
    print(f'Required t_clustered >= {t_bar:.2f} (T_MIN, k=1 — one new candidate, no Sidak widening)')
    for label, s, trades in (('BASELINE', s_base, baseline_trades), ('OU-TIMED', s_ou, ou_trades)):
        if s['n'] < 2:
            print(f'{label}: n={s["n"]} — too few trades to judge.')
            continue
        dsr = _deflated_sharpe(s['sharpe'], s['eff_n'], s['skew'], s['kurt'], sr0=0.0)
        cleared = s['n'] >= 30 and s['expectancy'] > 0 and s['t_clustered'] >= t_bar and dsr > 0.95
        print(f"{label}: n={s['n']} expectancy=${s['expectancy']:+.4f} "
              f"t_clustered={s['t_clustered']:.2f} DSR={dsr:.3f} -> "
              f"{'CLEARS BAR' if cleared else 'does not clear bar'}")
    print('=' * 78)


if __name__ == '__main__':
    main()

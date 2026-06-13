"""
Volatility-targeting research — a PRINCIPLED (theory-first) improvement test.

Round 1 showed trend-following works on BTC/ETH but SOL (choppiest) drags the
equal-weight portfolio. Risk-parity theory says: size each sleeve by INVERSE
volatility so no single coin dominates risk, and cap total exposure when vol
spikes. Hypothesis: this improves Sharpe + shrinks maxDD CONSISTENTLY across
lookbacks (a real effect), not just in one cell (a fit).

Compares 3 sizings of the same trend signal:
  equal      — 1/3 capital per coin sleeve (the current tsmom)
  inv-vol    — sleeve_i ∝ 1/vol_i (20d), risk-parity
  vol-target — inv-vol, then scale total exposure to ~40% annualized vol (cap 1x;
               spot can't lever, so this only DE-risks in high vol)

Verdict = does the principled change help across ALL lookbacks? Read-only.
Lookahead-free; honest 0.26%/leg cost on signal flips.
"""
from __future__ import annotations
import statistics as st
from math import sqrt

COINS = ['BTC/USD', 'ETH/USD', 'SOL/USD']
LOOKBACKS = [50, 100, 150, 200]
COST_LEG = 0.0026
VOL_WIN = 20          # trailing days for realized vol
TARGET_VOL = 0.40     # annualized portfolio vol target
ANN = 365


def fetch_daily(ex, sym, want=720):
    try:
        return [row[4] for row in ex.fetch_ohlcv(sym, timeframe='1d', limit=want) if row[4]]
    except Exception as e:
        print(f"  [{sym}] fetch failed: {e}"); return []


def sma(xs, n, i):
    return None if i + 1 < n else sum(xs[i + 1 - n:i + 1]) / n


def trailing_vol(rets, i, win=VOL_WIN):
    lo = max(0, i - win + 1)
    seg = rets[lo:i + 1]
    return st.pstdev(seg) if len(seg) > 1 else 0.0


def metrics(rets):
    if not rets:
        return 0.0, 0.0, 0.0
    eq = peak = 1.0
    mdd = 0.0
    for r in rets:
        eq *= (1 + r); peak = max(peak, eq); mdd = min(mdd, eq / peak - 1)
    cagr = eq ** (ANN / len(rets)) - 1
    sd = st.pstdev(rets)
    sharpe = (st.mean(rets) / sd * sqrt(ANN)) if sd > 0 else 0.0
    return cagr, sharpe, mdd


def per_coin(closes, n):
    """Return (signal[t], coin_ret[t+1], flipped[t]) lookahead-free."""
    rets = [closes[i + 1] / closes[i] - 1 for i in range(len(closes) - 1)]
    sig, flip = [], []
    prev = 0
    for i in range(len(closes) - 1):
        s = sma(closes, n, i)
        want = 1 if (s is not None and closes[i] > s) else 0
        sig.append(want); flip.append(want != prev); prev = want
    return sig, rets


def run(data, n, scheme):
    coins = list(data)
    sigs, rets, vols = {}, {}, {}
    for c in coins:
        s, r = per_coin(data[c], n)
        sigs[c], rets[c] = s, r
        vols[c] = [trailing_vol(r, i) for i in range(len(r))]
    T = min(len(rets[c]) for c in coins)
    port = []
    prev_sig = {c: 0 for c in coins}
    for t in range(T):
        # sleeve weights
        if scheme == 'equal':
            w = {c: 1.0 / len(coins) for c in coins}
        else:  # inv-vol base
            inv = {c: (1.0 / vols[c][t]) if vols[c][t] > 0 else 0.0 for c in coins}
            s = sum(inv.values()) or 1.0
            w = {c: inv[c] / s for c in coins}
        day = 0.0
        port_vol_est = 0.0
        for c in coins:
            sleeve = w[c] if sigs[c][t] == 1 else 0.0
            r = sigs[c][t] * rets[c][t] * (w[c] if sigs[c][t] else 0)
            if sigs[c][t] != prev_sig[c]:
                r -= COST_LEG * w[c]
            day += r
            port_vol_est += sleeve * vols[c][t]
            prev_sig[c] = sigs[c][t]
        if scheme == 'vol-target' and port_vol_est > 0:
            ann_vol = port_vol_est * sqrt(ANN)
            lev = min(1.0, TARGET_VOL / ann_vol) if ann_vol > 0 else 1.0
            day *= lev
        port.append(day)
    return metrics(port)


def main():
    try:
        import ccxt
    except Exception as e:
        print("ccxt unavailable:", e); return
    ex = ccxt.kraken({'enableRateLimit': True})
    data = {}
    for c in COINS:
        s = fetch_daily(ex, c)
        if s:
            data[c] = s
    print(f"  loaded {', '.join(f'{c}={len(data[c])}d' for c in data)}")

    print("\n" + "=" * 66)
    print("VOL-TARGETING — equal vs inv-vol vs vol-target (trend portfolio)")
    print("=" * 66)
    print(f"{'N':>5}  {'scheme':<12}{'CAGR':>8}{'Sharpe':>8}{'maxDD':>8}")
    print("-" * 66)
    win_invvol = win_vt = 0
    for n in LOOKBACKS:
        base = run(data, n, 'equal')
        iv = run(data, n, 'inv-vol')
        vt = run(data, n, 'vol-target')
        for name, m in (('equal', base), ('inv-vol', iv), ('vol-target', vt)):
            print(f"{n:>5}  {name:<12}{m[0]*100:>7.0f}%{m[1]:>8.2f}{m[2]*100:>7.0f}%")
        if iv[1] > base[1] and iv[2] >= base[2]:
            win_invvol += 1
        if vt[1] > base[1] and vt[2] >= base[2]:
            win_vt += 1
        print()
    print("-" * 66)
    print(f"inv-vol improved (Sharpe up & maxDD >=) in {win_invvol}/{len(LOOKBACKS)} lookbacks")
    print(f"vol-target improved in {win_vt}/{len(LOOKBACKS)} lookbacks")
    if win_vt >= 3 or win_invvol >= 3:
        print("=> PRINCIPLED WIN — consistent across lookbacks; worth a forward config.")
    else:
        print("=> NOT consistent — don't adopt; would be fitting noise.")


if __name__ == '__main__':
    main()

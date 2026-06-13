"""
Volatility-regime timing — a FRESH executable (long-only spot) idea.

Theory: crashes cluster in high-vol regimes; staying long only when vol is calm
should sidestep the worst drawdowns. Unlike trend (waits for a bull), this trades
in more regimes. Long-only-spot-executable on Kraken majors.

Rule: hold the BTC/ETH/SOL basket when a coin's 20d realized vol is BELOW its
rolling median (calm), cash when ABOVE (turbulent). Lookahead-free, honest cost.
Tested across vol/median windows + the INVERSE (sanity: a real signal beats its
inverse). Verdict = does calm-vol-long beat buy-&-hold on Sharpe AND maxDD
robustly across params? Read-only.
"""
from __future__ import annotations
import statistics as st
from math import sqrt

COINS = ['BTC/USD', 'ETH/USD', 'SOL/USD']
COST_LEG = 0.0026
ANN = 365


def fetch(ex, sym, want=720):
    try:
        return [r[4] for r in ex.fetch_ohlcv(sym, timeframe='1d', limit=want) if r[4]]
    except Exception:
        return []


def metrics(rets):
    if not rets:
        return 0.0, 0.0, 0.0
    eq = peak = 1.0; mdd = 0.0
    for r in rets:
        eq *= (1 + r); peak = max(peak, eq); mdd = min(mdd, eq / peak - 1)
    cagr = eq ** (ANN / len(rets)) - 1
    sd = st.pstdev(rets)
    return cagr, ((st.mean(rets) / sd * sqrt(ANN)) if sd > 0 else 0.0), mdd


def realized_vol(rets, i, win):
    seg = rets[max(0, i - win + 1):i + 1]
    return st.pstdev(seg) if len(seg) > 1 else 0.0


def basket_bh(data):
    coins = list(data)
    rets = {c: [data[c][i + 1] / data[c][i] - 1 for i in range(len(data[c]) - 1)] for c in coins}
    T = min(len(rets[c]) for c in coins)
    port = [st.mean([rets[c][-T:][t] for c in coins]) for t in range(T)]
    return metrics(port)


def regime(data, vol_win, med_win, inverse=False):
    coins = list(data)
    rets = {c: [data[c][i + 1] / data[c][i] - 1 for i in range(len(data[c]) - 1)] for c in coins}
    vol = {c: [realized_vol(rets[c], i, vol_win) for i in range(len(rets[c]))] for c in coins}
    T = min(len(rets[c]) for c in coins)
    prev = {c: 0 for c in coins}
    port = []
    for t in range(T):
        day = 0.0
        for c in coins:
            v = vol[c][-T:][t]
            seg = vol[c][-T:][max(0, t - med_win + 1):t + 1]
            med = st.median(seg) if seg else 0.0
            calm = v <= med
            want = (calm if not inverse else (not calm)) and v > 0
            r = (rets[c][-T:][t] / len(coins)) if want else 0.0
            if int(want) != prev[c]:
                r -= COST_LEG / len(coins)
            day += r
            prev[c] = int(want)
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
        s = fetch(ex, c)
        if s:
            data[c] = s
    if not data:
        print("no data"); return
    bh = basket_bh(data)
    print("=" * 62)
    print("VOL-REGIME TIMING — calm-vol-long vs buy-&-hold (BTC/ETH/SOL)")
    print("=" * 62)
    print(f"buy & hold basket:  CAGR={bh[0]*100:>5.0f}%  Sharpe={bh[1]:>5.2f}  maxDD={bh[2]*100:>4.0f}%\n")
    print(f"{'vol_win':>8}{'med_win':>8}{'CAGR':>8}{'Sharpe':>8}{'maxDD':>8}{'  vs B&H':>10}")
    print("-" * 62)
    wins = 0; cells = 0
    for vw in (14, 20, 30):
        for mw in (60, 90):
            cg, sh, dd = regime(data, vw, mw)
            cells += 1
            won = sh > bh[1] and dd > bh[2]
            wins += won
            print(f"{vw:>8}{mw:>8}{cg*100:>7.0f}%{sh:>8.2f}{dd*100:>7.0f}%"
                  f"{('  beats' if won else '  -'):>10}")
    # sanity: inverse should be WORSE if the signal is real
    inv = regime(data, 20, 90, inverse=True)
    print(f"\n  inverse (vol-HIGH-long, 20/90): CAGR={inv[0]*100:.0f}% Sharpe={inv[1]:.2f} "
          f"(should be worse than calm-long if real)")
    print("-" * 62)
    print(f"calm-vol-long beats B&H (Sharpe AND maxDD) in {wins}/{cells} param cells.")
    print("=> ROBUST candidate" if wins >= cells * 0.7 else
          "=> MIXED — fragile" if wins >= cells * 0.4 else
          "=> NO edge — don't pursue")


if __name__ == '__main__':
    main()

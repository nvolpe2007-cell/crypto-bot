"""
Fee re-pricing study — does a cheaper venue actually revive any of our strategies?

The whole repo history says "cost dominates." So before chasing a low-fee venue
(Hyperliquid — which is geoblocked for US persons anyway), answer the cheap
question first: take a spread of strategies across the trading-frequency spectrum
and re-price each at several round-trip fee levels. The verdict per strategy:

  • profitable already at the CURRENT ~0.54% taker?            → fee isn't the issue
  • only profitable BELOW some fee → is that fee LEGALLY reachable?
        - breakeven >= ~0.20%  → Kraken MAKER (limit orders) revives it — LEGAL, today
        - breakeven  0.045-0.20% → only Hyperliquid-cheap revives it — NOT legal for US
  • loses even at 0.045% (Hyperliquid)?                        → EDGE-DEAD, no fee saves it

Also compares each to buy & hold, so "positive" isn't mistaken for "has an edge".
Read-only; Kraken public OHLC; BTC/ETH/SOL averaged.

    python scripts/fee_repricing.py
"""
from __future__ import annotations
import json
import urllib.request
import numpy as np

START = 1000.0
PAIRS = {"BTC": "XBTUSD", "ETH": "ETHUSD", "SOL": "SOLUSD"}
# round-trip fee levels to test (per-leg = half)
FEES_RT = [0.00045, 0.0010, 0.0020, 0.0030, 0.0054]   # HL / cheap-maker / Kraken-maker / mid / taker
KRAKEN_MAKER_RT = 0.0020        # ~ the best LEGAL round-trip we can realistically make on Kraken
HL_RT = 0.00045                 # Hyperliquid taker (reference only — geoblocked for US)


def closes(pair: str) -> np.ndarray:
    url = f"https://api.kraken.com/0/public/OHLC?pair={pair}&interval=1440"
    d = json.loads(urllib.request.urlopen(url, timeout=30).read())
    s = next(v for k, v in d["result"].items() if k != "last")
    return np.array([float(r[4]) for r in s])[:-1]


def sma(a, n):
    out = np.full(len(a), np.nan)
    if len(a) >= n:
        c = np.cumsum(np.insert(a, 0, 0.0)); out[n - 1:] = (c[n:] - c[:-n]) / n
    return out


def rsi(a, n=2):
    d = np.diff(a, prepend=a[0]); up = np.clip(d, 0, None); dn = -np.clip(d, None, 0)
    ru = np.convolve(up, np.ones(n) / n, mode="full")[:len(a)]
    rd = np.convolve(dn, np.ones(n) / n, mode="full")[:len(a)]
    rs = ru / np.where(rd == 0, np.nan, rd)
    return np.nan_to_num(100 - 100 / (1 + rs), nan=50.0)


def strategies(c: np.ndarray) -> dict:
    """Each returns a position series in [-1,1] (long-only use [0,1]). Lookahead-free."""
    s10, s20, s30, s50, s100 = (sma(c, n) for n in (10, 20, 30, 50, 100))
    out = {}
    out["trend_slow_long (SMA100, ~rare)"] = np.nan_to_num((c > s100).astype(float))
    out["trend_fast_long (SMA20)"] = np.nan_to_num((c > s20).astype(float))
    out["cross_long_10_30"] = np.nan_to_num((s10 > s30).astype(float))
    # donchian-20 breakout long/flat
    hi = np.array([np.max(c[max(0, i - 20):i]) if i else c[0] for i in range(len(c))])
    lo = np.array([np.min(c[max(0, i - 20):i]) if i else c[0] for i in range(len(c))])
    dpos = np.where(c >= hi, 1.0, np.where(c <= lo, 0.0, np.nan))
    out["donchian20_breakout_long"] = np.nan_to_num(np_ffill(dpos))
    # RSI(2) mean-reversion long (classic HIGH-frequency, fee-sensitive)
    r = rsi(c, 2); mr = np.where(r < 10, 1.0, np.where(c > s10, 0.0, np.nan))
    out["rsi2_meanrev_long (high-freq)"] = np.nan_to_num(np_ffill(mr))
    # long/SHORT fast trend (the shorting "prize")
    out["trend_fast_LS (SMA50, long/short)"] = np.where(c > s50, 1.0, -1.0)
    return out


def np_ffill(a):
    a = a.copy(); idx = np.where(~np.isnan(a))[0]
    if len(idx) == 0:
        return np.zeros_like(a)
    a[: idx[0]] = 0.0
    for i in range(1, len(a)):
        if np.isnan(a[i]):
            a[i] = a[i - 1]
    return a


def run(c, pos, fee_rt):
    held = np.roll(pos, 1); held[0] = 0.0
    ret = np.diff(c, prepend=c[0]) / np.roll(c, 1); ret[0] = 0.0
    turn = np.abs(np.diff(held, prepend=0.0))
    net = held * ret - turn * (fee_rt / 2.0)
    eq = START * np.cumprod(1 + net)
    trades = int((turn > 1e-9).sum())
    return eq[-1], trades


def breakeven_rt_avg(data, name):
    """Largest round-trip fee at which the AVG-across-coins book still ends >= START.
    None => the average loses even at zero fee (edge-dead). Return is monotonic in
    fee, so scanning upward until the first losing fee gives the breakeven."""
    best = None
    for fee in np.linspace(0.0, 0.012, 121):
        fins = [run(c, strategies(c)[name], fee)[0] for c in data.values()]
        if np.mean(fins) >= START:
            best = fee
        else:
            break
    return best


def main():
    data = {k: closes(p) for k, p in PAIRS.items()}
    days = min(len(v) for v in data.values())
    yrs = days / 365.0
    bh = {k: (v[-1] / v[100] - 1) * 100 for k, v in data.items()}   # rough B&H over window
    bh_avg = np.mean(list(bh.values()))

    print("=" * 100)
    print(f"FEE RE-PRICING — BTC/ETH/SOL daily (~{days}d / {yrs:.1f}y). $1000 each, averaged. "
          f"Buy&hold avg: {bh_avg:+.0f}%")
    print(f"Legal Kraken maker ~{KRAKEN_MAKER_RT*100:.2f}% RT | current taker 0.54% | "
          f"Hyperliquid {HL_RT*100:.3f}% (US-geoblocked, reference only)")
    print("=" * 100)
    hdr = f"{'strategy':<36}{'tr/yr':>6}" + "".join(f"{f'{f*100:.2f}%':>9}" for f in FEES_RT) + f"{'breakeven':>11}  verdict"
    print(hdr); print("-" * 100)

    for name in strategies(data["BTC"]):
        per_fee = {f: np.mean([run(c, strategies(c)[name], f)[0] for c in data.values()])
                   for f in FEES_RT}
        tr_yr = np.mean([run(c, strategies(c)[name], 0.0054)[1] for c in data.values()]) / yrs
        be = breakeven_rt_avg(data, name)
        ret_taker = (per_fee[0.0054] / START - 1) * 100

        row = f"{name:<36}{tr_yr:>6.0f}"
        for f in FEES_RT:
            row += f"{(per_fee[f]/START-1)*100:>8.0f}%"
        if be is None:
            be_s, verdict = "  none", "EDGE-DEAD (avg loses even at zero fee) — no venue saves it"
        else:
            be_s = f"{be*100:>9.2f}%"
            if ret_taker > 0:
                verdict = "profitable at CURRENT 0.54% taker — fee is NOT the blocker"
            elif be >= KRAKEN_MAKER_RT:
                verdict = "revives at LEGAL Kraken maker (limit orders)"
            elif be >= HL_RT:
                verdict = "needs sub-maker fees → only Hyperliquid (NOT legal for US)"
            else:
                verdict = "needs near-zero fees — unreachable"
        print(f"{row}{be_s:>11}  {verdict}")
    print("-" * 100)
    print("READ: 'EDGE-DEAD' = cost was never the only problem; a cheaper venue changes nothing.")
    print("      'revives at LEGAL Kraken maker' = worth a maker-only execution mode (no venue change).")
    print("      'only Hyperliquid' = the edge needs a venue you can't legally use — drop it.")


if __name__ == "__main__":
    main()

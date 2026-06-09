#!/usr/bin/env python3
"""
THESIS TEST: time-series momentum / trend-following ALLOCATION on majors.

Different return source than swing. The cost wall (0.54% round-trip) is fatal for
frequent small trades; it is NEGLIGIBLE for a low-turnover allocation that rides
multi-month trends (a few switches/year -> ~0.1%/yr cost drag). Classic TSMOM
(Moskowitz-Ooi-Pedersen 2012; the "200-day MA" trend filter is decades old and
PRE-SPECIFIED here — NOT fitted to this data, which would be the overfit we avoid).

Spec (one, pre-committed):
  * Daily bars, ~5y (must include the 2022 bear — trend-following's value is
    sidestepping bears, so a bull-only sample would be a rigged test).
  * Signal at each daily close: LONG if close > SMA(200), else CASH.
  * Applied with a 1-bar lag (yesterday's signal drives today's return — no
    look-ahead). Cost 0.27%/side charged only when the position FLIPS.
  * Benchmark: buy-and-hold.

Success metric is RISK-ADJUSTED: Sharpe and especially max-drawdown / MAR
(CAGR/|maxDD|) vs buy-and-hold, with cost drag shown to be negligible. Raw return
need not beat B&H in a mostly-up sample; cutting drawdown materially is the win.
IN-SAMPLE screen, NOT proof.

    python tsmom_test.py
"""
from __future__ import annotations
import json, math, time, urllib.request
from datetime import datetime, timezone

SYMBOLS = ["BTC", "ETH", "SOL", "LTC", "BCH", "XRP"]
SMA_N = 200            # pre-specified trend filter (not swept)
COST_PER_SIDE = 0.0027 # 0.54% round-trip / 2


def fetch_daily(fsym: str, limit: int = 2000) -> list[dict]:
    url = (f"https://min-api.cryptocompare.com/data/v2/histoday"
           f"?fsym={fsym}&tsym=USD&limit={limit}")
    with urllib.request.urlopen(url, timeout=30) as r:
        d = json.loads(r.read())
    return [{"t": c["time"], "c": float(c["close"])}
            for c in d.get("Data", {}).get("Data", []) if c.get("close", 0) > 0]


def metrics(daily_rets: list[float]) -> dict:
    n = len(daily_rets)
    if n == 0:
        return dict(cagr=0, sharpe=0, maxdd=0, mar=0, total=0)
    eq = 1.0
    curve = [1.0]
    for r in daily_rets:
        eq *= (1 + r); curve.append(eq)
    years = n / 365.0
    cagr = eq ** (1 / years) - 1 if years > 0 and eq > 0 else -1
    mean = sum(daily_rets) / n
    sd = (sum((x - mean) ** 2 for x in daily_rets) / n) ** 0.5
    sharpe = (mean / sd * math.sqrt(365)) if sd > 0 else 0.0
    peak = dd = 0.0
    for v in curve:
        peak = max(peak, v); dd = min(dd, v / peak - 1)
    mar = cagr / abs(dd) if dd < 0 else 0.0
    return dict(cagr=cagr, sharpe=sharpe, maxdd=dd, mar=mar, total=eq - 1)


def run_symbol(bars: list[dict]) -> dict | None:
    if len(bars) < SMA_N + 30:
        return None
    closes = [b["c"] for b in bars]
    # position decided at close t (long if close>SMA200), applied to return t->t+1
    strat_rets, bh_rets = [], []
    prev_pos = 0
    switches = 0
    cost_drag = 0.0
    for t in range(SMA_N, len(closes) - 1):
        sma = sum(closes[t - SMA_N + 1:t + 1]) / SMA_N
        pos = 1 if closes[t] > sma else 0
        if pos != prev_pos:
            switches += 1
        r_next = closes[t + 1] / closes[t] - 1
        cost = COST_PER_SIDE if pos != prev_pos else 0.0
        cost_drag += cost
        strat_rets.append(pos * r_next - cost)
        bh_rets.append(r_next)
        prev_pos = pos
    s, b = metrics(strat_rets), metrics(bh_rets)
    return dict(n_days=len(strat_rets), switches=switches, cost_drag=cost_drag,
                strat=s, bh=b)


def main():
    print(f"Fetching ~5y daily (CryptoCompare) for {len(SYMBOLS)} majors...")
    data = {}
    for s in SYMBOLS:
        data[s] = fetch_daily(s); time.sleep(0.3)
        b = data[s]
        d0 = datetime.fromtimestamp(b[0]["t"], tz=timezone.utc).date() if b else "-"
        d1 = datetime.fromtimestamp(b[-1]["t"], tz=timezone.utc).date() if b else "-"
        print(f"  {s:<4} {len(b)} days  {d0} -> {d1}")

    print("\n" + "=" * 84)
    print(f"TREND-FOLLOWING (long > SMA{SMA_N}, else cash)  vs  BUY-AND-HOLD   "
          f"[in-sample screen]")
    print("=" * 84)
    print(f"{'sym':<5}{'yrs':>4} | {'TF CAGR':>8} {'TF Shrp':>7} {'TF maxDD':>8} "
          f"{'TF MAR':>6} | {'BH CAGR':>8} {'BH Shrp':>7} {'BH maxDD':>8} | "
          f"{'flips':>5} {'cost$':>6}")
    print("-" * 84)
    for s in SYMBOLS:
        r = run_symbol(data[s])
        if not r:
            print(f"{s:<5} (insufficient history)"); continue
        yrs = r["n_days"] / 365.0
        tf, bh = r["strat"], r["bh"]
        print(f"{s:<5}{yrs:>4.1f} | {tf['cagr']*100:>7.1f}% {tf['sharpe']:>7.2f} "
              f"{tf['maxdd']*100:>7.1f}% {tf['mar']:>6.2f} | "
              f"{bh['cagr']*100:>7.1f}% {bh['sharpe']:>7.2f} {bh['maxdd']*100:>7.1f}% | "
              f"{r['switches']:>5} {r['cost_drag']*100:>5.1f}%")
    print("-" * 84)
    print("Read: TF wins if it lifts Sharpe / shrinks maxDD (smaller |maxDD|, higher MAR)")
    print("vs B&H, with cost drag small. Raw CAGR < B&H in a bull sample is EXPECTED —")
    print("the edge is risk control, not more return.")


if __name__ == "__main__":
    main()

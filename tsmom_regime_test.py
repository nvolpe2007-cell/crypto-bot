#!/usr/bin/env python3
"""
DOES A REGIME FILTER IMPROVE THE TREND ALLOCATION? — head-to-head, honest cost.

Trend-following's one weakness is WHIPSAW in choppy/range regimes (price sawing
across the SMA, paying cost on every false flip). The fix is to only take longs
when price is actually MOVING EFFICIENTLY, not chopping. The correct zero-data
measure is Kaufman's EFFICIENCY RATIO (ER):

    ER(n) = |close_t - close_{t-n}|  /  sum_{i}|close_i - close_{i-1}|   over n days

ER -> 1 : straight-line move (trend).   ER -> 0 : lots of motion, no progress (chop).

Compares on ~5y daily (incl. 2022 bear), 0.54% round-trip, 1-bar lag (no lookahead):
  A) Buy & hold              (benchmark)
  B) SMA200 + 2% band        (the live tsmom_paper.py spec)
  C) SMA200 + 2% band + ER gate on ENTRY  (the new filter)

ER gates ENTRIES only (don't buy into chop); exits stay on the trend break, so a
confirmed trend is allowed to ride. ONE pre-specified threshold (ER_THRESH=0.30) —
0.20/0.40 shown only as ROBUSTNESS, NOT to select the best (we deploy 0.30 either
way). In-sample screen, NOT proof. Promote to the live runner ONLY if C beats B on
PORTFOLIO risk-adjusted terms (Sharpe / MAR) and/or cost drag, without gutting return.

    python tsmom_regime_test.py
"""
from __future__ import annotations
import time
from tsmom_test import fetch_daily, metrics, SYMBOLS, SMA_N, COST_PER_SIDE

ER_N = 20
BAND = 0.02
THRESHOLDS = [0.30, 0.20, 0.40]    # 0.30 is THE pre-committed deploy value


def efficiency_ratio(closes: list[float], n: int) -> float:
    if len(closes) < n + 1:
        return 0.0
    change = abs(closes[-1] - closes[-1 - n])
    path = sum(abs(closes[-i] - closes[-i - 1]) for i in range(1, n + 1))
    return change / path if path > 0 else 0.0


def simulate(closes: list[float], times: list[int], *, use_er: bool,
             er_thresh: float = 0.30) -> tuple[dict, int, float]:
    """Return {date_ts: strat_daily_return}, switches, cost_drag. Position decided
    at close t, applied to t->t+1 return; cost charged on flips (no lookahead)."""
    rets: dict[int, float] = {}
    long = False
    switches = 0
    cost_drag = 0.0
    start = max(SMA_N, ER_N)
    for t in range(start, len(closes) - 1):
        sma = sum(closes[t - SMA_N + 1:t + 1]) / SMA_N
        prev = long
        if long:
            if closes[t] < sma * (1 - BAND):
                long = False
        else:
            ok_trend = closes[t] > sma * (1 + BAND)
            ok_eff = (not use_er) or efficiency_ratio(closes[:t + 1], ER_N) > er_thresh
            if ok_trend and ok_eff:
                long = True
        cost = COST_PER_SIDE if long != prev else 0.0
        if long != prev:
            switches += 1
        cost_drag += cost
        r_next = closes[t + 1] / closes[t] - 1
        rets[times[t + 1]] = (1.0 if long else 0.0) * r_next - cost
    return rets, switches, cost_drag


def portfolio(per_symbol_rets: list[dict]) -> list[float]:
    """Equal-weight across symbols per date (average of whatever symbols traded)."""
    all_dates = sorted({d for r in per_symbol_rets for d in r})
    out = []
    for d in all_dates:
        vals = [r[d] for r in per_symbol_rets if d in r]
        if vals:
            out.append(sum(vals) / len(vals))
    return out


def line(name, m, switches=None, cost=None):
    extra = ""
    if switches is not None:
        extra = f" flips={switches:<3} cost={cost*100:4.1f}%"
    print(f"  {name:<26} CAGR={m['cagr']*100:+6.1f}%  Sharpe={m['sharpe']:+5.2f}  "
          f"maxDD={m['maxdd']*100:+6.1f}%  MAR={m['mar']:+5.2f}{extra}")


def main():
    print(f"Fetching ~5y daily for {SYMBOLS}...")
    data = {s: fetch_daily(s) for s in SYMBOLS}
    for s in SYMBOLS:
        time.sleep(0)  # already fetched; placeholder
    print(f"  done ({sum(len(v) for v in data.values())} bars)\n")

    def closes_times(s):
        return [b["c"] for b in data[s]], [b["t"] for b in data[s]]

    print("=" * 78)
    print("REGIME-FILTER TEST  (SMA200+band, +/- ER entry gate)  [in-sample screen]")
    print("=" * 78)

    # --- per-symbol, base (B) vs ER (C @ 0.30) ---
    base_rets, er_rets = [], []
    print("\nPER SYMBOL  (B = SMA200+band  vs  C = +ER@0.30):")
    for s in SYMBOLS:
        c, t = closes_times(s)
        br, bsw, bcost = simulate(c, t, use_er=False)
        er, esw, ecost = simulate(c, t, use_er=True, er_thresh=0.30)
        base_rets.append(br); er_rets.append(er)
        print(f" {s}")
        line("B SMA200+band", metrics(list(br.values())), bsw, bcost)
        line("C +ER@0.30", metrics(list(er.values())), esw, ecost)

    # --- PORTFOLIO (the verdict view): B&H vs B vs C across thresholds ---
    print("\n" + "-" * 78)
    print("PORTFOLIO (equal-weight BTC/ETH/SOL) — the verdict view:")
    bh = []
    for s in SYMBOLS:
        c, t = closes_times(s)
        bh.append({t[i + 1]: c[i + 1] / c[i] - 1 for i in range(len(c) - 1)})
    line("A buy & hold", metrics(portfolio(bh)))
    line("B SMA200+band", metrics(portfolio(base_rets)))
    for th in THRESHOLDS:
        rets = [simulate(*closes_times(s), use_er=True, er_thresh=th)[0] for s in SYMBOLS]
        tag = "C +ER@%.2f" % th + ("  <-- deploy value" if th == 0.30 else "  (robustness)")
        line(tag, metrics(portfolio(rets)))

    print("\nRead: C earns deployment iff (at 0.30) it lifts Sharpe/MAR and/or cuts cost")
    print("drag vs B, without gutting CAGR. If 0.20/0.40 also help, the edge is robust;")
    print("if only 0.30 helps, it's a fragile fit — do NOT deploy.")


if __name__ == "__main__":
    main()

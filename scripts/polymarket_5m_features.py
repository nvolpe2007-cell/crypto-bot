#!/usr/bin/env python3
"""
Do order flow + trend regime add information to Polymarket's own 5m BTC price?

For each 5-minute "Bitcoin Up or Down" window and each Polymarket print inside
it, features are computed from Kraken trades STRICTLY BEFORE the print instant
(no staleness edge — both sides see the same moment):

  z        ln(S/S0) / (sigma*sqrt(t_left))  — distance to the target in vol units
  ofi60    Kraken signed volume imbalance, last 60s   (buy - sell) / total
  ofi180   same, last 180s
  mom60    last-60s log return / sigma-per-minute
  trend60  60-minute log return / (sigma*sqrt(3600))   — trend regime
  sma_gap  (S - SMA60min) / (sigma*S*sqrt(3600))       — above/below the trend line

Order flow is Kraken-only on purpose (CLAUDE.md rule 5: never build a signal from
aggregated multi-venue volume — wash trading). Gamma levels are NOT here: there is
no free historical per-strike options OI, so they can only be logged forward.

Test (fit on the first half of days, judged on the second half):
  1. log-loss of the market price alone vs market + features (logistic regression
     on logit(p_mkt) + features). Features matter only if OOS log-loss improves.
  2. trading: buy the side where model prob - ask - fee >= MIN_EDGE, one bet per
     window, fill at print + half-spread, taker fee charged.

Usage:  python scripts/polymarket_5m_features.py --days 7
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
from pathlib import Path

import numpy as np
import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from polymarket_bot.fair_value import taker_fee_per_share  # noqa: E402

W = 300
HALF_SPREAD = 0.005
MIN_EDGE = 0.03
PM_CACHE = Path("data/polymarket_5m_cache.json")
TRADES_CACHE = Path("data/kraken_xbtusd_trades.npz")
FEATURES = ["z", "ofi60", "ofi180", "mom60", "trend60", "sma_gap"]


# ── Kraken trade history ─────────────────────────────────────────────────────

def fetch_trades(t0: int, t1: int) -> np.ndarray:
    """(time, price, signed_volume) rows from Kraken public Trades, cached."""
    if TRADES_CACHE.exists():
        arr = np.load(TRADES_CACHE)["trades"]
        if len(arr) and arr[0, 0] <= t0 + 60 and arr[-1, 0] >= t1 - 120:
            return arr
    rows, since, calls = [], str(t0 * 10**9), 0
    while True:
        try:
            r = requests.get("https://api.kraken.com/0/public/Trades",
                             params={"pair": "XBTUSD", "since": since, "count": 1000}, timeout=20).json()
        except Exception as exc:
            print(f"  retry ({exc})", flush=True)
            time.sleep(3)
            continue
        if r.get("error"):
            time.sleep(5)  # rate limited
            continue
        batch = next(v for k, v in r["result"].items() if k != "last")
        since = r["result"]["last"]
        for p, v, t, side, *_ in batch:
            rows.append((float(t), float(p), float(v) if side == "b" else -float(v)))
        calls += 1
        if calls % 50 == 0:
            print(f"  kraken trades: {len(rows):,} rows, at {time.strftime('%m-%d %H:%M', time.gmtime(rows[-1][0]))}",
                  flush=True)
        if not batch or rows[-1][0] >= t1:
            break
        time.sleep(1.1)
    arr = np.array(rows)
    TRADES_CACHE.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(TRADES_CACHE, trades=arr)
    return arr


class Tape:
    def __init__(self, trades: np.ndarray):
        self.t, self.p, self.sv = trades[:, 0], trades[:, 1], trades[:, 2]
        self.cum_sv = np.cumsum(self.sv)
        self.cum_av = np.cumsum(np.abs(self.sv))
        # 1-minute closes for vol / trend
        mins = (self.t // 60).astype(np.int64)
        last_idx = np.flatnonzero(np.diff(mins, append=mins[-1] + 1))
        self.min_ts, self.min_close = mins[last_idx] * 60, self.p[last_idx]

    def idx_before(self, ts: float) -> int:
        return int(np.searchsorted(self.t, ts, side="left")) - 1

    def price(self, ts: float):
        i = self.idx_before(ts)
        return self.p[i] if i >= 0 else None

    def ofi(self, ts: float, lookback: float) -> float:
        i, j = self.idx_before(ts - lookback), self.idx_before(ts)
        if j <= i:
            return 0.0
        tot = self.cum_av[j] - (self.cum_av[i] if i >= 0 else 0.0)
        return float((self.cum_sv[j] - (self.cum_sv[i] if i >= 0 else 0.0)) / tot) if tot > 0 else 0.0

    def closes(self, ts: float, n_min: int) -> np.ndarray:
        """Closes of the n complete minutes before ts."""
        k = int(np.searchsorted(self.min_ts, (ts // 60) * 60, side="left"))
        return self.min_close[max(0, k - n_min):k]


def features(tape: Tape, start: int, ts: float):
    s0, s = tape.price(start), tape.price(ts)
    closes = tape.closes(ts, 60)
    if s0 is None or s is None or len(closes) < 50:
        return None
    rets = np.diff(np.log(closes))
    sig_min = float(np.std(rets, ddof=1))
    if sig_min <= 0:
        return None
    sig_s = sig_min / math.sqrt(60)
    left = start + W - ts
    s60 = tape.price(ts - 60)
    return {
        "z": math.log(s / s0) / (sig_s * math.sqrt(max(left, 1.0))),
        "ofi60": tape.ofi(ts, 60),
        "ofi180": tape.ofi(ts, 180),
        "mom60": math.log(s / s60) / sig_min if s60 else 0.0,
        "trend60": math.log(s / closes[0]) / (sig_min * math.sqrt(60)),
        "sma_gap": (s - float(np.mean(closes))) / (sig_min * s * math.sqrt(60)),
    }


# ── model ────────────────────────────────────────────────────────────────────

def logit(p):
    p = np.clip(p, 1e-4, 1 - 1e-4)
    return np.log(p / (1 - p))


def fit_logistic(X, y, l2=1.0, iters=50):
    X = np.column_stack([np.ones(len(X)), X])
    w = np.zeros(X.shape[1])
    reg = np.full(X.shape[1], l2)
    reg[0] = 0.0
    for _ in range(iters):
        p = 1 / (1 + np.exp(-X @ w))
        g = X.T @ (p - y) + reg * w
        H = (X * (p * (1 - p))[:, None]).T @ X + np.diag(reg)
        w -= np.linalg.solve(H, g)
    return w


def predict(w, X):
    return 1 / (1 + np.exp(-(np.column_stack([np.ones(len(X)), X]) @ w)))


def logloss(p, y):
    p = np.clip(p, 1e-6, 1 - 1e-6)
    return float(-np.mean(y * np.log(p) + (1 - y) * np.log(1 - p)))


def trade_returns(rows, q):
    """One bet per window: first print where model edge clears MIN_EDGE."""
    seen, rets = set(), []
    for r, qi in zip(rows, q):
        if r["start"] in seen:
            continue
        for fair, px, won in ((qi, r["p"], r["y"] == 1), (1 - qi, 1 - r["p"], r["y"] == 0)):
            ask = px + HALF_SPREAD
            if not 0.10 <= ask <= 0.90:
                continue
            if fair - ask - taker_fee_per_share(ask) >= MIN_EDGE:
                rets.append(((1.0 if won else 0.0) - ask - taker_fee_per_share(ask)) / ask)
                seen.add(r["start"])
                break
    n = len(rets)
    if n < 2:
        return n, 0.0, 0.0
    m, sd = float(np.mean(rets)), float(np.std(rets, ddof=1))
    return n, m, m / (sd / math.sqrt(n))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=7)
    args = ap.parse_args()

    cache = json.loads(PM_CACHE.read_text())
    windows = sorted((w for w in cache.values() if w), key=lambda w: w["start"])
    cutoff = windows[-1]["start"] - args.days * 86400
    windows = [w for w in windows if w["start"] >= cutoff]
    print(f"5m windows: {len(windows)}", flush=True)

    tape = Tape(fetch_trades(windows[0]["start"] - 3700, windows[-1]["start"] + W))
    print(f"kraken trades: {len(tape.t):,}\n", flush=True)

    rows = []
    for w in windows:
        for ts, p in w["hist"]:
            if not (w["start"] + 30 <= ts <= w["start"] + W - 30) or not 0.02 < p < 0.98:
                continue
            f = features(tape, w["start"], ts)
            if f:
                rows.append({"start": w["start"], "p": p, "y": int(w["up_won"]), **f})
    split = windows[len(windows) // 2]["start"]
    tr = [r for r in rows if r["start"] < split]
    te = [r for r in rows if r["start"] >= split]
    y_tr, y_te = np.array([r["y"] for r in tr]), np.array([r["y"] for r in te])
    print(f"decision points: IS {len(tr)}  OOS {len(te)}\n")

    def X(rs, cols):
        return np.column_stack([logit(np.array([r["p"] for r in rs]))] + [np.array([r[c] for r in rs]) for c in cols])

    print("OOS log-loss (lower = better).  'market raw' = Polymarket's price taken at face value.")
    print(f"  market raw                         {logloss(np.array([r['p'] for r in te]), y_te):.4f}")
    base_w = fit_logistic(X(tr, []), y_tr)
    print(f"  market recalibrated                {logloss(predict(base_w, X(te, [])), y_te):.4f}")
    for cols in (["z"], ["ofi60"], ["ofi180"], ["mom60"], ["trend60"], ["sma_gap"], FEATURES):
        w = fit_logistic(X(tr, cols), y_tr)
        q = predict(w, X(te, cols))
        n, m, t = trade_returns(te, q)
        coefs = " ".join(f"{c}={v:+.3f}" for c, v in zip(["logit_p"] + cols, w[1:]))
        print(f"  market + {'+'.join(cols):26s} {logloss(q, y_te):.4f}   OOS trades n={n:4d} "
              f"mean={m:+.2%} t={t:+.2f}   [{coefs}]")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
Backtest the TWAP fair-value model on real, resolved Polymarket 15m BTC markets.

Question it answers: does the computed P(Up) price these markets better than the
market does, by enough to clear the taker fee + spread?

Data (all public, no keys):
  - Polymarket Gamma: each window's resolution (btc-updown-15m-<start_ts>)
  - Polymarket CLOB prices-history: the Up token's price at 1-minute fidelity
  - Coinbase BTC-USD 1m candles: the underlying (Chainlink's TWAP is not
    public history; the basis between the two is a real noise source here —
    it is why the rule-match rate below is < 100%)

No lookahead: at decision minute k the model sees only candles that CLOSED by
start+60k and the last Polymarket price printed at or before that instant.
Trades are hold-to-resolution, at most ONE per window (first qualifying minute),
filled at price + half-spread, taker fee charged.

Usage:  python scripts/polymarket_twap_backtest.py --days 7
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from polymarket_bot.fair_value import (  # noqa: E402
    WINDOW_S,
    edge_per_share,
    prob_up_endpoint,
    prob_up_window_twap,
    sigma_per_second,
    taker_fee_per_share,
)

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
COINBASE = "https://api.exchange.coinbase.com/products/BTC-USD/candles"
CACHE = Path("data/polymarket_backtest_cache.json")

# Pre-registered config (the ONE config judged; the sweep below is descriptive).
DECISION_MINUTES = (5, 7, 9, 11, 13)
MIN_EDGE = 0.03          # net expected profit per share, after fee
HALF_SPREAD = 0.005      # observed spread ~1c on live books
PRICE_BAND = (0.10, 0.90)
VOL_LOOKBACK_MIN = 60


def _get(url, params=None, tries=4):
    for i in range(tries):
        try:
            r = requests.get(url, params=params, timeout=20)
            if r.status_code == 429:
                time.sleep(2 + 2 * i)
                continue
            r.raise_for_status()
            return r.json()
        except Exception:
            if i == tries - 1:
                raise
            time.sleep(1 + i)


def fetch_window(start_ts: int):
    ev = _get(f"{GAMMA}/events", {"slug": f"btc-updown-15m-{start_ts}"})
    if not ev or not ev[0].get("markets"):
        return None
    m = ev[0]["markets"][0]
    if not m.get("closed"):
        return None
    prices = json.loads(m.get("outcomePrices") or "[]")
    outcomes = json.loads(m.get("outcomes") or "[]")
    tokens = json.loads(m.get("clobTokenIds") or "[]")
    if len(prices) != 2 or outcomes[:1] != ["Up"] or len(tokens) != 2:
        return None
    if prices[0] not in ("1", "0"):
        return None  # not cleanly resolved
    hist = _get(f"{CLOB}/prices-history",
                {"market": tokens[0], "startTs": start_ts - 60, "endTs": start_ts + WINDOW_S, "fidelity": 1})
    return {"start": start_ts, "up_won": prices[0] == "1",
            "hist": [(int(h["t"]), float(h["p"])) for h in hist.get("history", [])]}


def fetch_candles(t0: int, t1: int) -> dict:
    """{minute_start_ts: (open, close)} from Coinbase, 300 bars per request."""
    out = {}
    step = 300 * 60
    for s in range(t0, t1, step):
        e = min(s + step, t1)
        rows = _get(COINBASE, {"granularity": 60,
                               "start": datetime.fromtimestamp(s, timezone.utc).isoformat(),
                               "end": datetime.fromtimestamp(e, timezone.utc).isoformat()})
        for ts, _lo, _hi, op, cl, _v in rows:
            out[int(ts)] = (float(op), float(cl))
        time.sleep(0.15)
    return out


def load_data(days: int):
    now = int(time.time())
    end = now - (now % WINDOW_S) - WINDOW_S  # last fully resolved window
    starts = list(range(end - days * 86400, end, WINDOW_S))
    cache = json.loads(CACHE.read_text()) if CACHE.exists() else {"windows": {}}
    missing = [s for s in starts if str(s) not in cache["windows"]]
    print(f"windows: {len(starts)}  (fetching {len(missing)} not cached)", flush=True)
    with ThreadPoolExecutor(8) as pool:
        for s, w in zip(missing, pool.map(fetch_window_safe, missing)):
            cache["windows"][str(s)] = w
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    CACHE.write_text(json.dumps(cache))
    windows = [cache["windows"][str(s)] for s in starts if cache["windows"].get(str(s))]
    candles = fetch_candles(starts[0] - VOL_LOOKBACK_MIN * 60 - 60, end + WINDOW_S)
    return windows, candles


def fetch_window_safe(s):
    try:
        return fetch_window(s)
    except Exception as exc:  # one bad window must not kill the run
        print(f"  skip {s}: {exc}", flush=True)
        return None


def window_features(w, candles, k, btc_lag_bars=0, max_quote_age=None):
    """Everything knowable at start + 60k, or None if data is missing.

    btc_lag_bars: withhold the newest N BTC bars (staleness diagnostic).
    max_quote_age: drop decision points whose Polymarket print is older than
    this many seconds — a stale print against a fresh BTC price is a latency
    edge that exists only in the backtest.
    """
    s = w["start"]
    if k - btc_lag_bars < 1:
        return None
    bars = [candles.get(s + 60 * i) for i in range(k - btc_lag_bars)]
    if any(b is None for b in bars) or s not in candles:
        return None
    start_price = candles[s][0]
    closes = [b[1] for b in bars]
    pre = [candles.get(s - 60 * i) for i in range(VOL_LOOKBACK_MIN, 0, -1)]
    if any(b is None for b in pre):
        return None
    sig = sigma_per_second([b[1] for b in pre])
    t = s + 60 * k
    pm = [(ts, p) for ts, p in w["hist"] if ts <= t]
    if not pm or sig <= 0:
        return None
    if max_quote_age is not None and t - pm[-1][0] > max_quote_age:
        return None
    return start_price, sum(closes) / len(closes), closes[-1], sig, pm[-1][1]


def rule_match(windows, candles):
    """Which resolution rule does the real outcome follow?"""
    twap_hits = end_hits = n = 0
    for w in windows:
        s = w["start"]
        bars = [candles.get(s + 60 * i) for i in range(15)]
        if s not in candles or any(b is None for b in bars):
            continue
        start = candles[s][0]
        n += 1
        twap_hits += (sum(b[1] for b in bars) / 15 >= start) == w["up_won"]
        end_hits += (bars[-1][1] >= start) == w["up_won"]
    return n, twap_hits, end_hits


def simulate(windows, candles, model, min_edge, fee_rate, minutes=DECISION_MINUTES,
             btc_lag_bars=0, max_quote_age=None):
    trades, brier_m, brier_x = [], [], []
    for w in windows:
        taken = False
        for k in minutes:
            f = window_features(w, candles, k, btc_lag_bars, max_quote_age)
            if f is None:
                continue
            start, avg, spot, sig, mkt = f
            rem = WINDOW_S - 60 * k
            q = (prob_up_window_twap(start, avg, 60 * k, spot, sig) if model == "twap"
                 else prob_up_endpoint(start, spot, rem, sig))
            y = 1.0 if w["up_won"] else 0.0
            brier_m.append((q - y) ** 2)
            brier_x.append((mkt - y) ** 2)
            if taken:
                continue
            for side, fair, px in (("UP", q, mkt), ("DOWN", 1 - q, 1 - mkt)):
                ask = px + HALF_SPREAD
                if not (PRICE_BAND[0] <= ask <= PRICE_BAND[1]):
                    continue
                if edge_per_share(fair, ask, fee_rate) >= min_edge:
                    won = w["up_won"] == (side == "UP")
                    pnl = (1.0 if won else 0.0) - ask - taker_fee_per_share(ask, fee_rate)
                    trades.append({"start": w["start"], "k": k, "side": side, "ask": ask,
                                   "fair": fair, "won": won, "ret": pnl / ask})
                    taken = True
                    break
    return trades, brier_m, brier_x


def summarize(trades):
    n = len(trades)
    if n == 0:
        return "n=0"
    rets = [t["ret"] for t in trades]
    mean = sum(rets) / n
    sd = math.sqrt(sum((r - mean) ** 2 for r in rets) / (n - 1)) if n > 1 else 0.0
    t = mean / (sd / math.sqrt(n)) if sd > 0 else float("nan")
    wins = sum(t_["won"] for t_ in trades)
    fair = sum(t_["fair"] for t_ in trades) / n
    return (f"n={n:4d}  win={wins / n:5.1%}  model-said={fair:5.1%}  "
            f"mean ret/stake={mean:+.2%}  t={t:+.2f}  total on $10/trade=${10 * sum(rets):+.2f}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=7)
    args = ap.parse_args()
    windows, candles = load_data(args.days)
    print(f"resolved windows with data: {len(windows)}   candles: {len(candles)}\n")

    n, th, eh = rule_match(windows, candles)
    print("RESOLUTION RULE (Coinbase proxy vs real outcome)")
    print(f"  window-TWAP rule matches {th}/{n} = {th / max(n, 1):.1%}")
    print(f"  endpoint   rule matches {eh}/{n} = {eh / max(n, 1):.1%}\n")

    for model in ("twap", "endpoint"):
        trades, bm, bx = simulate(windows, candles, model, MIN_EDGE, 0.07)
        print(f"MODEL {model}")
        print(f"  Brier  model={sum(bm) / max(len(bm), 1):.4f}  market={sum(bx) / max(len(bx), 1):.4f}"
              f"  (lower is better; n={len(bm)} decision points)")
        print(f"  PRE-REGISTERED (edge>={MIN_EDGE}, fee 0.07): {summarize(trades)}")
        for fee in (0.07, 0.10):
            for me in (0.0, 0.02, 0.05, 0.10):
                tr, _, _ = simulate(windows, candles, model, me, fee)
                print(f"    sweep fee={fee:.2f} edge>={me:.2f}: {summarize(tr)}")
        print()

    # Staleness diagnostic: is the "edge" just a fresh BTC price vs a stale print?
    print("STALENESS DIAGNOSTIC (endpoint model, pre-registered edge/fee)")
    for label, lag, age in (("baseline", 0, None),
                            ("quote <=20s old", 0, 20),
                            ("BTC info 1 bar older than quote", 1, None),
                            ("BTC 1 bar older + quote <=20s", 1, 20)):
        tr, bm, bx = simulate(windows, candles, "endpoint", MIN_EDGE, 0.07,
                              btc_lag_bars=lag, max_quote_age=age)
        print(f"  {label:34s} Brier model={sum(bm) / max(len(bm), 1):.4f} "
              f"market={sum(bx) / max(len(bx), 1):.4f}  {summarize(tr)}")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
Backtest the fair-value model on real, settled Kalshi KXBTC15M markets.

Question: does the vol-scaled digital P(Up) price these markets better than the
market does, by enough to clear Kalshi's taker fee — WITHOUT a latency edge?

Data (all public, no keys):
  - Kalshi settled markets: floor_strike (exact BRTI target), expiration_value,
    result
  - Kalshi 1m candlesticks: yes_ask / yes_bid close = the executable quote at
    the end of each minute (real asks, not a mid or last trade)
  - Coinbase BTC-USD 1m candles as the BRTI proxy. The proxy/BRTI basis is
    measured at each window's open (floor_strike is known then) and removed.

No lookahead: at decision minute k (t = open + 60k) the model sees Coinbase bars
that CLOSED by t and the Kalshi quote of the minute ending at t. One trade per
window, first qualifying minute, bought at the ask, fee charged, held to
settlement.

The staleness test withholds the newest BTC bar (model one minute behind the
quote). An edge that vanishes there is a latency artifact, not an edge.

Usage:  python scripts/kalshi_backtest.py --days 30
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
from kalshi_bot.client import candlesticks, iso_ts, settled_markets  # noqa: E402
from kalshi_bot.fair_value import WINDOW_S, edge_per_contract, prob_up, sigma_per_second, taker_fee  # noqa: E402

COINBASE = "https://api.exchange.coinbase.com/products/BTC-USD/candles"
CACHE = Path("data/kalshi_backtest_cache.json")

# Pre-registered config (registry kalshi-btc-15m-updown). The ONE config judged;
# everything else printed is descriptive and counted as a variant.
DECISION_MINUTES = (5, 7, 9, 11, 13)
MIN_EDGE = 0.03          # net expected profit per contract, after fee
PRICE_BAND = (0.10, 0.90)
STAKE_USD = 10.0
VOL_LOOKBACK_MIN = 60


def fetch_window(m: dict):
    o, c = iso_ts(m["open_time"]), iso_ts(m["close_time"])
    if c - o != WINDOW_S or m.get("result") not in ("yes", "no") or not m.get("floor_strike"):
        return None
    quotes = {}
    for cd in candlesticks(m["ticker"], o, c):
        ya, yb = cd.get("yes_ask", {}).get("close_dollars"), cd.get("yes_bid", {}).get("close_dollars")
        if ya is not None and yb is not None:
            quotes[int(cd["end_period_ts"])] = (float(ya), float(yb))
    ev = m.get("expiration_value")
    return {"ticker": m["ticker"], "open": o, "strike": float(m["floor_strike"]),
            "exp": float(ev) if ev else None, "up_won": m["result"] == "yes",
            "quotes": {str(k): v for k, v in quotes.items()}}


_FAILED = object()


def fetch_window_safe(m):
    try:
        return fetch_window(m)
    except Exception as exc:
        print(f"  skip {m.get('ticker')}: {exc}", flush=True)
        return _FAILED


def fetch_candles(t0: int, t1: int) -> dict:
    """{minute_start_ts: (open, close)} from Coinbase, 300 bars per request."""
    out = {}
    for s in range(t0, t1, 300 * 60):
        e = min(s + 300 * 60, t1)
        for attempt in range(4):
            try:
                r = requests.get(COINBASE, params={
                    "granularity": 60,
                    "start": datetime.fromtimestamp(s, timezone.utc).isoformat(),
                    "end": datetime.fromtimestamp(e, timezone.utc).isoformat()}, timeout=20)
                r.raise_for_status()
                break
            except Exception:
                if attempt == 3:
                    raise
                time.sleep(2 + attempt)
        for ts, _lo, _hi, op, cl, _v in r.json():
            out[int(ts)] = (float(op), float(cl))
        time.sleep(0.12)
    return out


def load_data(days: int):
    now = int(time.time())
    end = now - now % WINDOW_S - WINDOW_S
    start = end - days * 86400
    cache = json.loads(CACHE.read_text()) if CACHE.exists() else {"windows": {}}
    markets = list(settled_markets(start + WINDOW_S, end))
    missing = [m for m in markets if m["ticker"] not in cache["windows"]]
    print(f"settled markets: {len(markets)}  (fetching {len(missing)} not cached)", flush=True)
    with ThreadPoolExecutor(4) as pool:
        for m, w in zip(missing, pool.map(fetch_window_safe, missing)):
            if w is not _FAILED:          # a transient error is retried next run, not cached
                cache["windows"][m["ticker"]] = w
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    CACHE.write_text(json.dumps(cache))
    windows = sorted((cache["windows"][m["ticker"]] for m in markets
                      if isinstance(cache["windows"].get(m["ticker"]), dict)),
                     key=lambda w: w["open"])
    candles = fetch_candles(start - (VOL_LOOKBACK_MIN + 2) * 60, end + WINDOW_S)
    return windows, candles


def proxy_minute(candles, ts):
    """Coinbase stand-in for a 60s BRTI average over the minute starting at ts."""
    b = candles.get(ts)
    return None if b is None else (b[0] + b[1]) / 2


def basis(w, candles):
    """Coinbase proxy minus BRTI, measured on the minute before the open."""
    p = proxy_minute(candles, w["open"] - 60)
    return None if p is None else p - w["strike"]


def rule_check(windows, candles):
    n = hits = 0
    errs = []
    for w in windows:
        b, p = basis(w, candles), proxy_minute(candles, w["open"] + WINDOW_S - 60)
        if b is None or p is None:
            continue
        n += 1
        hits += ((p - b) >= w["strike"]) == w["up_won"]
        if w["exp"]:
            errs.append(abs(p - b - w["exp"]))
    errs.sort()
    med = errs[len(errs) // 2] if errs else float("nan")
    return n, hits, med


def features(w, candles, k, btc_lag_bars=0):
    """Everything knowable at t = open + 60k, or None."""
    o, t = w["open"], w["open"] + 60 * k
    q = w["quotes"].get(str(t))
    b = basis(w, candles)
    spot_bar = candles.get(t - 60 * (1 + btc_lag_bars))
    pre = [candles.get(o - 60 * i) for i in range(VOL_LOOKBACK_MIN, 0, -1)]
    if q is None or b is None or spot_bar is None or any(x is None for x in pre):
        return None
    ya, yb = q
    if not (0 < yb < ya < 1):
        return None  # one-sided or empty book
    sig = sigma_per_second([x[1] for x in pre])
    if sig <= 0:
        return None
    return spot_bar[1] - b, sig, ya, yb


def simulate(windows, candles, min_edge=MIN_EDGE, minutes=DECISION_MINUTES, btc_lag_bars=0,
             fee_rate=0.07):
    trades, brier_m, brier_x = [], [], []
    for w in windows:
        taken = False
        y = 1.0 if w["up_won"] else 0.0
        for k in minutes:
            f = features(w, candles, k, btc_lag_bars)
            if f is None:
                continue
            spot, sig, ya, yb = f
            q = prob_up(w["strike"], spot, WINDOW_S - 60 * k, sig)
            brier_m.append((q - y) ** 2)
            brier_x.append(((ya + yb) / 2 - y) ** 2)
            if taken:
                continue
            for side, fair, ask in (("UP", q, ya), ("DOWN", 1 - q, 1 - yb)):
                if not (PRICE_BAND[0] <= ask <= PRICE_BAND[1]):
                    continue
                contracts = STAKE_USD / ask
                if edge_per_contract(fair, ask, contracts, fee_rate) >= min_edge:
                    won = w["up_won"] == (side == "UP")
                    fee_pc = taker_fee(contracts, ask, fee_rate) / contracts
                    trades.append({"open": w["open"], "k": k, "side": side, "ask": ask, "fee_pc": fee_pc,
                                   "fair": fair, "won": won, "pnl_pc": (1.0 if won else 0.0) - ask - fee_pc})
                    taken = True
                    break
    return trades, brier_m, brier_x


def stats(trades) -> dict:
    n = len(trades)
    if n < 2:
        return {"n": n}
    pnl = [t["pnl_pc"] for t in trades]
    mean = sum(pnl) / n
    sd = math.sqrt(sum((p - mean) ** 2 for p in pnl) / (n - 1))
    win = sum(t["won"] for t in trades) / n
    be = sum(t["ask"] + t["fee_pc"] for t in trades) / n
    ret = sum(t["pnl_pc"] / t["ask"] for t in trades) / n
    return {"n": n, "win": win, "breakeven": be, "win_minus_be": win - be,
            "model_said": sum(t["fair"] for t in trades) / n,
            "pnl_per_contract": mean, "t": mean / (sd / math.sqrt(n)) if sd > 0 else float("nan"),
            "ret_per_stake": ret, "total_usd": STAKE_USD * ret * n}


def fmt(s: dict) -> str:
    if s["n"] < 2:
        return f"n={s['n']}"
    return (f"n={s['n']:4d} win={s['win']:5.1%} breakeven={s['breakeven']:5.1%} "
            f"(model-said {s['model_said']:5.1%})  pnl/contract={s['pnl_per_contract']:+.4f} "
            f"t={s['t']:+.2f}  ret/stake={s['ret_per_stake']:+.2%}  total=${s['total_usd']:+.2f}")


def brier(bm, bx):
    return f"Brier model={sum(bm) / max(len(bm), 1):.4f} market-mid={sum(bx) / max(len(bx), 1):.4f} (n={len(bm)})"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--json", type=str, default="", help="write the headline numbers here")
    args = ap.parse_args()
    windows, candles = load_data(args.days)
    print(f"windows with data: {len(windows)}   coinbase bars: {len(candles)}\n")
    out = {"windows": len(windows)}

    n, hits, med = rule_check(windows, candles)
    print(f"SETTLEMENT PROXY: basis-corrected Coinbase matches the real result {hits}/{n} = "
          f"{hits / max(n, 1):.1%}; median |proxy - expiration_value| = ${med:.2f}\n")
    out["proxy_match"] = hits / max(n, 1)

    variants = 0
    tr, bm, bx = simulate(windows, candles)
    variants += 1
    out["preregistered"] = stats(tr)
    print("PRE-REGISTERED (edge>=0.03, minutes 5..13, price band 0.10-0.90, fee 0.07, buy at ask)")
    print(f"  {brier(bm, bx)}")
    print(f"  all     {fmt(stats(tr))}")
    half = windows[len(windows) // 2]["open"]
    out["is"] = stats([t for t in tr if t["open"] < half])
    out["oos"] = stats([t for t in tr if t["open"] >= half])
    print(f"  IS  1st {fmt(out['is'])}")
    print(f"  OOS 2nd {fmt(out['oos'])}\n")

    print("STALENESS TEST (model's BTC price one minute older than the quote)")
    tr_l, bm_l, bx_l = simulate(windows, candles, btc_lag_bars=1)
    variants += 1
    out["lag1"] = stats(tr_l)
    print(f"  {brier(bm_l, bx_l)}")
    print(f"  lag1    {fmt(out['lag1'])}\n")

    print("DESCRIPTIVE SWEEP (each counts as a variant; none is judged)")
    for me in (0.0, 0.05, 0.10):
        for lag in (0, 1):
            s = stats(simulate(windows, candles, min_edge=me, btc_lag_bars=lag)[0])
            variants += 1
            print(f"  edge>={me:.2f} lag={lag}: {fmt(s)}")
    out["variants"] = variants
    # Sidak bar for `variants` tries at family alpha 0.05 (two-sided normal approx).
    alpha = 1 - (1 - 0.05) ** (1 / variants)
    lo, hi = 0.0, 10.0
    for _ in range(60):
        mid = (lo + hi) / 2
        lo, hi = (mid, hi) if math.erfc(mid / math.sqrt(2)) > alpha else (lo, mid)
    out["sidak_t"] = max(3.0, hi)
    print(f"\nvariants tried: {variants}  ->  pass bar t >= {out['sidak_t']:.2f} (floor 3.0)")
    if args.json:
        Path(args.json).write_text(json.dumps(out, indent=2))


if __name__ == "__main__":
    main()

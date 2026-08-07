#!/usr/bin/env python3
"""
Liquidation-cascade backtest (research, 2026-07-17).

Tests the two "Moon Dev stream" strategy shapes against our data and our
production cost model (lev_perp arm family: 0.15% notional round-trip,
10% APY funding drag on notional, fixed $333 margin @ 3x = ~$1k notional).

We have no historical liquidation feed, so cascades are PROXIED on 1h bars:
a bar whose return z-score (vs rolling 200-bar sigma) exceeds RET_Z and whose
volume z-score exceeds VOL_Z. That is the on-chart signature of a cascade.

Strategies (entries at next bar open, taker):
  A. regime_switch  — trade WITH the cascade if price is not yet stretched
                      (|close-SMA50| < STRETCH_ATR * ATR14), FADE it if it is.
  B. swing_prox     — trade WITH the cascade only if the cascade bar swept
                      within SWEEP_ATR * ATR14 of the prior 48-bar swing
                      high/low; skip cascades in no-man's land.
  C. with_cascade   — baseline: always trade with the cascade (no filter).
  D. fade_cascade   — baseline: always fade.

Exit for all: chandelier ATR(14) x 2 trail (the v2 finding), 48h time stop.

Usage: .venv-test/bin/python3 backtest_liq_cascade.py [--days 1825]
"""
import argparse, sys, time
from pathlib import Path

import ccxt
import numpy as np
import pandas as pd

SYMBOLS      = ["BTC/USDT", "ETH/USDT", "SOL/USDT"]
CACHE_DIR    = Path(__file__).parent / "data" / "cache_1h"
RET_Z        = 2.5
VOL_Z        = 2.0
STRETCH_ATR  = 3.0
SWEEP_ATR    = 0.5
SWING_N      = 48
TRAIL_MULT   = 2.0
MAX_HOLD_H   = 48
COST_FRAC    = 0.0015          # round-trip, on notional (production TRADE_COST_FRAC)
FUNDING_APY  = 0.10            # on notional, always a drag (production)
MARGIN       = 333.0
LEVERAGE     = 3.0
NOTIONAL     = MARGIN * LEVERAGE


def fetch_1h(symbol: str, days: int) -> pd.DataFrame:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache = CACHE_DIR / f"{symbol.replace('/', '_')}_{days}d.parquet"
    if cache.exists():
        return pd.read_pickle(cache)
    last_err = None
    for exname in ["okx", "bybit", "kucoin", "binance"]:
        try:
            ex = getattr(ccxt, exname)({"enableRateLimit": True})
            since = ex.milliseconds() - days * 86400_000
            rows = []
            while True:
                batch = ex.fetch_ohlcv(symbol, "1h", since=since, limit=300)
                if not batch:
                    break
                rows += batch
                nxt = batch[-1][0] + 3600_000
                if nxt <= since or len(batch) < 2:
                    break
                since = nxt
                if since > ex.milliseconds() - 3600_000:
                    break
            if len(rows) < days * 20:   # demand ~85% coverage
                raise RuntimeError(f"{exname}: only {len(rows)} bars")
            df = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close", "volume"])
            df = df.drop_duplicates("ts").sort_values("ts").reset_index(drop=True)
            df["dt"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
            df.to_pickle(cache)
            print(f"  {symbol}: {len(df)} bars from {exname} "
                  f"({df.dt.iloc[0].date()} -> {df.dt.iloc[-1].date()})")
            return df
        except Exception as e:                       # noqa: BLE001
            last_err = e
            print(f"  {symbol}: {exname} failed ({e}); trying next", file=sys.stderr)
    raise RuntimeError(f"all exchanges failed for {symbol}: {last_err}")


def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["ret"] = df.close.pct_change()
    df["ret_sig"] = df.ret.rolling(200).std()
    df["ret_z"] = df.ret / df.ret_sig
    lv = np.log1p(df.volume)
    df["vol_z"] = (lv - lv.rolling(200).mean()) / lv.rolling(200).std()
    tr = np.maximum(df.high - df.low,
                    np.maximum((df.high - df.close.shift()).abs(),
                               (df.low - df.close.shift()).abs()))
    df["atr"] = tr.rolling(14).mean()
    df["sma50"] = df.close.rolling(50).mean()
    df["stretch"] = (df.close - df.sma50) / df.atr
    df["swing_hi"] = df.high.shift(1).rolling(SWING_N).max()
    df["swing_lo"] = df.low.shift(1).rolling(SWING_N).min()
    df["cascade"] = (df.ret_z.abs() > RET_Z) & (df.vol_z > VOL_Z)
    return df


def run_strategy(df: pd.DataFrame, mode: str) -> list:
    trades, i, n = [], 200, len(df)
    while i < n - 2:
        row = df.iloc[i]
        if not row.cascade or np.isnan(row.atr) or np.isnan(row.stretch):
            i += 1
            continue
        casc_dir = 1 if row.ret > 0 else -1
        side = None
        if mode == "with_cascade":
            side = casc_dir
        elif mode == "fade_cascade":
            side = -casc_dir
        elif mode == "regime_switch":
            side = casc_dir if abs(row.stretch) < STRETCH_ATR else -casc_dir
        elif mode == "swing_prox":
            near_hi = row.high >= row.swing_hi - SWEEP_ATR * row.atr
            near_lo = row.low <= row.swing_lo + SWEEP_ATR * row.atr
            if (casc_dir > 0 and near_hi) or (casc_dir < 0 and near_lo):
                side = casc_dir
        if side is None:
            i += 1
            continue
        entry = df.iloc[i + 1].open
        qty = NOTIONAL / entry
        peak = trough = entry
        exit_px, exit_i = None, None
        for j in range(i + 1, min(i + 1 + MAX_HOLD_H, n)):
            b = df.iloc[j]
            if side > 0:
                peak = max(peak, b.high)
                stop = peak - TRAIL_MULT * b.atr
                if b.low <= stop:
                    exit_px, exit_i = min(stop, b.open), j
                    break
            else:
                trough = min(trough, b.low)
                stop = trough + TRAIL_MULT * b.atr
                if b.high >= stop:
                    exit_px, exit_i = max(stop, b.open), j
                    break
        if exit_px is None:
            exit_i = min(i + MAX_HOLD_H, n - 1)
            exit_px = df.iloc[exit_i].close
        gross = side * qty * (exit_px - entry)
        hours = exit_i - i
        cost = NOTIONAL * COST_FRAC + NOTIONAL * FUNDING_APY * hours / (24 * 365)
        trades.append({"dt": df.iloc[i]["dt"], "side": side, "pnl": gross - cost,
                       "hours": hours})
        i = exit_i + 1          # no overlapping positions per symbol
    return trades


def report(name: str, trades: list):
    if not trades:
        print(f"{name:14s}  no trades")
        return
    t = pd.DataFrame(trades)
    eq = t.pnl.cumsum()
    dd = (eq - eq.cummax()).min()
    yearly = t.groupby(t.dt.dt.year).pnl.sum().round(0).to_dict()
    print(f"{name:14s}  trades {len(t):4d}  WR {100*(t.pnl>0).mean():4.1f}%  "
          f"PnL ${t.pnl.sum():+8.0f}  ret {100*t.pnl.sum()/1000:+6.1f}% of $1k book  "
          f"maxDD ${dd:+.0f}  avg hold {t.hours.mean():.0f}h")
    print(f"{'':14s}  by year: {yearly}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=1825)
    args = ap.parse_args()
    print(f"Fetching 1h bars, {args.days}d ...")
    data = {s: add_indicators(fetch_1h(s, args.days)) for s in SYMBOLS}
    for s, df in data.items():
        print(f"{s}: {int(df.cascade.sum())} cascade bars "
              f"({100*df.cascade.mean():.2f}% of {len(df)})")
    for mode in ["with_cascade", "fade_cascade", "regime_switch", "swing_prox"]:
        allt = []
        for s in SYMBOLS:
            allt += run_strategy(data[s], mode)
        allt.sort(key=lambda x: x["dt"])
        report(mode, allt)


if __name__ == "__main__":
    main()

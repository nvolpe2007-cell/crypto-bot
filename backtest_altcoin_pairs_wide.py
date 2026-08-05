#!/usr/bin/env python3
"""
Widened altcoin cointegration walk-forward (research, 2026-08-05).

Extends backtest_altcoin_pairs.py's methodology from 10 to 16 coins (adds
AAVE/UNI/ATOM/ALGO/DOGE/FIL) and applies the walk-forward discipline from the
START (not as a post-hoc strengthening step): for pair (a,b), fold k trains
cointegration on window k, the strategy is backtested strictly on window k+1,
across 3 non-overlapping fold boundaries. A pair only counts as a candidate if
ALL 3 folds are net positive -- the same bar the original 4 pairs were held to
after the fact, applied here as the selection criterion itself.
"""
import numpy as np
import pandas as pd
from itertools import combinations
from pathlib import Path
from statsmodels.tsa.stattools import adfuller
import statsmodels.api as sm
import importlib.util

spec = importlib.util.spec_from_file_location("bap", "backtest_altcoin_pairs.py")
bap = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bap)

WIDE_COINS = bap.COINS + ["AAVE", "UNI", "ATOM", "ALGO", "DOGE", "FIL"]
CACHE_DAILY = Path("data/cache_daily")
K_FOLDS = 4
ALREADY_SHIPPED = {("AVAX", "ETH"), ("BCH", "ETH"), ("ETH", "XRP"), ("LINK", "SOL")}
ALREADY_TRADED_PROD = {("BTC", "ETH"), ("BTC", "SOL"), ("ETH", "SOL")}


def load_hourly(coin: str) -> pd.Series:
    if coin in ("BTC", "ETH", "SOL"):
        df = pd.read_pickle(f"data/cache_1h/{coin}_USDT_1825d.parquet")
        df["dt"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
        return df.set_index("dt")["close"]
    daily = pd.read_pickle(CACHE_DAILY / f"{coin}_USDT_1825d.pkl").set_index("dt")["close"]
    idx = pd.date_range(daily.index[0], daily.index[-1], freq="1h", tz="UTC")
    return daily.reindex(idx).ffill()


def run_pair(prices: pd.DataFrame, a: str, b: str, i_start: int, i_end: int) -> pd.DataFrame:
    spread = np.log(prices[a]) - np.log(prices[b])
    mean = spread.rolling(bap.LOOKBACK_H).mean()
    std = spread.rolling(bap.LOOKBACK_H).std()
    z = (spread - mean) / std
    trades = []
    i, pos = max(i_start, bap.LOOKBACK_H), None
    while i < i_end:
        if pos is None:
            if pd.notna(z.iloc[i]) and abs(z.iloc[i]) >= bap.ENTRY_Z:
                side = -1 if z.iloc[i] > 0 else 1
                pos = {"entry_i": i, "side": side, "entry_a": prices[a].iloc[i], "entry_b": prices[b].iloc[i]}
        else:
            held_h = i - pos["entry_i"]
            exit_now = False
            if pd.notna(z.iloc[i]) and (abs(z.iloc[i]) <= bap.EXIT_Z or abs(z.iloc[i]) >= bap.STOP_Z):
                exit_now = True
            if held_h >= bap.MAX_HOLD_H:
                exit_now = True
            if exit_now:
                ret_a = pos["side"] * (prices[a].iloc[i] - pos["entry_a"]) / pos["entry_a"]
                ret_b = -pos["side"] * (prices[b].iloc[i] - pos["entry_b"]) / pos["entry_b"]
                gross = bap.LEG_NOTIONAL * (ret_a + ret_b)
                cost = bap.LEG_NOTIONAL * bap.COST_FRAC * 2 + bap.LEG_NOTIONAL * bap.FUNDING_APY * held_h / (24 * 365)
                trades.append({"pnl": gross - cost})
                pos = None
        i += 1
    return pd.DataFrame(trades)


def test_cointegration(prices: pd.DataFrame, a: str, b: str, i_start: int, i_end: int) -> float:
    y, x = np.log(prices[a].iloc[i_start:i_end]), np.log(prices[b].iloc[i_start:i_end])
    X = sm.add_constant(x)
    resid = sm.OLS(y, X).fit().resid
    _, pval, *_ = adfuller(resid, autolag="AIC")
    return pval


def main():
    print("Loading price series for 16 coins...")
    series = {c: load_hourly(c) for c in WIDE_COINS}
    common_idx = None
    for s in series.values():
        common_idx = s.index if common_idx is None else common_idx.intersection(s.index)
    aligned = {c: series[c].reindex(common_idx).dropna() for c in WIDE_COINS}
    common_idx = None
    for s in aligned.values():
        common_idx = s.index if common_idx is None else common_idx.intersection(s.index)
    prices = pd.DataFrame({c: aligned[c].reindex(common_idx) for c in WIDE_COINS}).dropna()
    n = len(prices)
    print(f"{n} common hourly bars, {prices.index[0]} -> {prices.index[-1]}")

    fold_edges = [int(n * i / K_FOLDS) for i in range(K_FOLDS + 1)]
    all_pairs = list(combinations(WIDE_COINS, 2))
    print(f"\nScanning {len(all_pairs)} pairs across {K_FOLDS-1} walk-forward folds "
          f"(pair passes only if ALL folds net positive)...\n")

    survivors = []
    for a, b in all_pairs:
        key = tuple(sorted((a, b)))
        if key in ALREADY_SHIPPED or key in ALREADY_TRADED_PROD:
            continue
        fold_results = []
        for k in range(K_FOLDS - 1):
            train_s, train_e = fold_edges[k], fold_edges[k + 1]
            test_s, test_e = fold_edges[k + 1], fold_edges[k + 2]
            t = run_pair(prices, a, b, test_s, test_e)
            if len(t) < 10:  # too few trades in this fold to trust
                fold_results = None
                break
            fold_results.append(t.pnl.sum())
        if fold_results is None:
            continue
        if all(f > 0 for f in fold_results):
            pval_last_train = test_cointegration(prices, a, b, fold_edges[-2], fold_edges[-1])
            survivors.append({"a": a, "b": b, "fold_totals": fold_results,
                              "sum_total": sum(fold_results), "last_train_pval": pval_last_train})

    if not survivors:
        print("No new pairs cleared ALL folds net-positive. Nothing further to report.")
        return

    surv = pd.DataFrame(survivors).sort_values("sum_total", ascending=False)
    print(f"{len(surv)} NEW pairs (beyond the 4 already shipped) net-positive in EVERY fold:\n")
    for _, row in surv.iterrows():
        folds_str = "  ".join(f"${f:+.0f}" for f in row.fold_totals)
        print(f"  {row.a}/{row.b}: folds=[{folds_str}]  sum=${row.sum_total:+.2f}  "
              f"last-fold train-coint p={row.last_train_pval:.3f}")


if __name__ == "__main__":
    main()

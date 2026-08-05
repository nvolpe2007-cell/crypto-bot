#!/usr/bin/env python3
"""
4-fold walk-forward validation for the altcoin pairs found in
backtest_altcoin_pairs.py (research, 2026-08-05).

For each of the 4 candidate pairs (ETH/AVAX, ETH/BCH, ETH/XRP, LINK/SOL): split
the 5-year hourly series into 4 non-overlapping windows. For each consecutive
pair of windows (3 folds), test cointegration on the EARLIER window only, then
backtest the z-score mean-reversion strategy strictly on the LATER window (never
seen by the cointegration test). This is the honest robustness check the earlier
single-split test (backtest_altcoin_pairs.py) didn't have: does the edge hold up
across multiple independent train/test boundaries, not just one.

Result (2026-08-05): 12/12 fold-tests net positive across all 4 pairs -- see
RESEARCH_2026-08-04_altcoin_pairs_and_arb.md section 3b for the full table and
discussion.
"""
import numpy as np
import pandas as pd
from statsmodels.tsa.stattools import adfuller
import statsmodels.api as sm
import importlib.util

spec = importlib.util.spec_from_file_location("bap", "backtest_altcoin_pairs.py")
bap = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bap)

CANDIDATES = [("ETH", "AVAX"), ("ETH", "BCH"), ("ETH", "XRP"), ("LINK", "SOL")]
K_FOLDS = 4


def load_prices() -> pd.DataFrame:
    series = {c: bap.load_hourly(c) for c in bap.COINS}
    common_idx = None
    for s in series.values():
        common_idx = s.index if common_idx is None else common_idx.intersection(s.index)
    aligned = {c: series[c].reindex(common_idx).dropna() for c in bap.COINS}
    common_idx = None
    for s in aligned.values():
        common_idx = s.index if common_idx is None else common_idx.intersection(s.index)
    return pd.DataFrame({c: aligned[c].reindex(common_idx) for c in bap.COINS}).dropna()


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
            if pd.notna(z.iloc[i]):
                if abs(z.iloc[i]) <= bap.EXIT_Z or abs(z.iloc[i]) >= bap.STOP_Z:
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
    prices = load_prices()
    n = len(prices)
    fold_edges = [int(n * i / K_FOLDS) for i in range(K_FOLDS + 1)]
    print(f"Walk-forward, {K_FOLDS} folds of ~{fold_edges[1]-fold_edges[0]} bars each\n")

    all_positive = True
    for a, b in CANDIDATES:
        print(f"=== {a}/{b} ===")
        for k in range(K_FOLDS - 1):
            train_s, train_e = fold_edges[k], fold_edges[k + 1]
            test_s, test_e = fold_edges[k + 1], fold_edges[k + 2]
            pval = test_cointegration(prices, a, b, train_s, train_e)
            t = run_pair(prices, a, b, test_s, test_e)
            if len(t) == 0:
                print(f"  fold {k}: train-coint p={pval:.3f}  test: no trades")
                continue
            ts = t.pnl.mean() / t.pnl.std() * len(t) ** 0.5 if t.pnl.std() > 0 else 0
            all_positive &= t.pnl.sum() > 0
            date_s, date_e = prices.index[test_s].date(), prices.index[test_e - 1].date()
            print(f"  fold {k}: train-coint p={pval:.3f}  test[{date_s}->{date_e}]: "
                  f"n={len(t):3d} WR={100*(t.pnl>0).mean():5.1f}% total=${t.pnl.sum():+7.2f} t={ts:+.2f}")
        print()
    print(f"All {(K_FOLDS-1)*len(CANDIDATES)} fold-tests net positive: {all_positive}")


if __name__ == "__main__":
    main()

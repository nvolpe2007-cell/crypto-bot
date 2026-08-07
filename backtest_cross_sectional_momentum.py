#!/usr/bin/env python3
"""
Cross-sectional relative-strength momentum (research, 2026-08-05).

A genuinely different strategy SHAPE from everything else tested this session:
not absolute trend-following (lev_perp/trend_ensemble/tsmom -- "is this coin
going up"), not relative-value mean-reversion (pairs_altcoin -- "will this
spread revert"), but relative-strength momentum -- "which coins are winning
right now, ride those, avoid the losers" (continuation, not reversion).

Pre-specified, NOT swept (avoids the multiple-testing trap that killed the
directional-bias indicator search): 30-day lookback for ranking, weekly
rebalance, long the top-3 of 16 coins by trailing return, equal-weighted,
funded from cash (no shorting the bottom -- keeps it spot-executable). These
are standard textbook parameters (Jegadeesh-Titman-style), not fitted to this
data.

Walk-forward validated: split into 4 non-overlapping windows, no parameter
ever chosen using data the strategy is then tested on (there's nothing to fit
here since parameters are fixed, but the METRIC is still computed per-window
to check consistency, same discipline as the pairs research).
"""
import numpy as np
import pandas as pd
from pathlib import Path
import importlib.util

spec = importlib.util.spec_from_file_location("bapw", "backtest_altcoin_pairs_wide.py")
bapw = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bapw)

COINS = bapw.WIDE_COINS  # 16 coins
LOOKBACK_DAYS = 30
REBAL_DAYS = 7
TOP_N = 3
TAKER_COST = 0.0026
START_EQUITY = 1000.0
K_FOLDS = 4


def load_daily_prices() -> pd.DataFrame:
    series = {}
    for c in COINS:
        if c in ("BTC", "ETH", "SOL"):
            df = pd.read_pickle(f"data/cache_1h/{c}_USDT_1825d.parquet")
            df["dt"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
            s = df.set_index("dt")["close"].resample("1D").last()
        else:
            s = pd.read_pickle(f"data/cache_daily/{c}_USDT_1825d.pkl").set_index("dt")["close"]
        series[c] = s
    common = None
    for s in series.values():
        common = s.index if common is None else common.intersection(s.index)
    return pd.DataFrame({c: series[c].reindex(common) for c in COINS}).dropna()


def backtest(prices: pd.DataFrame, i_start: int, i_end: int) -> pd.DataFrame:
    equity = START_EQUITY
    holdings = {}  # coin -> units
    trades = []
    i = max(i_start, LOOKBACK_DAYS)
    last_rebal = i
    while i < i_end:
        if i == max(i_start, LOOKBACK_DAYS) or (i - last_rebal) >= REBAL_DAYS:
            cur_val = sum(holdings.get(c, 0.0) * prices[c].iloc[i] for c in COINS)
            equity_now = cur_val if holdings else equity
            trailing_ret = (prices.iloc[i] / prices.iloc[i - LOOKBACK_DAYS] - 1)
            winners = trailing_ret.nlargest(TOP_N).index.tolist()
            target_val = {c: (equity_now / TOP_N if c in winners else 0.0) for c in COINS}
            turnover = sum(abs(target_val[c] - holdings.get(c, 0.0) * prices[c].iloc[i]) for c in COINS)
            cost = turnover * TAKER_COST
            equity_after = equity_now - cost
            new_holdings = {c: (equity_after / TOP_N) / prices[c].iloc[i] for c in winners}
            trades.append({"dt": prices.index[i], "winners": winners, "equity": equity_after, "cost": cost})
            holdings = new_holdings
            equity = equity_after
            last_rebal = i
        i += 1
    return pd.DataFrame(trades)


def main():
    prices = load_daily_prices()
    n = len(prices)
    print(f"{n} common daily bars, {prices.index[0].date()} -> {prices.index[-1].date()}\n")

    fold_edges = [int(n * i / K_FOLDS) for i in range(K_FOLDS + 1)]
    print(f"Walk-forward, {K_FOLDS} folds:\n")
    all_returns = []
    for k in range(K_FOLDS):
        s, e = fold_edges[k], fold_edges[k + 1]
        t = backtest(prices, s, e)
        if len(t) < 2:
            print(f"  fold {k}: too few rebalances")
            continue
        ret_pct = 100 * (t.equity.iloc[-1] / START_EQUITY - 1)
        n_weeks = len(t)
        ann_ret = ((t.equity.iloc[-1] / START_EQUITY) ** (365 / (n_weeks * REBAL_DAYS)) - 1) * 100
        eq = t.equity
        dd = (eq / eq.cummax() - 1).min() * 100
        date_s, date_e = prices.index[s].date(), prices.index[e - 1].date()
        print(f"  fold {k} [{date_s}->{date_e}]: {n_weeks} rebalances, "
              f"total={ret_pct:+.1f}%  ann={ann_ret:+.1f}%  maxDD={dd:.1f}%")
        all_returns.append(ret_pct)

    print(f"\nAll {K_FOLDS} folds positive: {all(r > 0 for r in all_returns)}")

    # Buy-and-hold comparison: equal-weight the SAME 16 coins, no rebalancing/momentum
    bh_ret = 100 * ((prices.iloc[-1] / prices.iloc[0]).mean() - 1)
    print(f"Reference: equal-weight buy&hold of all 16 coins, full period: {bh_ret:+.1f}%")


if __name__ == "__main__":
    main()

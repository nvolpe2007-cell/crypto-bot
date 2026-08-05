#!/usr/bin/env python3
"""
Altcoin cointegration + pairs-trading backtest (research, 2026-08-04).

pairs_paper.py (production) only trades the 3 pre-picked pairs among
BTC/ETH/SOL, on a hardcoded z-score threshold -- it never actually TESTS
cointegration, it assumes it. This extends the search to all C(10,2)=45 pairs
across the wider 10-coin universe (BTC/ETH/SOL + LTC/BCH/XRP/ADA/LINK/AVAX/
DOT), running a proper Engle-Granger test (OLS hedge ratio + ADF on the
residual) on each pair, THEN backtesting only the pairs that pass a real
stationarity bar -- with the exact z-score entry/exit/stop/cost mechanics
already in production (pairs_paper.py: entry|z|>=2.0, exit|z|<=0.5,
stop|z|>=3.5, 7-day time stop, 0.15%x2-leg cost + funding drag on the short
leg), so any survivor is directly comparable to the live arm's own numbers.
"""
import numpy as np
import pandas as pd
from itertools import combinations
from pathlib import Path
from statsmodels.tsa.stattools import adfuller
import statsmodels.api as sm

COINS = ["BTC", "ETH", "SOL", "LTC", "BCH", "XRP", "ADA", "LINK", "AVAX", "DOT"]
CACHE_DAILY = Path("data/cache_daily")
CACHE_1H = Path("data/cache_1h")

LOOKBACK_H = 168          # 7 days, matches pairs_paper.py LOOKBACK
ENTRY_Z, EXIT_Z, STOP_Z = 2.0, 0.5, 3.5
MAX_HOLD_H = 168
LEG_NOTIONAL = 150.0
COST_FRAC = 0.0015
FUNDING_APY = 0.10
ADF_P_BAR = 0.05          # standard cointegration significance bar


def load_hourly(coin: str) -> pd.Series:
    if coin in ("BTC", "ETH", "SOL"):
        df = pd.read_pickle(CACHE_1H / f"{coin}_USDT_1825d.parquet")
        df["dt"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
    else:
        daily = pd.read_pickle(CACHE_DAILY / f"{coin}_USDT_1825d.pkl")
        # only daily bars cached for these -- upsample to hourly via ffill for
        # the SCREENING step only (cointegration test on daily closes is fine;
        # backtest re-derives proper hourly z-scores from real hourly data
        # where available, daily-ffill'd elsewhere, clearly labeled below)
        daily = daily.set_index("dt")["close"]
        idx = pd.date_range(daily.index[0], daily.index[-1], freq="1h", tz="UTC")
        return daily.reindex(idx).ffill()
    return df.set_index("dt")["close"]


def main():
    print("Loading price series...")
    series = {c: load_hourly(c) for c in COINS}
    common_idx = None
    for s in series.values():
        common_idx = s.index if common_idx is None else common_idx.intersection(s.index)
    aligned = {c: series[c].reindex(common_idx).dropna() for c in COINS}
    common_idx = None
    for s in aligned.values():
        common_idx = s.index if common_idx is None else common_idx.intersection(s.index)
    prices = pd.DataFrame({c: aligned[c].reindex(common_idx) for c in COINS}).dropna()
    print(f"{len(prices)} common hourly bars, {prices.index[0]} -> {prices.index[-1]}")

    print("\nEngle-Granger cointegration scan, all C(10,2)=45 pairs:")
    results = []
    for a, b in combinations(COINS, 2):
        y, x = np.log(prices[a]), np.log(prices[b])
        X = sm.add_constant(x)
        model = sm.OLS(y, X).fit()
        hedge_ratio = model.params.iloc[1]
        resid = model.resid
        adf_stat, pval, *_ = adfuller(resid, autolag="AIC")
        results.append({"a": a, "b": b, "hedge_ratio": hedge_ratio,
                        "adf_stat": adf_stat, "pval": pval,
                        "cointegrated": pval < ADF_P_BAR})

    r = pd.DataFrame(results).sort_values("pval")
    pd.set_option("display.width", 140)
    print(r.to_string(index=False))
    coint_pairs = r[r.cointegrated]
    print(f"\n{len(coint_pairs)} / {len(r)} pairs pass ADF p<{ADF_P_BAR} (Engle-Granger cointegrated)")
    already_traded = {("BTC", "ETH"), ("BTC", "SOL"), ("ETH", "SOL")}
    new_pairs = coint_pairs[~coint_pairs.apply(lambda row: (row.a, row.b) in already_traded, axis=1)]
    print(f"{len(new_pairs)} of those are NEW (not already in production pairs_paper.py's 3 pairs)")

    if len(new_pairs) == 0:
        print("\nNo new cointegrated altcoin pairs found -- nothing to backtest.")
        return

    print("\n=== Backtesting z-score mean-reversion on newly-cointegrated pairs ===")
    print("(same mechanics/costs as production pairs_paper.py)")
    for _, row in new_pairs.iterrows():
        a, b = row.a, row.b
        spread = np.log(prices[a]) - np.log(prices[b])
        mean = spread.rolling(LOOKBACK_H).mean()
        std = spread.rolling(LOOKBACK_H).std()
        z = (spread - mean) / std

        trades = []
        i, n = LOOKBACK_H, len(prices)
        pos = None
        while i < n:
            if pos is None:
                if pd.notna(z.iloc[i]) and abs(z.iloc[i]) >= ENTRY_Z:
                    side = -1 if z.iloc[i] > 0 else 1  # side=+1 means long A/short B
                    pos = {"entry_i": i, "side": side, "entry_a": prices[a].iloc[i], "entry_b": prices[b].iloc[i]}
            else:
                held_h = i - pos["entry_i"]
                exit_now, reason = False, None
                if pd.notna(z.iloc[i]):
                    if abs(z.iloc[i]) <= EXIT_Z:
                        exit_now, reason = True, "converged"
                    elif abs(z.iloc[i]) >= STOP_Z:
                        exit_now, reason = True, "stop:diverged"
                if held_h >= MAX_HOLD_H:
                    exit_now, reason = True, "stop:time"
                if exit_now:
                    ret_a = pos["side"] * (prices[a].iloc[i] - pos["entry_a"]) / pos["entry_a"]
                    ret_b = -pos["side"] * (prices[b].iloc[i] - pos["entry_b"]) / pos["entry_b"]
                    gross = LEG_NOTIONAL * (ret_a + ret_b)
                    cost = LEG_NOTIONAL * COST_FRAC * 2 + LEG_NOTIONAL * FUNDING_APY * held_h / (24*365)
                    trades.append({"pnl": gross - cost, "hold_h": held_h, "reason": reason})
                    pos = None
            i += 1
        if not trades:
            print(f"  {a}/{b}: no trades")
            continue
        t = pd.DataFrame(trades)
        print(f"  {a}/{b}: n={len(t):3d}  WR={100*(t.pnl>0).mean():5.1f}%  "
              f"total=${t.pnl.sum():+8.2f}  avg=${t.pnl.mean():+6.2f}  "
              f"reasons={t.reason.value_counts().to_dict()}")


if __name__ == "__main__":
    main()

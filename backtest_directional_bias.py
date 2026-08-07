#!/usr/bin/env python3
"""
Compare directional-bias indicators for the lev_perp arm family (research,
2026-07-27).

lev_perp currently picks side = sign(close - SMA(50)) -- a lagging, simple
signal. This tests whether a different indicator gives a better directional
bias, holding EVERYTHING else fixed: the same 4 entry filters (RSI<45,
trend-age>=8d on the candidate's own direction persistence, vol>=1.2x,
ADX-dead-zone skip), the same v1 fixed 5% TP / 5% stop / liquidation exit,
the same production cost model (0.15% round-trip + 10% APY funding), 3x
leverage, $333 margin/symbol, on 5 years of OKX daily bars (resampled from
the cached 1h data), BTC/ETH/SOL.

Candidates:
  A. SMA(50) sign            -- current production signal (baseline)
  B. Supertrend(10, 2.5)     -- ATR-band trend-flip, manual implementation
  C. MACD(12,26,9) histogram -- sign of MACD line minus signal line
  D. DMI (DI+ vs DI-)        -- directional movement index (Wilder), the
                                 same family as the ADX already computed
                                 for the dead-zone filter -- natural pairing
  E. EMA(21) vs EMA(55) cross

Each candidate also gets tested with the v2 ATR(14)x2 chandelier exit
instead of the fixed TP/SL, since that's the better-proven exit mechanism.
"""
import numpy as np
import pandas as pd
from pathlib import Path

CACHE_DIR = Path(__file__).parent / "data" / "cache_1h"
SYMBOLS = ["BTC_USDT", "ETH_USDT", "SOL_USDT"]

RSI_PERIOD, RSI_MAX = 14, 45.0
MIN_TREND_AGE = 8
VOL_MULT = 1.2
ADX_PERIOD = 14
SKIP_ADX_DEAD_ZONE = (20, 30)

LEVERAGE, MARGIN, MAINT = 3.0, 333.0, 0.05
TP_FRAC, SL_FRAC = 0.05, 0.05
LIQ_FRAC = (1 - MAINT) / LEVERAGE
COST_FRAC, FUNDING_APY = 0.0015, 0.10
ATR_MULT_V2 = 2.0


def load_daily(sym: str) -> pd.DataFrame:
    df = pd.read_pickle(CACHE_DIR / f"{sym}_1825d.parquet")
    df["dt"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
    d = df.set_index("dt").resample("1D").agg(
        open=("open", "first"), high=("high", "max"),
        low=("low", "min"), close=("close", "last"),
        volume=("volume", "sum"),
    ).dropna()
    return d.reset_index()


def rsi(close: pd.Series, period: int) -> pd.Series:
    delta = close.diff()
    up = delta.clip(lower=0).ewm(alpha=1 / period, adjust=False).mean()
    down = (-delta.clip(upper=0)).ewm(alpha=1 / period, adjust=False).mean()
    rs = up / down.replace(0, np.nan)
    return 100 - 100 / (1 + rs)


def true_range(df: pd.DataFrame) -> pd.Series:
    pc = df.close.shift()
    return pd.concat([df.high - df.low, (df.high - pc).abs(), (df.low - pc).abs()], axis=1).max(axis=1)


def atr_wilder(df: pd.DataFrame, period: int) -> pd.Series:
    return true_range(df).ewm(alpha=1 / period, adjust=False).mean()


def adx_dmi(df: pd.DataFrame, period: int = ADX_PERIOD):
    up_move = df.high.diff()
    down_move = -df.low.diff()
    plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
    minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)
    tr = true_range(df)
    atr_s = tr.ewm(alpha=1 / period, adjust=False).mean()
    plus_di = 100 * pd.Series(plus_dm, index=df.index).ewm(alpha=1 / period, adjust=False).mean() / atr_s
    minus_di = 100 * pd.Series(minus_dm, index=df.index).ewm(alpha=1 / period, adjust=False).mean() / atr_s
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di)
    adx = dx.ewm(alpha=1 / period, adjust=False).mean()
    return adx, plus_di, minus_di


def supertrend_dir(df: pd.DataFrame, period: int = 10, mult: float = 2.5) -> pd.Series:
    hl2 = (df.high + df.low) / 2.0
    atr_s = atr_wilder(df, period)
    upper_basic = hl2 + mult * atr_s
    lower_basic = hl2 - mult * atr_s
    upper = upper_basic.copy()
    lower = lower_basic.copy()
    direction = pd.Series(1, index=df.index)
    close = df.close.values
    for i in range(1, len(df)):
        pu, pl, pc = upper.iat[i - 1], lower.iat[i - 1], close[i - 1]
        upper.iat[i] = upper_basic.iat[i] if (upper_basic.iat[i] < pu or pc > pu) else pu
        lower.iat[i] = lower_basic.iat[i] if (lower_basic.iat[i] > pl or pc < pl) else pl
        pdir = direction.iat[i - 1]
        ci = close[i]
        if pdir == -1 and ci > upper.iat[i]:
            direction.iat[i] = 1
        elif pdir == 1 and ci < lower.iat[i]:
            direction.iat[i] = -1
        else:
            direction.iat[i] = pdir
    return direction


def macd_hist_sign(close: pd.Series, fast=12, slow=26, sig=9) -> pd.Series:
    macd_line = close.ewm(span=fast, adjust=False).mean() - close.ewm(span=slow, adjust=False).mean()
    signal = macd_line.ewm(span=sig, adjust=False).mean()
    return np.sign(macd_line - signal).replace(0, np.nan).ffill().fillna(1)


def ema_cross_sign(close: pd.Series, fast=21, slow=55) -> pd.Series:
    return np.sign(close.ewm(span=fast, adjust=False).mean() - close.ewm(span=slow, adjust=False).mean())


def build_indicators(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["rsi"] = rsi(df.close, RSI_PERIOD)
    df["vol_ma20"] = df.volume.rolling(20).mean()
    df["vol_ratio"] = df.volume / df.vol_ma20
    df["adx"], df["plus_di"], df["minus_di"] = adx_dmi(df)
    df["atr14"] = atr_wilder(df, 14)

    df["side_sma50"] = np.sign(df.close - df.close.rolling(50).mean())
    st_dir = supertrend_dir(df, 10, 2.5)
    df["side_supertrend"] = st_dir
    df["side_macd"] = macd_hist_sign(df.close)
    df["side_dmi"] = np.sign(df.plus_di - df.minus_di)
    df["side_emacross"] = ema_cross_sign(df.close)
    return df


def trend_age(side: pd.Series) -> pd.Series:
    age = np.zeros(len(side), dtype=int)
    run = 0
    prev = None
    vals = side.values
    for i, v in enumerate(vals):
        if pd.isna(v):
            run = 0
        elif v == prev:
            run += 1
        else:
            run = 1
        age[i] = run
        prev = v
    return pd.Series(age, index=side.index)


def run_backtest(df: pd.DataFrame, side_col: str, exit_style: str) -> list:
    df = df.copy()
    df["age"] = trend_age(df[side_col])
    trades = []
    i, n = 60, len(df)
    pos = None
    while i < n - 1:
        row = df.iloc[i]
        if pos is None:
            ok = (
                pd.notna(row.rsi) and row.rsi < RSI_MAX
                and row.age >= MIN_TREND_AGE
                and pd.notna(row.vol_ratio) and row.vol_ratio >= VOL_MULT
                and pd.notna(row.adx) and not (SKIP_ADX_DEAD_ZONE[0] <= row.adx <= SKIP_ADX_DEAD_ZONE[1])
                and pd.notna(row[side_col])
            )
            if ok:
                side = int(row[side_col])
                entry_i = i + 1
                entry = df.iloc[entry_i].open
                notional = MARGIN * LEVERAGE
                qty = notional / entry
                if exit_style == "fixed":
                    tp = entry * (1 + side * TP_FRAC)
                    sl = entry * (1 - side * SL_FRAC)
                    liq = entry * (1 - side * LIQ_FRAC)
                    peak = trough = None
                else:
                    atr0 = df.iloc[entry_i].atr14
                    liq = entry * (1 - side * LIQ_FRAC)
                    peak = trough = entry
                    trail = entry - side * ATR_MULT_V2 * atr0
                j = entry_i
                exit_px, reason = None, None
                while j < n:
                    b = df.iloc[j]
                    if exit_style == "fixed":
                        if side > 0:
                            if b.low <= liq:
                                exit_px, reason = liq, "liq"; break
                            if b.low <= sl:
                                exit_px, reason = sl, "sl"; break
                            if b.high >= tp:
                                exit_px, reason = tp, "tp"; break
                        else:
                            if b.high >= liq:
                                exit_px, reason = liq, "liq"; break
                            if b.high >= sl:
                                exit_px, reason = sl, "sl"; break
                            if b.low <= tp:
                                exit_px, reason = tp, "tp"; break
                    else:
                        if side > 0:
                            if b.low <= liq:
                                exit_px, reason = liq, "liq"; break
                            if b.low <= trail:
                                exit_px, reason = trail, "trail"; break
                            peak = max(peak, b.high)
                            new_trail = peak - ATR_MULT_V2 * atr0
                            trail = max(trail, new_trail)
                        else:
                            if b.high >= liq:
                                exit_px, reason = liq, "liq"; break
                            if b.high >= trail:
                                exit_px, reason = trail, "trail"; break
                            trough = min(trough, b.low)
                            new_trail = trough + ATR_MULT_V2 * atr0
                            trail = min(trail, new_trail)
                    # trend-flip exit check for fixed-style (v1 semantics)
                    if pd.notna(b[side_col]) and int(b[side_col]) != side:
                        exit_px, reason = b.close, "flip"
                        break
                    j += 1
                if exit_px is None:
                    j = n - 1
                    exit_px, reason = df.iloc[j].close, "eod"
                hours = (j - entry_i) * 24
                gross = side * qty * (exit_px - entry)
                cost = notional * COST_FRAC + notional * FUNDING_APY * hours / (24 * 365)
                trades.append({"entry_i": entry_i, "exit_i": j, "pnl": gross - cost, "reason": reason})
                i = j + 1
                continue
        i += 1
    return trades


def summarize(name: str, trades: list, dfs_dt) -> dict:
    if not trades:
        return {"variant": name, "n": 0}
    t = pd.DataFrame(trades)
    eq = t.pnl.cumsum()
    dd = (eq - eq.cummax()).min()
    return {
        "variant": name, "n": len(t), "wr": round(100 * (t.pnl > 0).mean(), 1),
        "total_pnl": round(t.pnl.sum(), 2), "avg_pnl": round(t.pnl.mean(), 2),
        "maxDD": round(dd, 2), "reasons": t.reason.value_counts().to_dict(),
    }


def main():
    data = {s: build_indicators(load_daily(s)) for s in SYMBOLS}

    side_cols = {
        "A_SMA50": "side_sma50",
        "B_Supertrend": "side_supertrend",
        "C_MACD": "side_macd",
        "D_DMI": "side_dmi",
        "E_EMAcross": "side_emacross",
    }

    print(f"{'variant':16s} {'exit':8s} {'n':>4s} {'WR%':>6s} {'total$':>9s} {'avg$':>8s} {'maxDD$':>8s}  reasons")
    for label, col in side_cols.items():
        for exit_style in ["fixed", "chandelier"]:
            all_trades = []
            for s in SYMBOLS:
                all_trades += run_backtest(data[s], col, exit_style)
            summ = summarize(f"{label}", all_trades, None)
            if summ["n"] == 0:
                print(f"{label:16s} {exit_style:8s}    0 trades")
                continue
            print(f"{label:16s} {exit_style:8s} {summ['n']:4d} {summ['wr']:6.1f} "
                  f"{summ['total_pnl']:9.2f} {summ['avg_pnl']:8.2f} {summ['maxDD']:8.2f}  {summ['reasons']}")


if __name__ == "__main__":
    main()

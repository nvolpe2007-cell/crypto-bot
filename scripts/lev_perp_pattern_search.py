#!/usr/bin/env python3
"""
lev_perp pattern search — does a DIFFERENT entry signal beat the current SMA50 core?

Owner ask (2026-07-12): "find a new pattern for the lev perps to trade, maybe a
slight variation of the current strategy using the best indicators to find trends
EARLY." The live arm enters on daily close-vs-SMA50 and then WAITS (MIN_TREND_AGE>=8)
for a mature trend. This screen tests the opposite hypothesis — earlier-trend
indicators — against that baseline, holding the arm's mechanics FIXED so only the
signal varies.

MECHANICS (identical to lev_perp_paper.py, so net$ is comparable to the live record):
  * 3x flat leverage, margin $333.33 -> notional $1000 per trade.
  * TP at +5% favorable price move; hard STOP at -5% adverse; LIQUIDATION at
    (1-0.05)/3 = 31.67% adverse. Intrabar ordering: liquidation > stop > TP.
  * Flip-exit at bar close when the signal reverses; re-enter in signal direction.
  * Costs: 0.15% taker round-trip on notional + 10% APY funding drag on notional.
  * No lookahead: signal uses bars up to and including bar i's close; the position
    opens at that close; exits are evaluated from bar i+1's high/low onward.

HONEST FRAMING: 720 daily bars (2yr) x 3 coins ~= a few dozen trades per signal.
This is a comparative SCREEN, not a proof. A winner earns a forward paper test on a
separate ledger (same bar as every arm: n>=30, family-wise t). In-sample / out-of-sample
split is printed so a signal that only works on the fit window is exposed.

    python scripts/lev_perp_pattern_search.py
"""
from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field

import ccxt

SYMBOLS = {"BTC": "BTC/USD", "ETH": "ETH/USD", "SOL": "SOL/USD"}
BARS = 720

# ── mechanics (mirror lev_perp_paper.py) ───────────────────────────────────────
LEVERAGE = 3.0
MARGIN = 333.33
NOTIONAL = MARGIN * LEVERAGE            # ~1000
TP_FRAC = 0.05
SL_FRAC = 0.05
MAINT = 0.05
LIQ_FRAC = (1.0 - MAINT) / LEVERAGE     # 0.31667
COST_FRAC = 0.0015
FUNDING_APY = 0.10
SEC_PER_YEAR = 365 * 24 * 3600
OOS_FRAC = 0.40                          # last 40% of the series is out-of-sample


def fetch(sym: str) -> list[dict]:
    ex = ccxt.kraken({"enableRateLimit": True})
    o = ex.fetch_ohlcv(sym, timeframe="1d", limit=BARS)
    return [{"t": r[0] // 1000, "h": r[2], "l": r[3], "c": r[4], "v": r[5]} for r in o]


# ── indicator helpers ──────────────────────────────────────────────────────────
def sma(xs, n):
    return sum(xs[-n:]) / n if len(xs) >= n else None


def ema(xs, n):
    if len(xs) < n:
        return None
    k = 2 / (n + 1)
    e = sum(xs[:n]) / n
    for x in xs[n:]:
        e = x * k + e * (1 - k)
    return e


def rsi(xs, n=14):
    if len(xs) < n + 1:
        return None
    d = [xs[i] - xs[i - 1] for i in range(len(xs) - n, len(xs))]
    g = sum(max(x, 0) for x in d) / n
    loss = sum(-min(x, 0) for x in d) / n
    return 100.0 if loss == 0 else 100 - 100 / (1 + g / loss)


def atr(bars, n=14):
    if len(bars) < n + 1:
        return None
    trs = []
    for i in range(len(bars) - n, len(bars)):
        h, l, pc = bars[i]["h"], bars[i]["l"], bars[i - 1]["c"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    return sum(trs) / n


def adx(bars, n=14):
    if len(bars) < 2 * n:
        return None
    pdm, mdm, trs = [], [], []
    for i in range(1, len(bars)):
        up = bars[i]["h"] - bars[i - 1]["h"]
        dn = bars[i - 1]["l"] - bars[i]["l"]
        pdm.append(up if up > dn and up > 0 else 0.0)
        mdm.append(dn if dn > up and dn > 0 else 0.0)
        pc = bars[i - 1]["c"]
        trs.append(max(bars[i]["h"] - bars[i]["l"], abs(bars[i]["h"] - pc), abs(bars[i]["l"] - pc)))
    a = sum(trs[-n:]) / n
    if a == 0:
        return None, 0, 0
    pdi = 100 * (sum(pdm[-n:]) / n) / a
    mdi = 100 * (sum(mdm[-n:]) / n) / a
    dx = 100 * abs(pdi - mdi) / (pdi + mdi) if (pdi + mdi) else 0
    return dx, pdi, mdi


# ── signals: bars_so_far (inclusive of current bar) -> desired side (+1/-1/0) ────
def s_baseline_raw(bars):
    c = [b["c"] for b in bars]
    m = sma(c, 50)
    return 0 if m is None else (1 if c[-1] >= m else -1)


def s_baseline_filtered(bars):
    """The LIVE config: SMA50 direction + RSI<45 + trend_age>=8 + vol>1.2x + skip ADX 20-30."""
    c = [b["c"] for b in bars]
    m = sma(c, 50)
    if m is None:
        return 0
    side = 1 if c[-1] >= m else -1
    r = rsi(c, 14)
    if r is not None and r >= 45:
        return 0
    # trend age
    age = 0
    for i in range(len(c) - 1, 49, -1):
        s = sma(c[:i], 50)
        if s is None:
            break
        if (1 if c[i - 1] >= s else -1) != side:
            break
        age += 1
    if age < 8:
        return 0
    if len(bars) > 21:
        avgv = sum(b["v"] for b in bars[-21:-1]) / 20
        if avgv > 0 and bars[-1]["v"] / avgv < 1.2:
            return 0
    ad = adx(bars[-30:], 14)
    if ad and ad[0] is not None and 20 <= ad[0] <= 30:
        return 0
    return side


def s_sma_cross_20_50(bars):
    c = [b["c"] for b in bars]
    f, s = sma(c, 20), sma(c, 50)
    if f is None or s is None:
        return 0
    return 1 if f >= s else -1


def s_ema_cross_12_26(bars):
    c = [b["c"] for b in bars]
    f, s = ema(c, 12), ema(c, 26)
    if f is None or s is None:
        return 0
    return 1 if f >= s else -1


def s_donchian_20(bars):
    """Turtle breakout: close above 20d high -> long, below 20d low -> short (early trend)."""
    if len(bars) < 21:
        return 0
    hh = max(b["h"] for b in bars[-21:-1])
    ll = min(b["l"] for b in bars[-21:-1])
    c = bars[-1]["c"]
    if c > hh:
        return 1
    if c < ll:
        return -1
    return 0  # inside channel: hold whatever (engine keeps prior)


def s_macd_hist(bars):
    c = [b["c"] for b in bars]
    f, s = ema(c, 12), ema(c, 26)
    if f is None or s is None:
        return 0
    macd = f - s
    # signal line = EMA9 of macd; approximate via recent macd series
    hist = [ema(c[:i + 1], 12) - ema(c[:i + 1], 26) for i in range(len(c) - 9, len(c))
            if ema(c[:i + 1], 12) is not None and ema(c[:i + 1], 26) is not None]
    if len(hist) < 9:
        return 1 if macd >= 0 else -1
    sig = sum(hist) / len(hist)
    return 1 if macd >= sig else -1


def s_roc_10(bars):
    """Momentum: 10-day rate of change sign (fast trend)."""
    c = [b["c"] for b in bars]
    if len(c) < 11:
        return 0
    roc = c[-1] / c[-11] - 1
    return 1 if roc >= 0 else -1


def s_adx_di_cross(bars):
    """Early-but-confirmed: DI cross with ADX>20 (trend initiation)."""
    ad = adx(bars[-40:], 14)
    if not ad or ad[0] is None:
        return 0
    dx, pdi, mdi = ad
    if dx < 20:
        return 0
    return 1 if pdi >= mdi else -1


def s_supertrend(bars):
    """Close vs ATR band around SMA20 (trend-following breakout, early)."""
    c = [b["c"] for b in bars]
    m = sma(c, 20)
    a = atr(bars[-30:], 14)
    if m is None or a is None:
        return 0
    upper, lower = m + 1.5 * a, m - 1.5 * a
    if c[-1] > upper:
        return 1
    if c[-1] < lower:
        return -1
    return 0


def s_tsmom_20(bars):
    """Short-lookback time-series momentum: price vs price 20 bars ago (robust winner prior)."""
    c = [b["c"] for b in bars]
    if len(c) < 21:
        return 0
    return 1 if c[-1] >= c[-21] else -1


SIGNALS = {
    "baseline_SMA50_raw": s_baseline_raw,
    "baseline_LIVE_filtered": s_baseline_filtered,
    "sma_cross_20_50": s_sma_cross_20_50,
    "ema_cross_12_26": s_ema_cross_12_26,
    "donchian_20_breakout": s_donchian_20,
    "macd_hist": s_macd_hist,
    "roc_10_momentum": s_roc_10,
    "adx_di_cross": s_adx_di_cross,
    "supertrend_atr20": s_supertrend,
    "tsmom_20": s_tsmom_20,
}


# ── backtest engine ─────────────────────────────────────────────────────────────
@dataclass
class Trade:
    side: int
    entry: float
    exit: float
    reason: str
    net: float
    bars_held: int
    oos: bool


def backtest(bars: list[dict], signal_fn, warmup=51) -> list[Trade]:
    trades: list[Trade] = []
    pos = None  # dict: side, entry, tp, sl, liq, i_open
    oos_start = int(len(bars) * (1 - OOS_FRAC))

    def close(exit_px, reason, i):
        nonlocal pos
        side = pos["side"]
        ret = side * (exit_px - pos["entry"]) / pos["entry"]
        gross = NOTIONAL * ret
        cost = NOTIONAL * COST_FRAC
        held = i - pos["i_open"]
        funding = NOTIONAL * FUNDING_APY * (held * 86400) / SEC_PER_YEAR
        net = gross - cost - funding
        trades.append(Trade(side, pos["entry"], exit_px, reason, net, held,
                            pos["i_open"] >= oos_start))
        pos = None

    for i in range(warmup, len(bars)):
        bar = bars[i]
        # 1) exit existing position on THIS bar's high/low (opened on a prior bar)
        if pos is not None:
            side = pos["side"]
            ex = None
            if side > 0:
                if bar["l"] <= pos["liq"]:
                    ex = (pos["liq"], "liquidation")
                elif bar["l"] <= pos["sl"]:
                    ex = (pos["sl"], "stop_loss")
                elif bar["h"] >= pos["tp"]:
                    ex = (pos["tp"], "take_profit")
            else:
                if bar["h"] >= pos["liq"]:
                    ex = (pos["liq"], "liquidation")
                elif bar["h"] >= pos["sl"]:
                    ex = (pos["sl"], "stop_loss")
                elif bar["l"] <= pos["tp"]:
                    ex = (pos["tp"], "take_profit")
            if ex:
                close(ex[0], ex[1], i)

        want = signal_fn(bars[: i + 1])
        # 2) flip exit at bar close
        if pos is not None and want != 0 and want != pos["side"]:
            close(bar["c"], "flip", i)
        # 3) enter at bar close if flat and signal has a direction
        if pos is None and want != 0:
            px = bar["c"]
            pos = {
                "side": want, "entry": px, "i_open": i,
                "tp": px * (1 + want * TP_FRAC),
                "sl": px * (1 - want * SL_FRAC),
                "liq": px * (1 - want * LIQ_FRAC),
            }
    return trades


def stats(trades: list[Trade]) -> dict:
    if not trades:
        return {"n": 0}
    nets = [t.net for t in trades]
    wins = [t for t in trades if t.net > 0]
    liqs = sum(1 for t in trades if t.reason == "liquidation")
    tps = sum(1 for t in trades if t.reason == "take_profit")
    # equity path for maxDD
    eq, peak, dd = 0.0, 0.0, 0.0
    for n in nets:
        eq += n
        peak = max(peak, eq)
        dd = min(dd, eq - peak)
    exp = sum(nets) / len(nets)
    sd = statistics.pstdev(nets) if len(nets) > 1 else 0.0
    t_stat = (exp / sd * math.sqrt(len(nets))) if sd > 0 else 0.0
    return {
        "n": len(trades), "net": sum(nets), "exp": exp, "win": len(wins) / len(trades),
        "tps": tps, "liqs": liqs, "maxDD": dd, "t": t_stat,
        "avg_hold": sum(t.bars_held for t in trades) / len(trades),
    }


def main():
    print("Fetching 720d BTC/ETH/SOL from Kraken ...")
    data = {b: fetch(s) for b, s in SYMBOLS.items()}
    n = len(next(iter(data.values())))
    oos_start = int(n * (1 - OOS_FRAC))
    print(f"{n} bars each; in-sample first {oos_start}, OOS last {n - oos_start} "
          f"(~{(n-oos_start)}d ~40%)\n")

    rows = []
    for name, fn in SIGNALS.items():
        pooled = []
        for b in SYMBOLS:
            pooled += backtest(data[b], fn)
        full = stats(pooled)
        oos = stats([t for t in pooled if t.oos])
        ins = stats([t for t in pooled if not t.oos])
        rows.append((name, full, ins, oos))

    hdr = f"{'signal':<24} {'n':>3} {'net$':>8} {'exp$':>7} {'win%':>5} {'TP/liq':>7} {'maxDD$':>8} {'t':>6} {'hold':>5}"
    print("=" * len(hdr))
    print("FULL 2-YEAR (pooled BTC+ETH+SOL, 3x, 5%TP/5%SL)")
    print(hdr)
    print("-" * len(hdr))
    for name, full, ins, oos in rows:
        if full["n"] == 0:
            print(f"{name:<24}   0  (no trades)")
            continue
        print(f"{name:<24} {full['n']:>3} {full['net']:>8.0f} {full['exp']:>7.2f} "
              f"{full['win']*100:>4.0f}% {full['tps']:>3}/{full['liqs']:<3} "
              f"{full['maxDD']:>8.0f} {full['t']:>6.2f} {full['avg_hold']:>5.1f}")

    print("\n" + "=" * len(hdr))
    print("OUT-OF-SAMPLE ONLY (last 40% — the honest test)")
    print(f"{'signal':<24} {'n':>3} {'net$':>8} {'exp$':>7} {'win%':>5} {'t':>6}   (in-sample exp$ for overfit check)")
    print("-" * len(hdr))
    for name, full, ins, oos in rows:
        if oos.get("n", 0) == 0:
            print(f"{name:<24}   0")
            continue
        flag = ""
        if ins.get("n", 0) and ins["exp"] > 0 and oos["exp"] < 0:
            flag = "  <- overfit (IS+ OOS-)"
        print(f"{name:<24} {oos['n']:>3} {oos['net']:>8.0f} {oos['exp']:>7.2f} "
              f"{oos['win']*100:>4.0f}% {oos['t']:>6.2f}   IS_exp={ins.get('exp',0):>6.2f}{flag}")

    print("\nNOTE: screen only (~2yr, one macro regime, few dozen trades/signal).")
    print("A signal earns nothing until it clears the forward proof bar (n>=30, family-wise t).")


if __name__ == "__main__":
    main()

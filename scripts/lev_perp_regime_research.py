#!/usr/bin/env python3
"""
Lev-perp regime research (2026-07-26) — owner asked: "make lev_perp really
profitable... come up with new things using science and math, no internet."

Reuses the EXACT production cost/margin/entry-filter machinery from
lev_perp_paper.py (v1) and lev_perp_v2_paper.py (v2, chandelier exit) so every
candidate is judged on equal footing against the two arms already live. Adds
five new math-derived mechanics, each a standard statistical/econometric tool
applied from first principles (not looked up as a "strategy"):

  1. Hurst exponent gate (R/S analysis, Hurst 1951/Mandelbrot) — only enter
     when the market is in a persistent (trending) regime, H > threshold.
  2. Lag-1 autocorrelation gate — direct empirical trend-continuation test.
  3. GARCH(1,1) one-step vol forecast (grid-search MLE) replacing realized-vol
     in the leverage sizing formula — should react faster to vol-regime shifts
     than a rolling stdev.
  4. Kelly-criterion edge-adjusted leverage (f* = mu/sigma^2, continuous-time
     Kelly) instead of pure vol-targeting — sizes UP when trend/vol ratio is
     favorable, not just when vol is low.
  5. Chop-timeout exit — cut a position early if it hasn't achieved any
     favorable excursion within N days (regime probably wrong).

Data: Coinbase daily OHLCV via ccxt (public, keyless), BTC/ETH/SOL, longest
common window (bounded by SOL's ~2021-06 listing, matching the existing
RESEARCH_2026-07-02_lev_perp_5yr_replay.md window for comparability).

Every candidate's closed trades feed proof_scorecard's REAL stats machinery
(_stats, _family_t_bar, _expected_max_sharpe, _deflated_sharpe) — the same
bar every other arm in this repo is judged against. Nothing reinvented.
"""
from __future__ import annotations

import json
import math
import os
import statistics as st
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import lev_perp_paper as lp
import lev_perp_v2_paper as v2mod
from proof_scorecard import _stats, _family_t_bar, _expected_max_sharpe, _deflated_sharpe, DSR_MIN

CACHE = Path(__file__).resolve().parent.parent / "data" / "research_cache_lev_perp_daily.json"
SYMBOLS = {"BTC": "BTC/USD", "ETH": "ETH/USD", "SOL": "SOL/USD"}
START_ISO = "2021-06-01T00:00:00Z"  # SOL's Coinbase listing floor; matches the prior 5yr replay window


# ── Data ─────────────────────────────────────────────────────────────────────
def fetch_all_daily() -> dict[str, list[dict]]:
    if CACHE.exists():
        cached = json.loads(CACHE.read_text())
        if all(b in cached for b in SYMBOLS):
            print(f"[data] using cache {CACHE}")
            return cached
    import ccxt
    ex = ccxt.coinbase({"enableRateLimit": True})
    since = ex.parse8601(START_ISO)
    out: dict[str, list[dict]] = {}
    for base, pair in SYMBOLS.items():
        print(f"[data] fetching {pair} daily from {START_ISO} ...")
        bars: list[dict] = []
        cursor = since
        now_ms = int(time.time() * 1000)
        stall = 0
        while cursor < now_ms:
            chunk = ex.fetch_ohlcv(pair, timeframe="1d", since=cursor, limit=300)
            if not chunk:
                stall += 1
                if stall >= 3:
                    break
                cursor += 300 * 86_400_000  # skip a data gap
                continue
            stall = 0
            for row in chunk:
                bars.append({"t": int(row[0] // 1000), "o": row[1], "h": row[2],
                             "l": row[3], "c": row[4], "v": row[5]})
            new_cursor = chunk[-1][0] + 86_400_000
            if new_cursor <= cursor:
                break  # not advancing -> stop
            cursor = new_cursor
            time.sleep(ex.rateLimit / 1000.0)
        # de-dup + sort + drop the in-progress last bar (today, incomplete)
        seen = {}
        for b in bars:
            seen[b["t"]] = b
        bars = sorted(seen.values(), key=lambda b: b["t"])
        today_cutoff = int(time.time() // 86400 * 86400)
        bars = [b for b in bars if b["t"] < today_cutoff]
        out[base] = bars
        print(f"[data] {base}: {len(bars)} daily bars [{bars[0]['t']} -> {bars[-1]['t']}]")
    # truncate to common start across symbols
    common_start = max(b[0]["t"] for b in out.values())
    for base in out:
        out[base] = [b for b in out[base] if b["t"] >= common_start]
    CACHE.parent.mkdir(exist_ok=True)
    CACHE.write_text(json.dumps(out))
    return out


# ── New math-derived mechanics ───────────────────────────────────────────────
def _returns(closes: list[float]) -> list[float]:
    return [(closes[i] / closes[i - 1] - 1) * 100.0 for i in range(1, len(closes))]


def hurst_exponent(closes: list[float], window: int = 150) -> float | None:
    """Rescaled-range (R/S) Hurst exponent over the trailing `window` daily
    returns. H > 0.5 = persistent/trending, H < 0.5 = mean-reverting/choppy,
    H = 0.5 = random walk. Classic Hurst(1951)/Mandelbrot estimator."""
    rets = _returns(closes[-(window + 1):])
    n = len(rets)
    if n < 40:
        return None
    sizes = [s for s in (10, 15, 20, 30, 40, 60, 80) if s <= n // 2]
    if len(sizes) < 3:
        return None
    xs, ys = [], []
    for size in sizes:
        rs_vals = []
        for start in range(0, n - size + 1, size):
            chunk = rets[start:start + size]
            mean = sum(chunk) / size
            dev = [x - mean for x in chunk]
            cum, s = [], 0.0
            for d in dev:
                s += d
                cum.append(s)
            R = max(cum) - min(cum)
            S = (sum((x - mean) ** 2 for x in chunk) / size) ** 0.5
            if S > 0:
                rs_vals.append(R / S)
        if rs_vals:
            avg = sum(rs_vals) / len(rs_vals)
            if avg > 0:
                xs.append(math.log(size))
                ys.append(math.log(avg))
    if len(xs) < 3:
        return None
    mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    sxx = sum((x - mx) ** 2 for x in xs)
    return sxy / sxx if sxx else None


def autocorr_1(closes: list[float], window: int = 20) -> float | None:
    """Lag-1 autocorrelation of daily returns over the trailing window."""
    rets = _returns(closes[-(window + 1):])
    n = len(rets)
    if n < 10:
        return None
    mean = sum(rets) / n
    c0 = sum((r - mean) ** 2 for r in rets)
    if c0 == 0:
        return None
    c1 = sum((rets[i] - mean) * (rets[i - 1] - mean) for i in range(1, n))
    return c1 / c0


_GARCH_CACHE: dict[str, tuple[int, tuple]] = {}


def garch_vol_forecast(closes: list[float], key: str, fit_window: int = 250,
                        refit_every: int = 5) -> float | None:
    """GARCH(1,1) one-step-ahead volatility forecast, grid-search MLE over
    (alpha, beta) with omega pinned to the unconditional-variance target.
    Refit every `refit_every` bars per series (keyed by symbol) to bound cost;
    otherwise reuses the last fitted (omega, alpha, beta) rolled forward one step."""
    rets = _returns(closes[-(fit_window + 1):])
    n = len(rets)
    if n < 60:
        return None
    idx = len(closes)
    cached = _GARCH_CACHE.get(key)
    if cached and idx - cached[0] < refit_every:
        omega, alpha, beta = cached[1]
    else:
        mean = sum(rets) / n
        dev = [r - mean for r in rets]
        var0 = sum(x * x for x in dev) / n
        best = None
        for alpha in (0.03, 0.05, 0.08, 0.10, 0.13, 0.16):
            for beta in (0.75, 0.80, 0.85, 0.88, 0.90, 0.92):
                if alpha + beta >= 0.999 or var0 <= 0:
                    continue
                omega = var0 * (1 - alpha - beta)
                if omega <= 0:
                    continue
                h = var0
                ll = 0.0
                ok = True
                for x in dev:
                    if h <= 0:
                        ok = False
                        break
                    ll += -0.5 * (math.log(2 * math.pi * h) + x * x / h)
                    h = omega + alpha * x * x + beta * h
                if ok and (best is None or ll > best[0]):
                    best = (ll, omega, alpha, beta)
        if best is None:
            return None
        _, omega, alpha, beta = best
        _GARCH_CACHE[key] = (idx, (omega, alpha, beta))
    # roll the recursion forward from the fit window to get today's h, then forecast one step
    mean = sum(rets) / n
    h = sum((r - mean) ** 2 for r in rets) / n
    for r in rets:
        x = r - mean
        h = omega + alpha * x * x + beta * h
    return math.sqrt(h)


def kelly_leverage(closes: list[float], window: int = 20, vol_n: int = 20,
                    cap: float = 3.0) -> float | None:
    """Continuous-time Kelly f* = mu / sigma^2 using the trailing drift and
    realized vol as mu/sigma estimators. Sized on MAGNITUDE (direction is
    already decided by the SMA signal); capped like the production leverage."""
    if len(closes) < window + vol_n + 1:
        return None
    drift = (closes[-1] / closes[-window - 1] - 1) / window  # avg daily return, fraction
    vol = lp._realized_vol(closes, vol_n)
    if not vol:
        return None
    vol_frac = vol / 100.0
    if vol_frac <= 0:
        return None
    kelly = abs(drift) / (vol_frac ** 2)
    return max(0.1, min(cap, kelly))


# ── Generic v2-style simulator with pluggable gate / leverage / chop-timeout ─
def simulate(bars_by_symbol: dict[str, list[dict]], *, warmup: int = 160,
             gate_fn=None, lev_fn=None, chop_days: int = 0, chop_atr_mult: float = 1.0,
             label: str = "") -> dict:
    """Mirrors lev_perp_v2_paper's chandelier-exit loop bar-by-bar, with three
    optional hooks: gate_fn(closes, bars)->bool (extra entry gate on top of the
    production _entry_filter), lev_fn(closes, bars)->float|None (overrides
    vol-targeted leverage when it returns non-None), and chop_days>0 (exit if
    no favorable excursion of >= chop_atr_mult*ATR within chop_days)."""
    equity = lp.STARTING_EQUITY
    closed: list[dict] = []
    positions: dict[str, dict] = {}

    for base, bars in bars_by_symbol.items():
        if len(bars) < warmup:
            continue
        closes_full = [b["c"] for b in bars]
        pos = None
        for idx in range(warmup, len(bars)):
            bar = bars[idx]
            closes = closes_full[: idx + 1]
            bars_so_far = bars[: idx + 1]
            sma = lp._sma(closes, lp.SMA_N)
            if sma is None:
                continue

            # 1) exit vs prior-bar levels (liq > trail > chop-timeout)
            if pos:
                ex = v2mod._check_exit(pos, bar)
                if ex is None and chop_days > 0 and (idx - pos["open_idx"]) >= chop_days:
                    fav = pos["side"] * (pos["peak"] - pos["entry"]) / pos["entry"]
                    if fav < chop_atr_mult * pos["atr"] / pos["entry"]:
                        ex = (bar["c"], "chop_timeout")
                if ex:
                    price, reason = ex
                    rec = _close_generic(pos, price, str(bar["t"]), reason)
                    equity += rec["pnl"]
                    closed.append(rec)
                    pos = None

            # 2) trend-flip exit at close
            want = lp._target_side(bar["c"], sma)
            if pos and pos["side"] != want:
                rec = _close_generic(pos, bar["c"], str(bar["t"]), "flip")
                equity += rec["pnl"]
                closed.append(rec)
                pos = None

            # 3) ratchet survivor
            if pos:
                v2mod._ratchet(pos, bar)

            # 4) re-entry
            if not pos:
                skip: list = []
                if lp._entry_filter(bars_so_far, closes, skip) and (gate_fn is None or gate_fn(closes, bars_so_far)):
                    lev = None
                    if lev_fn is not None:
                        lev = lev_fn(closes, bars_so_far)
                    if lev is None:
                        lev = lp._effective_leverage(closes)
                    lev = max(0.1, min(lp.LEVERAGE, lev))
                    margin = lp.MARGIN  # single-position-at-a-time per symbol; no correlation cap needed intra-symbol
                    atr = v2mod._atr(bars_so_far) or bar["c"] * 0.02
                    pos = {
                        "symbol": base, "side": want, "entry": bar["c"], "entry_ts": str(bar["t"]),
                        "margin_usd": margin, "leverage": round(lev, 2),
                        "notional_usd": round(margin * lev, 2), "peak": bar["c"], "atr": round(atr, 8),
                        "trail": round(bar["c"] - want * v2mod.ATR_MULT * atr, 8),
                        "liq": round(bar["c"] * (1 - want * max(1e-6, (1 - lp.MAINT) / lev)), 8),
                        "open_idx": idx,
                    }
        # close any still-open position at the last available price (mark-to-market end of window)
        if pos:
            rec = _close_generic(pos, closes_full[-1], str(bars[-1]["t"]), "window_end")
            equity += rec["pnl"]
            closed.append(rec)

    return {"label": label, "closed": closed, "final_equity": equity}


def _close_generic(pos: dict, price: float, ts: str, reason: str) -> dict:
    side = pos["side"]
    notional = pos["notional_usd"]
    ret = side * (price - pos["entry"]) / pos["entry"]
    gross = notional * ret
    cost = notional * lp.TRADE_COST_FRAC
    funding = lp._funding_cost(notional, pos["entry_ts"], ts)
    net = gross - cost - funding
    return {"symbol": pos["symbol"], "side": side, "entry_ts": pos["entry_ts"], "exit_ts": ts,
            "pnl": net, "reason": reason, "leverage": pos["leverage"]}


def run_production_baseline(bars_by_symbol: dict[str, list[dict]], module, warmup: int, label: str) -> dict:
    """Replays the ACTUAL v1/v2 production code bar-by-bar (not the generic
    simulator) — seeds last_bar_t past warm-up, lets process_symbol walk history,
    exactly as RESEARCH_2026-07-02_lev_perp_5yr_replay.md did."""
    state = {"positions": {}, "closed": [], "last_bar_t": {}, "equity": lp.STARTING_EQUITY,
              "starting_equity": lp.STARTING_EQUITY}
    for base, bars in bars_by_symbol.items():
        if len(bars) < warmup + 1:
            continue
        state["last_bar_t"][base] = bars[warmup - 1]["t"]
        if module is lp:
            prices = {base: bars[-1]["c"]}
            os.environ["LEV_PERP_NOTIFY"] = "0"
            module.process_symbol(base, bars, state, prices)
        else:
            module.process_symbol(base, bars, state)
    return {"label": label, "closed": state["closed"], "final_equity": state.get("equity", lp.STARTING_EQUITY)}


# ── Stats / proof-bar reporting ──────────────────────────────────────────────
def week_cluster(ts: str) -> str:
    t = int(ts)
    return str(t // (7 * 86400))


def report(results: list[dict]) -> list[dict]:
    rows = []
    for r in results:
        nets = [c["pnl"] for c in r["closed"]]
        clusters = [week_cluster(c["entry_ts"]) for c in r["closed"]]
        s = _stats(nets, clusters)
        rows.append({**r, **s})
    return rows


def main():
    bars = fetch_all_daily()
    for base, b in bars.items():
        print(f"  {base}: {len(b)} bars, {b[0]['t']} -> {b[-1]['t']}")

    warmup = 160
    candidates = []
    candidates.append(run_production_baseline(bars, lp, warmup, "C0_v1_fixed_tp_sl"))
    candidates.append(run_production_baseline(bars, v2mod, warmup, "C1_v2_chandelier"))

    def gate_hurst(closes, _bars):
        h = hurst_exponent(closes)
        return h is not None and h > 0.5

    def gate_autocorr(closes, _bars):
        a = autocorr_1(closes)
        return a is not None and a > 0.0

    def lev_garch(closes, _bars):
        v = garch_vol_forecast(closes, key="garch")
        if v is None:
            return None
        return lp.VOL_TARGET / v if v > 0 else None

    def lev_kelly(closes, _bars):
        return kelly_leverage(closes, cap=lp.LEVERAGE)

    candidates.append(simulate(bars, warmup=warmup, gate_fn=gate_hurst, label="C2_v2_hurst_gate"))
    candidates.append(simulate(bars, warmup=warmup, gate_fn=gate_autocorr, label="C3_v2_autocorr_gate"))
    candidates.append(simulate(bars, warmup=warmup, lev_fn=lev_garch, label="C4_v2_garch_vol_sizing"))
    candidates.append(simulate(bars, warmup=warmup, lev_fn=lev_kelly, label="C5_v2_kelly_leverage"))
    candidates.append(simulate(bars, warmup=warmup, chop_days=15, chop_atr_mult=1.0, label="C6_v2_chop_timeout"))
    candidates.append(simulate(bars, warmup=warmup, gate_fn=gate_hurst, lev_fn=lev_kelly, label="C7_v2_hurst_plus_kelly"))

    rows = report(candidates)

    k = len(rows)
    t_family = _family_t_bar(k)
    sharpes = [r["sharpe"] for r in rows]
    sr0 = _expected_max_sharpe(sharpes)

    print("\n" + "=" * 100)
    print(f"k={k} candidates tested -> Sidak family t-bar = {t_family:.3f}   expected-max-Sharpe sr0={sr0:.4f}")
    print("=" * 100)
    out_rows = []
    for r in rows:
        dsr = _deflated_sharpe(r["sharpe"], r["eff_n"], r["skew"], r["kurt"], sr0)
        # split-half consistency
        nets_sorted = sorted(r["closed"], key=lambda c: c["entry_ts"])
        half = len(nets_sorted) // 2
        h1 = sum(c["pnl"] for c in nets_sorted[:half]) if half else 0.0
        h2 = sum(c["pnl"] for c in nets_sorted[half:]) if half else 0.0
        split_ok = h1 > 0 and h2 > 0
        passed = (r["n"] >= 30 and r["expectancy"] > 0 and r["t_clustered"] > t_family
                  and dsr > DSR_MIN)
        out_rows.append({**r, "dsr": dsr, "half1": h1, "half2": h2, "split_both_positive": split_ok,
                          "PRELIM_PASS": passed})
        print(f"{r['label']:28s} n={r['n']:4d} total=${r['total']:+9.2f} exp=${r['expectancy']:+7.3f} "
              f"WR={r['win_rate']*100:5.1f}% t={r['t_stat']:+6.2f} t_clu={r['t_clustered']:+6.2f} "
              f"sharpe={r['sharpe']:+6.3f} DSR={dsr:.3f} maxDD=${r['max_dd']:+8.2f} "
              f"split=({h1:+.1f}/{h2:+.1f}) PRELIM_PASS={passed}")

    out_path = Path(__file__).resolve().parent.parent / "data" / "lev_perp_regime_research_results.json"
    out_path.write_text(json.dumps({"k": k, "t_family": t_family, "sr0": sr0, "rows": [
        {kk: vv for kk, vv in row.items() if kk != "closed"} for row in out_rows
    ]}, indent=2, default=str))
    print(f"\nwrote {out_path}")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
Intraday REGIME-FOLLOWING arm — LONG in uptrends, SHORT in downtrends, flat else.

Two-sided trend/regime follower on an INTRADAY clock. Built deliberately to avoid
the cost wall that killed the 1m engine (move 0.1x cost): it runs on a higher
intraday timeframe where the move has a chance against the ~0.5% toll (1h ~1.9x,
4h ~2.8x — see scripts/timeframe_edge.py) AND it refuses entries when the bar's
expected move (ATR) is below a cost multiple. Shorts run as PAPER perps (a US
Kraken-spot account can't short spot).

Regime (pure-Python, timeframe-agnostic, trend-following):
  EMA(fast) vs EMA(slow) alignment + price position + EMA slope, with a
  hysteresis band to cut whipsaw and an ATR cost-gate.
    TRENDING_UP   -> LONG   (ema_fast>ema_slow, close>ema_fast, slope>0)
    TRENDING_DOWN -> SHORT  (ema_fast<ema_slow, close<ema_fast, slope<0)
    else / move<cost -> FLAT

Modes:
  python regime_arm.py --backtest   # honest two-sided backtest, sweep TF/coin
  python regime_arm.py              # forward paper step on newly-closed bars

PAPER ONLY. Honest cost on every entry+exit. Proof is the FORWARD scorecard
(n>=30, family-wise t), NOT the backtest. Pure stdlib so it runs via system
python3 on the VPS (like tsmom_paper.py / swing_paper.py).
"""
from __future__ import annotations

import json
import os
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from src.state import sanitize_for_json

# ── Config (env-overridable) ──────────────────────────────────────────────────
KRAKEN_PAIRS_ALL = {"BTC": "XBTUSD", "ETH": "ETHUSD", "SOL": "SOLUSD"}
_env = os.getenv("REGIME_SYMBOLS", "").strip()
if _env:
    _want = {s.strip().upper() for s in _env.split(",") if s.strip()}
    KRAKEN_PAIRS = {b: p for b, p in KRAKEN_PAIRS_ALL.items() if b in _want}
else:
    KRAKEN_PAIRS = dict(KRAKEN_PAIRS_ALL)

TF_MIN = int(os.getenv("REGIME_TF_MIN", "60"))          # Kraken interval: 60=1h, 240=4h
EMA_FAST = int(os.getenv("REGIME_EMA_FAST", "50"))
EMA_SLOW = int(os.getenv("REGIME_EMA_SLOW", "200"))
SLOPE_WIN = int(os.getenv("REGIME_SLOPE_WIN", "10"))
BAND = float(os.getenv("REGIME_BAND", "0.003"))          # hysteresis around ema_fast
ATR_N = 14
# Cost gate: require ATR% >= COST_MULT * round-trip cost, else the move can't
# clear the toll (the atr_alive principle that fixed the 1m bleed).
ROUND_TRIP_COST = float(os.getenv("REGIME_COST_FRAC", "0.005"))
COST_MULT = float(os.getenv("REGIME_ATR_COST_MULT", "1.5"))
ALLOW_SHORT = os.getenv("REGIME_ALLOW_SHORT", "1") == "1"
STARTING_EQUITY = float(os.getenv("REGIME_START_EQUITY", "500"))
ALLOC_FRAC = 1.0 / max(1, len(KRAKEN_PAIRS_ALL))
TRADE_SIZE = round(STARTING_EQUITY * ALLOC_FRAC, 2)
STATE_FILE = Path(os.getenv("REGIME_STATE_FILE", "data/regime_arm_state.json"))


# ── Pure-Python indicators ────────────────────────────────────────────────────
def ema(values: list[float], n: int) -> list[float | None]:
    """EMA series, None until the n-th bar (seeded with the SMA of the first n)."""
    out: list[float | None] = [None] * len(values)
    if len(values) < n:
        return out
    k = 2.0 / (n + 1)
    seed = sum(values[:n]) / n
    out[n - 1] = seed
    prev = seed
    for i in range(n, len(values)):
        prev = values[i] * k + prev * (1 - k)
        out[i] = prev
    return out


def atr_pct_series(highs, lows, closes, n=ATR_N) -> list[float | None]:
    """ATR as a fraction of close (simple mean of true range over n)."""
    tr = [None] * len(closes)
    for i in range(len(closes)):
        if i == 0:
            tr[i] = highs[i] - lows[i]
        else:
            tr[i] = max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]),
                        abs(lows[i] - closes[i - 1]))
    out: list[float | None] = [None] * len(closes)
    for i in range(len(closes)):
        if i + 1 >= n:
            a = sum(tr[i - n + 1:i + 1]) / n
            out[i] = (a / closes[i]) if closes[i] else None
    return out


def classify(i, closes, ema_f, ema_s, atrp) -> tuple[int, str]:
    """Target regime/position at bar i: +1 long, -1 short, 0 flat (+ label).
    Trend-following + cost gate; no hysteresis here (applied by the caller with
    the current position so we hold through the dead-zone)."""
    ef, es, ap = ema_f[i], ema_s[i], atrp[i]
    if ef is None or es is None or ap is None or i < SLOPE_WIN:
        return 0, "warmup"
    if ap < COST_MULT * ROUND_TRIP_COST:
        return 0, "move<cost"
    ef_past = ema_f[i - SLOPE_WIN]
    if ef_past is None:
        return 0, "warmup"
    slope = (ef - ef_past) / ef_past
    c = closes[i]
    if ef > es and c > ef and slope > 0:
        return 1, "TRENDING_UP"
    if ALLOW_SHORT and ef < es and c < ef and slope < 0:
        return -1, "TRENDING_DOWN"
    return 0, "no-trend"


def target_with_hysteresis(i, closes, ema_f, ema_s, atrp, cur: int) -> int:
    """Apply a hysteresis band around ema_fast so we don't flip on noise: only
    flip when price clears the band on the new side; otherwise hold cur."""
    want, _ = classify(i, closes, ema_f, ema_s, atrp)
    if want == cur:
        return cur
    ef = ema_f[i]
    c = closes[i]
    if ef is None:
        return cur
    # Require a clean break beyond the band to change state.
    if want == 1 and not (c > ef * (1 + BAND)):
        return cur if cur != -1 else 0   # exit short into flat, don't snap long
    if want == -1 and not (c < ef * (1 - BAND)):
        return cur if cur != 1 else 0
    return want


# ── Kraken data (stdlib) ──────────────────────────────────────────────────────
def fetch_closed(pair: str, interval: int = TF_MIN) -> list[dict]:
    url = f"https://api.kraken.com/0/public/OHLC?pair={pair}&interval={interval}"
    with urllib.request.urlopen(url, timeout=30) as r:
        data = json.loads(r.read())
    if data.get("error"):
        raise RuntimeError(f"Kraken error for {pair}: {data['error']}")
    series = next(v for k, v in data["result"].items() if k != "last")
    bars = [{"t": int(x[0]), "h": float(x[2]), "l": float(x[3]), "c": float(x[4])}
            for x in series]
    return bars[:-1]   # drop the in-progress bar (no repaint)


# ── Backtest ──────────────────────────────────────────────────────────────────
def backtest(bars: list[dict]) -> dict:
    """Two-sided regime follower. Returns metrics dict. Lookahead-free: signal at
    bar i uses data <= i; the P&L is realised on bar i+1's move."""
    closes = [b["c"] for b in bars]
    highs = [b["h"] for b in bars]
    lows = [b["l"] for b in bars]
    ema_f = ema(closes, EMA_FAST)
    ema_s = ema(closes, EMA_SLOW)
    atrp = atr_pct_series(highs, lows, closes)
    rets, pos, flips, eq = [], 0, 0, 1.0
    longs = shorts = 0
    for i in range(len(closes) - 1):
        want = target_with_hysteresis(i, closes, ema_f, ema_s, atrp, pos)
        r = 0.0
        if want != pos:
            legs = abs(want - pos)  # flat<->dir =1 leg; long<->short =2 legs
            r -= ROUND_TRIP_COST / 2 * legs   # half-cost per leg (entry or exit)
            flips += 1
            if want == 1:
                longs += 1
            elif want == -1:
                shorts += 1
        day = closes[i + 1] / closes[i] - 1
        r += want * day               # long=+day, short=-day, flat=0
        eq *= (1 + r)
        rets.append(r)
        pos = want
    return _metrics(rets, closes, longs, shorts, flips)


def _metrics(rets, closes, longs, shorts, flips) -> dict:
    import statistics as st
    from math import sqrt
    bars_per_year = 365 * 24 * 60 / TF_MIN
    if not rets:
        return {"n": 0}
    eq = peak = 1.0
    mdd = 0.0
    for r in rets:
        eq *= (1 + r); peak = max(peak, eq); mdd = min(mdd, eq / peak - 1)
    cagr = eq ** (bars_per_year / len(rets)) - 1
    sd = st.pstdev(rets)
    sharpe = (st.mean(rets) / sd * sqrt(bars_per_year)) if sd > 0 else 0.0
    # buy & hold
    bh = closes[-1] / closes[0] - 1
    wins = sum(1 for r in rets if r > 0)
    active = sum(1 for r in rets if r != 0)
    return {"n": len(rets), "total_ret": eq - 1, "cagr": cagr, "sharpe": sharpe,
            "maxdd": mdd, "bh_total": bh, "longs": longs, "shorts": shorts,
            "flips": flips, "win_rate": (wins / active * 100) if active else 0.0,
            "exposure": active / len(rets) * 100}


def run_backtest():
    print("=" * 70)
    print(f"REGIME-FOLLOWING ARM — backtest (EMA{EMA_FAST}/{EMA_SLOW}, cost {ROUND_TRIP_COST*100:.2f}% RT, "
          f"ATR gate {COST_MULT}x, shorts={'on' if ALLOW_SHORT else 'off'})")
    print("=" * 70)
    for tf in (60, 240):
        print(f"\n--- {tf//60}h bars ---")
        print(f"{'coin':<6}{'totRet':>9}{'CAGR':>8}{'Sharpe':>8}{'maxDD':>8}"
              f"{'expo%':>7}{'WR%':>6}{'L/S':>8}{'  B&H':>8}")
        global TF_MIN
        saved = TF_MIN
        TF_MIN = tf
        for base, pair in KRAKEN_PAIRS.items():
            try:
                bars = fetch_closed(pair, tf)
            except Exception as e:
                print(f"{base}: fetch failed {e}"); continue
            if len(bars) < EMA_SLOW + SLOPE_WIN + 2:
                print(f"{base}: not enough bars ({len(bars)})"); continue
            m = backtest(bars)
            ls = f"{m['longs']}/{m['shorts']}"
            print(f"{base:<6}{m['total_ret']*100:>8.0f}%{m['cagr']*100:>7.0f}%"
                  f"{m['sharpe']:>8.2f}{m['maxdd']*100:>7.0f}%{m['exposure']:>6.0f}%"
                  f"{m['win_rate']:>5.0f}%{ls:>8}{m['bh_total']*100:>7.0f}%")
        TF_MIN = saved
    print("\nNOTE: backtest is feasibility only (Kraken caps ~720 bars: 1h=30d, "
          "4h=120d). Proof = forward scorecard.")


# ── Forward paper runner ──────────────────────────────────────────────────────
def _load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"positions": {}, "closed": [], "last_bar_t": {},
            "starting_equity": STARTING_EQUITY, "equity": STARTING_EQUITY,
            "started_at": datetime.now(timezone.utc).isoformat()}


def _save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(exist_ok=True)
    tmp = STATE_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(sanitize_for_json(state), indent=2))
    tmp.replace(STATE_FILE)


def _open(state, base, side, price, ts):
    state["positions"][base] = {"symbol": base, "side": side, "entry": price,
                                "entry_ts": ts, "size_usd": TRADE_SIZE}


def _close(state, base, price, ts, reason):
    pos = state["positions"].pop(base)
    raw = (price - pos["entry"]) / pos["entry"]
    ret = raw if pos["side"] == "long" else -raw
    net = pos["size_usd"] * ret - pos["size_usd"] * ROUND_TRIP_COST
    state["equity"] = state.get("equity", STARTING_EQUITY) + net
    rec = {"symbol": base, "side": pos["side"], "entry_ts": pos["entry_ts"],
           "exit_ts": ts, "entry": pos["entry"], "exit": price,
           "size_usd": pos["size_usd"], "pnl": round(net, 4),
           "pnl_pct": round(ret * 100, 3), "reason": reason,
           "equity_after": round(state["equity"], 2)}
    state["closed"].append(rec)
    _attrib(base, pos["side"], price, net, pos["size_usd"], reason)
    return rec


def _attrib(base, side, price, net, size, reason):
    """Best-effort cross-arm ledger record (never breaks the run)."""
    try:
        sys.path.insert(0, str(Path(__file__).parent))
        from src.attribution import record
        record("regime_intraday", f"{base}/USD", side=("buy" if side == "long" else "short"),
               fill_price=price, size_usd=size, net_pnl=net, reason=reason)
    except Exception:
        pass


def process_symbol(base, bars, state) -> int:
    closes = [b["c"] for b in bars]
    highs = [b["h"] for b in bars]
    lows = [b["l"] for b in bars]
    need = EMA_SLOW + SLOPE_WIN + 2
    if len(closes) < need:
        print(f"{base}: warm-up ({len(closes)}/{need})"); return 0
    ema_f = ema(closes, EMA_FAST)
    ema_s = ema(closes, EMA_SLOW)
    atrp = atr_pct_series(highs, lows, closes)
    last_t = state["last_bar_t"].get(base)
    cur_side = state["positions"].get(base, {}).get("side")
    cur = {"long": 1, "short": -1}.get(cur_side, 0)

    if last_t is None:   # inception — seed from the latest closed bar, no backfill
        i = len(closes) - 1
        want = target_with_hysteresis(i, closes, ema_f, ema_s, atrp, 0)
        state["last_bar_t"][base] = bars[-1]["t"]
        if want == 1:
            _open(state, base, "long", closes[i], str(bars[-1]["t"]))
            print(f"{base}: SEED LONG @ {closes[i]:.2f}")
        elif want == -1:
            _open(state, base, "short", closes[i], str(bars[-1]["t"]))
            print(f"{base}: SEED SHORT @ {closes[i]:.2f}")
        else:
            print(f"{base}: SEED FLAT")
        return 0

    acted = 0
    for bi, bar in enumerate(bars):
        if bar["t"] <= last_t:
            continue
        want = target_with_hysteresis(bi, closes, ema_f, ema_s, atrp, cur)
        if want != cur:
            if cur != 0:
                rec = _close(state, base, bar["c"], str(bar["t"]),
                             "regime_flip" if want != 0 else "regime_exit")
                print(f"{base}: CLOSE {rec['side']} @ {bar['c']:.2f} net=${rec['pnl']:+.2f}")
                acted += 1
            if want != 0:
                _open(state, base, "long" if want == 1 else "short", bar["c"], str(bar["t"]))
                print(f"{base}: OPEN {'LONG' if want==1 else 'SHORT'} @ {bar['c']:.2f}")
                acted += 1
            cur = want
        state["last_bar_t"][base] = bar["t"]
    return acted


def run_forward():
    state = _load_state()
    total = 0
    for base, pair in KRAKEN_PAIRS.items():
        try:
            bars = fetch_closed(pair, TF_MIN)
        except Exception as e:
            print(f"{base}: fetch failed - {e}"); continue
        total += process_symbol(base, bars, state)
    _save_state(state)
    eq = state.get("equity", STARTING_EQUITY)
    start = state.get("starting_equity", STARTING_EQUITY)
    opens = {b: p["side"] for b, p in state["positions"].items()}
    print(f"[regime_arm] {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC "
          f"tf={TF_MIN}m equity=${eq:.2f} (start ${start:.0f}, {eq-start:+.2f}) "
          f"acted={total} open={opens} closed={len(state['closed'])}")


def main():
    if "--backtest" in sys.argv:
        run_backtest()
    else:
        run_forward()


if __name__ == "__main__":
    main()

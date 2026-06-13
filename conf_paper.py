#!/usr/bin/env python3
"""
Confluence trend ALLOCATION — FORWARD paper runner (single-shot, cron/loop-friendly).

The long-only tournament (scripts/longonly_tournament.py, BTC+ETH+SOL daily, honest
0.5% cost) found that a TREND + MOMENTUM confluence — long only when price is BOTH
above its SMA100 AND its 20-day momentum is positive — was the best DRAWDOWN-adjusted
spot-executable bot (avg $1138, Sharpe 0.40, -31% maxDD vs buy & hold's -65%). It is
the conjunction of two conditions, so neither the single-SMA tsmom arm nor the SMA50
fast arm expresses it. That backtest is a CANDIDATE, not proof — this runner earns the
proof on the forward clock (proof_scorecard.py reads data/conf_paper_state.json,
_conf_forward()).

PRE-SPECIFIED SPEC (one, not swept — the lookbacks come from the tournament, not tuned
here; sweeping them now would be the overfit we avoid):
  * Universe: BTC, ETH, SOL (liquid trenders; matches the tsmom arm. The 6-coin
    tournament showed trend beats B&H on 5/6 coins but never all — so a focused
    major-trender book, not per-coin chasing).
  * Signal: long when close > SMA(100) AND close > close[-20] (20d momentum up);
    CASH otherwise. The confluence itself is the whipsaw filter (both must agree),
    so no extra hysteresis band is added.
  * Equal weight, fixed fraction of STARTING equity (scale-invariant → clean t-stat).
    Round-trip cost charged on each close.

FORWARD-ONLY: first run per symbol seeds the CURRENT position at TODAY's price/ts (real
participation from inception — no backfilling a mature trend) and takes no historical
trades. Acts only on newly-CLOSED daily bars.

    python conf_paper.py        # process any newly-closed daily bar, then exit
"""
from __future__ import annotations

import json
import os
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

KRAKEN_PAIRS_ALL = {"BTC": "XBTUSD", "ETH": "ETHUSD", "SOL": "SOLUSD"}
_env = os.getenv("CONF_SYMBOLS", "").strip()
if _env:
    _want = {s.strip().upper() for s in _env.split(",") if s.strip()}
    KRAKEN_PAIRS = {b: p for b, p in KRAKEN_PAIRS_ALL.items() if b in _want}
else:
    KRAKEN_PAIRS = dict(KRAKEN_PAIRS_ALL)

SMA_N = int(os.getenv("CONF_SMA", "100"))               # trend leg
MOMO_N = int(os.getenv("CONF_MOMO", "20"))              # momentum leg
COST_FRAC = float(os.getenv("CONF_COST_FRAC", "0.0054"))
STARTING_EQUITY = float(os.getenv("CONF_START_EQUITY", "1000"))
ALLOC_FRAC = 1.0 / max(1, len(KRAKEN_PAIRS_ALL))        # equal weight across the universe
TRADE_SIZE = round(STARTING_EQUITY * ALLOC_FRAC, 2)
STATE_FILE = Path(os.getenv("CONF_STATE_FILE", "data/conf_paper_state.json"))
INTERVAL_DAILY = 1440
WARMUP = max(SMA_N, MOMO_N) + 1


def fetch_closed_daily(pair: str) -> list[dict]:
    """Ascending daily closes with the in-progress bar dropped (no repaint)."""
    url = f"https://api.kraken.com/0/public/OHLC?pair={pair}&interval={INTERVAL_DAILY}"
    with urllib.request.urlopen(url, timeout=30) as r:
        data = json.loads(r.read())
    if data.get("error"):
        raise RuntimeError(f"Kraken error for {pair}: {data['error']}")
    series = next(v for k, v in data["result"].items() if k != "last")
    bars = [{"t": int(row[0]), "c": float(row[4])} for row in series]
    return bars[:-1]


def _sma(closes: list[float], n: int) -> float | None:
    return sum(closes[-n:]) / n if len(closes) >= n else None


def _want_long(closes: list[float]) -> bool | None:
    """Confluence: above SMA100 AND 20-day momentum positive. None during warm-up.
    No hysteresis — the two-condition agreement IS the whipsaw filter."""
    if len(closes) < WARMUP:
        return None
    sma = _sma(closes, SMA_N)
    return (closes[-1] > sma) and (closes[-1] > closes[-1 - MOMO_N])


def _load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"positions": {}, "closed": [], "last_bar_t": {},
            "starting_equity": STARTING_EQUITY, "equity": STARTING_EQUITY,
            "started_at": datetime.now(timezone.utc).isoformat()}


def _save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(exist_ok=True)
    tmp = STATE_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2))
    tmp.replace(STATE_FILE)


def _open(state: dict, base: str, price: float, ts: str) -> None:
    state["positions"][base] = {"symbol": base, "entry": price, "entry_ts": ts,
                                "size_usd": TRADE_SIZE}


def _close(state: dict, base: str, price: float, ts: str, reason: str) -> dict:
    pos = state["positions"].pop(base)
    ret = (price - pos["entry"]) / pos["entry"]
    net = pos["size_usd"] * ret - pos["size_usd"] * COST_FRAC
    state["equity"] = state.get("equity", STARTING_EQUITY) + net
    rec = {"symbol": base, "entry_ts": pos["entry_ts"], "exit_ts": ts,
           "entry": pos["entry"], "exit": price, "size_usd": pos["size_usd"],
           "pnl": round(net, 4), "pnl_pct": round(ret * 100, 3), "reason": reason,
           "equity_after": round(state["equity"], 2)}
    state["closed"].append(rec)
    return rec


def process_symbol(base: str, bars: list[dict], state: dict) -> int:
    """Advance the allocation for one symbol on newly-closed daily bars. First run
    seeds the current position at today's price (forward-only). Returns # acted."""
    closes = [b["c"] for b in bars]
    if len(closes) < WARMUP:
        print(f"{base}: warm-up ({len(closes)}/{WARMUP} daily bars)")
        return 0
    last_t = state["last_bar_t"].get(base)
    latest = bars[-1]

    if last_t is None:                              # baseline / inception
        state["last_bar_t"][base] = latest["t"]
        if _want_long(closes):                      # participate from inception if confluence on
            _open(state, base, latest["c"], str(latest["t"]))
            print(f"{base}: SEED LONG @ {latest['c']:.2f} (>SMA{SMA_N} & {MOMO_N}d momo up)")
        else:
            print(f"{base}: SEED CASH (confluence off)")
        return 0

    new = [b for b in bars if b["t"] > last_t]
    acted = 0
    for bar in new:
        idx = bars.index(bar)
        sub = closes[: idx + 1]
        want = _want_long(sub)
        if want is None:
            continue
        is_long = base in state["positions"]
        if want and not is_long:
            _open(state, base, bar["c"], str(bar["t"]))
            print(f"{base}: OPEN LONG @ {bar['c']:.2f} (>SMA{SMA_N} & {MOMO_N}d momo up)")
            acted += 1
        elif is_long and not want:
            rec = _close(state, base, bar["c"], str(bar["t"]), "confluence_off")
            print(f"{base}: CLOSE @ {bar['c']:.2f} net=${rec['pnl']:+.2f} "
                  f"({rec['pnl_pct']:+.1f}%)")
            acted += 1
        state["last_bar_t"][base] = bar["t"]
    return acted


def main():
    state = _load_state()
    total = 0
    for base, pair in KRAKEN_PAIRS.items():
        try:
            bars = fetch_closed_daily(pair)
        except Exception as e:
            print(f"{base}: fetch failed - {e}")
            continue
        total += process_symbol(base, bars, state)
    _save_state(state)
    eq = state.get("equity", STARTING_EQUITY)
    start = state.get("starting_equity", STARTING_EQUITY)
    longs = list(state["positions"])
    print(f"[conf_paper] {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC  "
          f"equity=${eq:.2f} (start ${start:.0f}, {eq-start:+.2f}) "
          f"size=${TRADE_SIZE:.0f} universe={list(KRAKEN_PAIRS)} "
          f"acted={total} long={longs} closed={len(state['closed'])}")


if __name__ == "__main__":
    main()

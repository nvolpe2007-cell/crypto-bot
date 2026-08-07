#!/usr/bin/env python3
"""
Trend-following ALLOCATION — FORWARD paper runner (single-shot, cron-friendly).

The directional swing strategies died on the 0.54% cost wall (frequent small
trades). This is the opposite shape: a low-turnover long/cash allocation that
rides multi-month trends, where cost is a footnote (~6-10 switches/yr). Its
backtest (tsmom_test.py, 4.9y) halved drawdown vs buy-and-hold on BTC/ETH/SOL and
survived honest cost. That is a CANDIDATE, not proof — this runner earns the proof
on the forward clock (proof_scorecard.py reads data/tsmom_paper_state.json).

PRE-SPECIFIED SPEC (one, not swept — sweeping is the overfit we avoid):
  * Universe: BTC, ETH, SOL (liquid trenders; TSMOM is meant to work here and to
    fail on choppy alts, which it did — LTC/BCH/XRP excluded on PRINCIPLE).
  * Signal: daily close vs SMA(200), with a 2% HYSTERESIS BAND to cut whipsaw —
    go LONG when close > SMA*(1+band); go CASH when close < SMA*(1-band); else
    hold. (The band is the honest turnover fix, not return-tuning.)
  * Equal weight, fixed fraction of STARTING equity (scale-invariant → clean
    t-stat). Round-trip cost charged on each close.

FORWARD-ONLY: first run per symbol seeds the CURRENT position at TODAY's price/ts
(real participation from inception — no backfilling a mature trend into the ledger)
and takes no historical trades. Acts only on newly-CLOSED daily bars.

    python tsmom_paper.py        # process any newly-closed daily bar, then exit
"""
from __future__ import annotations

import json
import os
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from src.state import sanitize_for_json

# Master kill switch (best-effort import — never block trading if it can't load).
try:
    from src.kill_switch import is_killed as _is_killed
except Exception:  # pragma: no cover - import-path safety net
    def _is_killed() -> bool:
        return False

KRAKEN_PAIRS_ALL = {"BTC": "XBTUSD", "ETH": "ETHUSD", "SOL": "SOLUSD"}
_env = os.getenv("TSMOM_SYMBOLS", "").strip()
if _env:
    _want = {s.strip().upper() for s in _env.split(",") if s.strip()}
    KRAKEN_PAIRS = {b: p for b, p in KRAKEN_PAIRS_ALL.items() if b in _want}
else:
    KRAKEN_PAIRS = dict(KRAKEN_PAIRS_ALL)

SMA_N = int(os.getenv("TSMOM_SMA", "200"))
BAND = float(os.getenv("TSMOM_BAND", "0.02"))          # 2% hysteresis to cut whipsaw
COST_FRAC = float(os.getenv("TSMOM_COST_FRAC", "0.0054"))
STARTING_EQUITY = float(os.getenv("TSMOM_START_EQUITY", "500"))
ALLOC_FRAC = 1.0 / max(1, len(KRAKEN_PAIRS_ALL))        # equal weight across the universe
TRADE_SIZE = round(STARTING_EQUITY * ALLOC_FRAC, 2)
STATE_FILE = Path(os.getenv("TSMOM_STATE_FILE", "data/tsmom_paper_state.json"))
INTERVAL_DAILY = 1440


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


def _target_position(close: float, sma: float, current_long: bool) -> bool:
    """Hysteresis: enter long above the upper band, exit below the lower band,
    hold through the dead-zone between them (this is what suppresses whipsaw)."""
    if current_long:
        return not (close < sma * (1 - BAND))      # stay long unless we break down
    return close > sma * (1 + BAND)                 # go long only on a clean break up


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
    if len(closes) < SMA_N + 1:
        print(f"{base}: warm-up ({len(closes)}/{SMA_N + 1} daily bars)")
        return 0
    last_t = state["last_bar_t"].get(base)
    latest = bars[-1]
    sma = _sma(closes, SMA_N)

    if last_t is None:                              # baseline / inception
        state["last_bar_t"][base] = latest["t"]
        if latest["c"] > sma and not _is_killed():   # participate from inception if in uptrend
            _open(state, base, latest["c"], str(latest["t"]))
            print(f"{base}: SEED LONG @ {latest['c']:.2f} (close>{SMA_N}SMA {sma:.2f})")
        else:
            print(f"{base}: SEED CASH (close<{SMA_N}SMA {sma:.2f})")
        return 0

    new = [b for b in bars if b["t"] > last_t]
    acted = 0
    for bar in new:
        idx = bars.index(bar)
        sub = closes[: idx + 1]
        s = _sma(sub, SMA_N)
        if s is None:
            continue
        is_long = base in state["positions"]
        want_long = _target_position(bar["c"], s, is_long)
        if want_long and not is_long:
            if _is_killed():
                print(f"{base}: SKIP entry (kill switch engaged)")
            else:
                _open(state, base, bar["c"], str(bar["t"]))
                print(f"{base}: OPEN LONG @ {bar['c']:.2f} (>{SMA_N}SMA+band)")
                acted += 1
        elif is_long and not want_long:
            rec = _close(state, base, bar["c"], str(bar["t"]), "trend_exit")
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
    print(f"[tsmom_paper] {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC  "
          f"equity=${eq:.2f} (start ${start:.0f}, {eq-start:+.2f}) "
          f"size=${TRADE_SIZE:.0f} universe={list(KRAKEN_PAIRS)} "
          f"acted={total} long={longs} closed={len(state['closed'])}")


if __name__ == "__main__":
    main()

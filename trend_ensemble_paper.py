#!/usr/bin/env python3
"""
BTC TREND ENSEMBLE — 2-of-3 vote allocation, FORWARD paper runner.

Sibling of btc_trend_paper.py (same shape: long-only BTC spot, whole book,
binary in-or-out, Kraken-executable with real money someday). Different — and
deliberately SLOWER — signal: a 2-of-3 vote among three standard trend legs
instead of btc_trend's SMA100 AND 20d-momo conjunction. The ensemble's
diversification across lookbacks IS the whipsaw filter; no single magic
parameter to overfit.

PRE-SPECIFIED SPEC (2026-07-02, from the owner's indicator research — written
down BEFORE this arm's first forward trade; see
RESEARCH_2026-07-01_btc_patterns_and_leverage.md for the session lineage):
  * Universe: BTC only. The whole $1,000 paper book.
  * Signal: LONG when >= 2 of 3 votes are true at the daily close:
      1. close > SMA(100)
      2. close > SMA(200)
      3. close > close[-90]  (90d momentum)
    CASH otherwise.
  * Round-trip cost (0.54% Kraken spot) charged on each close, like btc_trend.
  * 5-yr backtest (Coinbase daily, 2021→2026, 0.3% RT cost): +141% vs BTC B&H
    +33%, maxDD -27% vs -67%, ~44 flips. SMA200-alone tested better (+187%)
    but was the best cell of a sweep — the ensemble is the robust choice.
    A CVD/OFI flow gate tested NEUTRAL on BTC (-6pts return, -2pts DD) and is
    deliberately omitted here; it only earned its keep on ETH/SOL.

HONEST CAVEATS: one backtest window (bull-heavy for the last 2 years); at
~8-10 flips/yr the n>=30 proof bar is a ~3-4 year clock, so for a long time
the verdict is qualitative (tracks upside? clips drawdowns?), not a t-test.
Judged by proof_scorecard (_trend_ensemble_forward) on the same pre-registered
family-wise bar as every other arm.

FORWARD-ONLY: first run seeds the CURRENT position at TODAY's price/ts and
books no history. Acts only on newly-CLOSED daily bars (no repaint).

    python trend_ensemble_paper.py   # process any newly-closed daily bar, then exit
"""
from __future__ import annotations

import json
import os
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

KRAKEN_PAIR = os.getenv("TREND_ENS_PAIR", "XBTUSD")
SMA_FAST = int(os.getenv("TREND_ENS_SMA_FAST", "100"))
SMA_SLOW = int(os.getenv("TREND_ENS_SMA_SLOW", "200"))
MOM_N = int(os.getenv("TREND_ENS_MOM", "90"))
VOTES_NEED = int(os.getenv("TREND_ENS_VOTES", "2"))
COST_FRAC = float(os.getenv("TREND_ENS_COST_FRAC", "0.0054"))
STARTING_EQUITY = float(os.getenv("TREND_ENS_START_EQUITY", "1000"))
TRADE_SIZE = float(os.getenv("TREND_ENS_SIZE", str(STARTING_EQUITY)))
STATE_FILE = Path(os.getenv("TREND_ENS_STATE_FILE", "data/trend_ensemble_state.json"))
INTERVAL_DAILY = 1440
WARMUP = max(SMA_FAST, SMA_SLOW, MOM_N) + 1


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


def _votes(closes: list[float]) -> tuple[int, list[str]] | None:
    """(vote count, which legs are on) — or None during warm-up."""
    if len(closes) < WARMUP:
        return None
    c = closes[-1]
    legs = [
        ("sma%d" % SMA_FAST, c > _sma(closes, SMA_FAST)),
        ("sma%d" % SMA_SLOW, c > _sma(closes, SMA_SLOW)),
        ("mom%d" % MOM_N, c > closes[-1 - MOM_N]),
    ]
    on = [name for name, ok in legs if ok]
    return len(on), on


def _want_long(closes: list[float]) -> bool | None:
    v = _votes(closes)
    return None if v is None else v[0] >= VOTES_NEED


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
    """Advance the allocation on newly-closed daily bars. Forward-only seed."""
    closes = [b["c"] for b in bars]
    if len(closes) < WARMUP:
        print(f"{base}: warm-up ({len(closes)}/{WARMUP} daily bars)")
        return 0
    last_t = state["last_bar_t"].get(base)
    latest = bars[-1]

    if last_t is None:
        state["last_bar_t"][base] = latest["t"]
        v = _votes(closes)
        if v and v[0] >= VOTES_NEED:
            _open(state, base, latest["c"], str(latest["t"]))
            print(f"{base}: SEED LONG @ {latest['c']:.2f} (votes {v[0]}/3: {','.join(v[1])})")
        else:
            print(f"{base}: SEED CASH (votes {v[0] if v else '?'}/3)")
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
            v = _votes(sub)
            _open(state, base, bar["c"], str(bar["t"]))
            print(f"{base}: OPEN LONG @ {bar['c']:.2f} (votes {v[0]}/3: {','.join(v[1])})")
            acted += 1
        elif is_long and not want:
            rec = _close(state, base, bar["c"], str(bar["t"]), "votes_off")
            print(f"{base}: CLOSE @ {bar['c']:.2f} net=${rec['pnl']:+.2f} "
                  f"({rec['pnl_pct']:+.1f}%)")
            acted += 1
        state["last_bar_t"][base] = bar["t"]
    return acted


def main():
    state = _load_state()
    try:
        bars = fetch_closed_daily(KRAKEN_PAIR)
    except Exception as e:
        print(f"BTC: fetch failed - {e}")
        return
    acted = process_symbol("BTC", bars, state)
    _save_state(state)
    eq = state.get("equity", STARTING_EQUITY)
    start = state.get("starting_equity", STARTING_EQUITY)
    longs = list(state["positions"])
    print(f"[trend_ensemble] {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC  "
          f"equity=${eq:.2f} (start ${start:.0f}, {eq-start:+.2f}) "
          f"votes_need={VOTES_NEED}/3 size=${TRADE_SIZE:.0f} acted={acted} "
          f"long={longs} closed={len(state['closed'])}")


if __name__ == "__main__":
    main()

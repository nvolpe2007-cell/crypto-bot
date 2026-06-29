#!/usr/bin/env python3
"""
BTC trend with CONVICTION-SCALED FRACTIONAL-KELLY COMPOUNDING — FORWARD paper arm.

The owner asked: as it wins, put more on; bet bigger when confident; compound. This
arm does exactly that, HONESTLY. It rides the *same* signal as btc_trend_paper.py
(>SMA100 AND 20d momentum up; long/cash) — so the ONLY difference from that flat
control arm is the SIZING. The proof scorecard judges them head-to-head: does
conviction-scaled compounding actually beat flat 100%-of-equity, or just add variance?

What the Monte-Carlo (scripts, last session) established, baked in here:
  • COMPOUNDING is the real exponential engine: every trade is sized off CURRENT
    equity, not the starting $500 — so wins grow the next bet (and losses shrink it).
  • Bet size scales with CONVICTION (entry trend strength), within a fraction band.
  • HARD NO-LEVERAGE CAP. fraction is clamped to <= MAX_FRAC (default 1.0 = fully
    invested, never borrowed). Leverage is where the account dies: the sim showed any
    perp leverage with a liquidation tail ruins ~57% of timelines. MAX_FRAC>1 is
    allowed only as an explicit research knob — it re-opens the ruin zone.
  • Sizing AMPLIFIES an edge, it cannot create one. On a positive-EV signal (this one
    backtests +2%/trade) fractional-Kelly compounds it; on a no-edge signal any sizing
    just loses faster. This arm only sizes up a signal that already has an edge.

CONVICTION (pre-specified, NOT swept — sweeping it is the overfit we avoid): the entry
20-day momentum, normalised to a fixed reference (CONV_REF=20% over 20d = full
conviction), clamped 0..1. Stronger thrust at entry → bigger fraction. Mechanical and
transparent on purpose — no API, deterministic, backtestable. (The discretionary brain
arm sizes by its OWN conviction; this is the rules-based control for that idea.)

  fraction f = MIN_FRAC + conviction * (MAX_FRAC - MIN_FRAC),  notional = f * equity

FORWARD-ONLY: first run seeds the current position at today's price (no backfill) and
takes no historical trades. Acts only on newly-CLOSED daily bars.

    python kelly_trend_paper.py        # process any newly-closed daily bar, then exit
"""
from __future__ import annotations

import json
import os
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from src.state import sanitize_for_json

KRAKEN_PAIR = os.getenv("KELLY_TREND_PAIR", "XBTUSD")
SMA_N = int(os.getenv("KELLY_TREND_SMA", "100"))
MOMO_N = int(os.getenv("KELLY_TREND_MOMO", "20"))
COST_FRAC = float(os.getenv("KELLY_TREND_COST_FRAC", "0.0054"))
STARTING_EQUITY = float(os.getenv("KELLY_TREND_START_EQUITY", "1000"))
# Conviction → fraction band. MAX_FRAC caps leverage: 1.0 = fully invested, never
# borrowed (the ruin-safe ceiling). >1.0 re-opens the leverage ruin zone — research only.
CONV_REF = float(os.getenv("KELLY_TREND_CONV_REF", "0.20"))     # 20% over 20d = full conviction
MIN_FRAC = float(os.getenv("KELLY_TREND_MIN_FRAC", "0.25"))     # weak trend still participates
MAX_FRAC = float(os.getenv("KELLY_TREND_MAX_FRAC", "1.00"))     # NO leverage by default
STATE_FILE = Path(os.getenv("KELLY_TREND_STATE_FILE", "data/kelly_trend_state.json"))
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
    """Confluence: above SMA100 AND 20-day momentum positive. None during warm-up."""
    if len(closes) < WARMUP:
        return None
    sma = _sma(closes, SMA_N)
    return (closes[-1] > sma) and (closes[-1] > closes[-1 - MOMO_N])


def _conviction(closes: list[float]) -> float:
    """Entry conviction in [0,1] from 20d momentum vs a fixed reference. Pre-specified."""
    if len(closes) < MOMO_N + 1:
        return 0.0
    momo = closes[-1] / closes[-1 - MOMO_N] - 1.0
    return max(0.0, min(1.0, momo / CONV_REF)) if CONV_REF > 0 else 1.0


def _fraction(conviction: float) -> float:
    """Conviction-scaled fraction of equity to deploy, clamped to the no-leverage band."""
    f = MIN_FRAC + conviction * (MAX_FRAC - MIN_FRAC)
    return max(0.0, min(MAX_FRAC, f))


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


def _open(state: dict, base: str, price: float, ts: str, closes: list[float]) -> None:
    """Open a long sized at fraction(conviction) of CURRENT equity — this is the
    compounding step: notional grows with the book as it wins."""
    conv = _conviction(closes)
    frac = _fraction(conv)
    notional = round(frac * state.get("equity", STARTING_EQUITY), 2)
    state["positions"][base] = {"symbol": base, "entry": price, "entry_ts": ts,
                                "size_usd": notional, "fraction": round(frac, 3),
                                "conviction": round(conv, 3)}


def _close(state: dict, base: str, price: float, ts: str, reason: str) -> dict:
    pos = state["positions"].pop(base)
    ret = (price - pos["entry"]) / pos["entry"]
    net = pos["size_usd"] * ret - pos["size_usd"] * COST_FRAC
    state["equity"] = state.get("equity", STARTING_EQUITY) + net
    rec = {"symbol": base, "entry_ts": pos["entry_ts"], "exit_ts": ts,
           "entry": pos["entry"], "exit": price, "size_usd": pos["size_usd"],
           "fraction": pos.get("fraction"), "conviction": pos.get("conviction"),
           "pnl": round(net, 4), "pnl_pct": round(ret * 100, 3), "reason": reason,
           "equity_after": round(state["equity"], 2)}
    state["closed"].append(rec)
    return rec


def process_symbol(base: str, bars: list[dict], state: dict) -> int:
    """Advance the allocation on newly-closed daily bars. First run seeds the current
    position at today's price (forward-only). Returns # acted."""
    closes = [b["c"] for b in bars]
    if len(closes) < WARMUP:
        print(f"{base}: warm-up ({len(closes)}/{WARMUP} daily bars)")
        return 0
    last_t = state["last_bar_t"].get(base)
    latest = bars[-1]

    if last_t is None:                              # baseline / inception
        state["last_bar_t"][base] = latest["t"]
        if _want_long(closes):
            _open(state, base, latest["c"], str(latest["t"]), closes)
            pos = state["positions"][base]
            print(f"{base}: SEED LONG @ {latest['c']:.2f} conv={pos['conviction']} "
                  f"frac={pos['fraction']} notional=${pos['size_usd']:.0f}")
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
            _open(state, base, bar["c"], str(bar["t"]), sub)
            pos = state["positions"][base]
            print(f"{base}: OPEN LONG @ {bar['c']:.2f} conv={pos['conviction']} "
                  f"frac={pos['fraction']} notional=${pos['size_usd']:.0f}")
            acted += 1
        elif is_long and not want:
            rec = _close(state, base, bar["c"], str(bar["t"]), "confluence_off")
            print(f"{base}: CLOSE @ {bar['c']:.2f} net=${rec['pnl']:+.2f} "
                  f"({rec['pnl_pct']:+.1f}%) equity=${rec['equity_after']:.0f}")
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
    pos = state["positions"].get("BTC")
    held = (f"long ${pos['size_usd']:.0f} (frac {pos['fraction']})" if pos else "cash")
    print(f"[kelly_trend_paper] {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC  "
          f"equity=${eq:.2f} (start ${start:.0f}, {eq-start:+.2f}) "
          f"held={held} acted={acted} closed={len(state['closed'])}")


if __name__ == "__main__":
    main()

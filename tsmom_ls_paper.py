#!/usr/bin/env python3
"""
Trend LONG/SHORT perp ALLOCATION — FORWARD paper runner (single-shot, loop-friendly).

This is the PAPER perp arm for the short side Kraken's US perps will unlock. The
long-only arms (tsmom_paper / conf_paper) can only go to CASH in a downtrend; this
one goes SHORT — the exact strategy scripts/short_leg_value.py measured (tsmom_50,
1x notional). That analysis found the short leg adds value on the blended book but
is ETH-carried and fragile, surviving funding up to ~25% APY — so this forward arm
exists to PROVE OR KILL the short side on the live clock before any real perp money.

PRE-SPECIFIED SPEC (from short_leg_value.py — not swept here):
  * Universe: BTC, ETH, SOL (the liquid trenders; same as the long-only arms).
  * Signal: daily close vs SMA(50). pos = +1 (LONG) above, -1 (SHORT) below. Always
    in the market once warm (this is the long/short decomposition the analysis used);
    optional hysteresis band (default 0 = faithful to the analysis).
  * 1x notional, equal weight, fixed fraction of STARTING equity. No leverage —
    leverage ruin is settled (memory doubling_in_a_month_verdict).
  * Costs: perp taker round-trip on each flip, PLUS a conservative FUNDING DRAG
    (default 10% APY) charged on notional for the time held — charged as a pure cost
    on BOTH sides (a directional perp doesn't always PAY funding, so this is
    deliberately pessimistic). Swap in the live funding feed (arbitrage/
    funding_history.py) later for the real per-cycle rate; until then a conservative
    fixed drag keeps the proof honest and the runner self-contained.

FORWARD-ONLY: first run per symbol seeds the CURRENT position (long or short) at
TODAY's price/ts and books no history. Acts only on newly-CLOSED daily bars.

    python tsmom_ls_paper.py        # process any newly-closed daily bar, then exit
"""
from __future__ import annotations

import json
import os
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

KRAKEN_PAIRS_ALL = {"BTC": "XBTUSD", "ETH": "ETHUSD", "SOL": "SOLUSD"}
_env = os.getenv("TSMOM_LS_SYMBOLS", "").strip()
if _env:
    _want = {s.strip().upper() for s in _env.split(",") if s.strip()}
    KRAKEN_PAIRS = {b: p for b, p in KRAKEN_PAIRS_ALL.items() if b in _want}
else:
    KRAKEN_PAIRS = dict(KRAKEN_PAIRS_ALL)

SMA_N = int(os.getenv("TSMOM_LS_SMA", "50"))
BAND = float(os.getenv("TSMOM_LS_BAND", "0.0"))            # 0 = faithful to short_leg_value
TRADE_COST_FRAC = float(os.getenv("TSMOM_LS_COST_FRAC", "0.001"))   # perp taker round-trip ~0.10%
FUNDING_APY = float(os.getenv("TSMOM_LS_FUNDING_APY", "0.10"))      # conservative funding drag
STARTING_EQUITY = float(os.getenv("TSMOM_LS_START_EQUITY", "1000"))
ALLOC_FRAC = 1.0 / max(1, len(KRAKEN_PAIRS_ALL))          # equal weight across the universe
TRADE_SIZE = round(STARTING_EQUITY * ALLOC_FRAC, 2)
STATE_FILE = Path(os.getenv("TSMOM_LS_STATE_FILE", "data/tsmom_ls_state.json"))
INTERVAL_DAILY = 1440
HOURS_PER_YEAR = 24.0 * 365.0


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


def _target_side(close: float, sma: float, current: int) -> int:
    """+1 long above SMA, -1 short below, with optional hysteresis dead-zone that
    holds the current side (BAND=0 -> pure sign of close-SMA, the analysis spec)."""
    if close > sma * (1 + BAND):
        return 1
    if close < sma * (1 - BAND):
        return -1
    return current if current != 0 else (1 if close > sma else -1)


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


def _open(state: dict, base: str, side: int, price: float, ts: str) -> None:
    state["positions"][base] = {"symbol": base, "side": side, "entry": price,
                                "entry_ts": ts, "size_usd": TRADE_SIZE}


def _funding_cost(size: float, entry_ts: str, exit_ts: str) -> float:
    """Conservative funding drag on notional for the holding period (always a cost)."""
    try:
        hours = (int(exit_ts) - int(entry_ts)) / 3600.0
    except (TypeError, ValueError):
        return 0.0
    return size * FUNDING_APY * max(0.0, hours) / HOURS_PER_YEAR


def _close(state: dict, base: str, price: float, ts: str, reason: str) -> dict:
    pos = state["positions"].pop(base)
    side = pos["side"]
    ret = side * (price - pos["entry"]) / pos["entry"]      # short profits when price falls
    funding = _funding_cost(pos["size_usd"], pos["entry_ts"], ts)
    net = pos["size_usd"] * ret - pos["size_usd"] * TRADE_COST_FRAC - funding
    state["equity"] = state.get("equity", STARTING_EQUITY) + net
    rec = {"symbol": base, "side": side, "entry_ts": pos["entry_ts"], "exit_ts": ts,
           "entry": pos["entry"], "exit": price, "size_usd": pos["size_usd"],
           "funding_cost": round(funding, 4), "pnl": round(net, 4),
           "pnl_pct": round(ret * 100, 3), "reason": reason,
           "equity_after": round(state["equity"], 2)}
    state["closed"].append(rec)
    return rec


def process_symbol(base: str, bars: list[dict], state: dict) -> int:
    """Advance the L/S allocation for one symbol on newly-closed daily bars. First
    run seeds the current side at today's price (forward-only). Returns # acted."""
    closes = [b["c"] for b in bars]
    if len(closes) < SMA_N + 1:
        print(f"{base}: warm-up ({len(closes)}/{SMA_N + 1} daily bars)")
        return 0
    last_t = state["last_bar_t"].get(base)
    latest = bars[-1]
    sma = _sma(closes, SMA_N)

    if last_t is None:                              # baseline / inception
        state["last_bar_t"][base] = latest["t"]
        side = _target_side(latest["c"], sma, 0)
        _open(state, base, side, latest["c"], str(latest["t"]))
        print(f"{base}: SEED {'LONG' if side > 0 else 'SHORT'} @ {latest['c']:.2f} "
              f"({SMA_N}SMA {sma:.2f})")
        return 0

    new = [b for b in bars if b["t"] > last_t]
    acted = 0
    for bar in new:
        idx = bars.index(bar)
        s = _sma(closes[: idx + 1], SMA_N)
        if s is None:
            continue
        cur = state["positions"].get(base, {}).get("side", 0)
        want = _target_side(bar["c"], s, cur)
        if want != cur:
            if cur != 0:                            # flip: realize the old side first
                rec = _close(state, base, bar["c"], str(bar["t"]), "flip")
                print(f"{base}: CLOSE {'LONG' if cur > 0 else 'SHORT'} @ {bar['c']:.2f} "
                      f"net=${rec['pnl']:+.2f} (fund ${rec['funding_cost']:.2f})")
            _open(state, base, want, bar["c"], str(bar["t"]))
            print(f"{base}: OPEN {'LONG' if want > 0 else 'SHORT'} @ {bar['c']:.2f}")
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
    held = {b: ("L" if p["side"] > 0 else "S") for b, p in state["positions"].items()}
    print(f"[tsmom_ls_paper] {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC  "
          f"equity=${eq:.2f} (start ${start:.0f}, {eq-start:+.2f}) "
          f"size=${TRADE_SIZE:.0f} universe={list(KRAKEN_PAIRS)} "
          f"acted={total} held={held} closed={len(state['closed'])}")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
LEVERAGED perp paper arm V2 — same entries as lev_perp_paper, TRAILED exits.

A/B EXPERIMENT (pre-specified 2026-07-02): lev_perp_paper's fixed +5% take-profit
caps every winner while the trend is still running (its first 8 wins were ALL
capped at the TP), and its fixed 5% stop is tight when BTC does 2%/day and loose
when vol is 0.8%/day. V2 changes ONLY the exit engine and lets proof_scorecard
judge the two head-to-head on the forward clock:

  * ENTRY: identical to lev_perp_paper — daily close vs SMA(50) direction, the
    same four entry filters (RSI/trend-age/volume/ADX dead-zone), the same
    vol-targeted leverage, correlation-capped margin, and news-halt gate. Shared
    helpers are IMPORTED from lev_perp_paper (one source of truth; its tests
    cover them).
  * EXIT: ATR CHANDELIER instead of fixed TP/SL. Stop sits ATR_MULT x ATR(14,
    daily) beyond the most favorable price seen since entry and RATCHETS only in
    the trade's favor. One mechanism is both the initial stop (from entry) and
    the profit trail (from the peak) — winners run until the trend actually
    bends, losers cut at ~2 ATR. src/trailing_stop.py was considered and not
    reused: it manages intraday PaperPosition objects with tier/hold semantics;
    this arm is a one-shot daily-bar script with a dict state file.
  * Conservative daily-bar ordering: each new bar is checked against the stop
    computed from PRIOR bars only (liquidation > trail stop), THEN the peak/stop
    ratchet updates from the bar's extremes. A bar can't arm its own trail.
  * Liquidation, trend-flip exit, costs and funding drag: identical to v1.

Own $1k book -> data/lev_perp_v2_state.json; judged via _lev_perp_v2_forward in
proof_scorecard, same pre-registered bar (n>=30, family-wise t). PAPER ONLY.

    python lev_perp_v2_paper.py     # process any newly-closed daily bar, then exit
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

import lev_perp_paper as lp

ATR_N       = int(os.getenv("LEV_PERP_V2_ATR_N", "14"))
ATR_MULT    = float(os.getenv("LEV_PERP_V2_ATR_MULT", "2.0"))
STATE_FILE  = Path(os.getenv("LEV_PERP_V2_STATE_FILE", "data/lev_perp_v2_state.json"))


def _atr(bars: list[dict], n: int = ATR_N) -> float | None:
    """Simple mean true range of the last n daily bars."""
    if len(bars) < n + 1:
        return None
    trs = []
    for i in range(len(bars) - n, len(bars)):
        h, l, pc = bars[i]["h"], bars[i]["l"], bars[i - 1]["c"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    return sum(trs) / n


def _load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"positions": {}, "closed": [], "last_bar_t": {},
            "leverage": lp.LEVERAGE, "atr_mult": ATR_MULT,
            "starting_equity": lp.STARTING_EQUITY, "equity": lp.STARTING_EQUITY,
            "started_at": datetime.now(timezone.utc).isoformat()}


def _save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(exist_ok=True)
    tmp = STATE_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2))
    tmp.replace(STATE_FILE)


def _open(state: dict, base: str, side: int, price: float, ts: str,
          bars: list[dict], closes: list[float]) -> None:
    lev = lp._effective_leverage(closes)
    margin = lp._entry_margin(state, side)
    liq_frac = max(1e-6, (1.0 - lp.MAINT) / lev)
    atr = _atr(bars) or price * 0.02  # ~2% fallback during warm-up
    state["positions"][base] = {
        "symbol": base, "side": side, "entry": price, "entry_ts": ts,
        "margin_usd": margin, "leverage": round(lev, 2),
        "notional_usd": round(margin * lev, 2),
        "peak": price,                                   # most favorable price so far
        "atr": round(atr, 8),                            # frozen at entry (stable trail width)
        "trail": round(price - side * ATR_MULT * atr, 8),
        "liq": round(price * (1 - side * liq_frac), 8),
    }


def _close(state: dict, base: str, price: float, ts: str, reason: str) -> dict:
    pos      = state["positions"].pop(base)
    side     = pos["side"]
    notional = pos["notional_usd"]
    ret      = side * (price - pos["entry"]) / pos["entry"]
    gross    = notional * ret
    cost     = notional * lp.TRADE_COST_FRAC
    funding  = lp._funding_cost(notional, pos["entry_ts"], ts)
    net      = gross - cost - funding
    state["equity"] = state.get("equity", lp.STARTING_EQUITY) + net
    rec = {"symbol": base, "side": side, "entry_ts": pos["entry_ts"], "exit_ts": ts,
           "entry": pos["entry"], "exit": price, "margin_usd": pos["margin_usd"],
           "leverage": pos["leverage"], "notional_usd": notional,
           "funding_cost": round(funding, 4), "cost": round(cost, 4),
           "pnl": round(net, 4), "pnl_pct_margin": round(net / pos["margin_usd"] * 100, 2),
           "price_move_pct": round(ret * 100, 3), "reason": reason,
           "equity_after": round(state["equity"], 2)}
    state["closed"].append(rec)
    return rec


def _check_exit(pos: dict, bar: dict) -> tuple[float, str] | None:
    """Exit vs PRIOR-bar levels only. Liquidation > trail stop (conservative)."""
    side, trail, liq = pos["side"], pos["trail"], pos["liq"]
    if side > 0:
        if bar["l"] <= liq:
            return liq, "liquidation"
        if bar["l"] <= trail:
            return trail, "trail_stop"
    else:
        if bar["h"] >= liq:
            return liq, "liquidation"
        if bar["h"] >= trail:
            return trail, "trail_stop"
    return None


def _ratchet(pos: dict, bar: dict) -> None:
    """Advance peak/trail from this bar's favorable extreme; never loosen."""
    side = pos["side"]
    extreme = bar["h"] if side > 0 else bar["l"]
    if side * (extreme - pos["peak"]) > 0:
        pos["peak"] = extreme
        new_trail = extreme - side * ATR_MULT * pos["atr"]
        if side * (new_trail - pos["trail"]) > 0:
            pos["trail"] = round(new_trail, 8)


def process_symbol(base: str, bars: list[dict], state: dict) -> int:
    closes = [b["c"] for b in bars]
    if len(closes) < lp.SMA_N + 1:
        print(f"{base}: warm-up ({len(closes)}/{lp.SMA_N + 1} daily bars)")
        return 0
    last_t = state["last_bar_t"].get(base)
    latest = bars[-1]
    sma = lp._sma(closes, lp.SMA_N)

    if last_t is None:
        state["last_bar_t"][base] = latest["t"]
        skip_reasons: list = []
        if lp._news_halted():
            print(f"{base}: SEED SKIPPED — news halt active")
        elif lp._entry_filter(bars, closes, skip_reasons):
            side = lp._target_side(latest["c"], sma)
            _open(state, base, side, latest["c"], str(latest["t"]), bars, closes)
            p = state["positions"][base]
            print(f"{base}: SEED {'LONG' if side > 0 else 'SHORT'} {p['leverage']:g}x "
                  f"@ {latest['c']:.2f} (trail {p['trail']:.2f} liq {p['liq']:.2f})")
        else:
            print(f"{base}: SEED SKIPPED — filters: {', '.join(skip_reasons)}")
        return 0

    new = [b for b in bars if b["t"] > last_t]
    acted = 0
    for bar in new:
        idx = bars.index(bar)
        s = lp._sma(closes[: idx + 1], lp.SMA_N)
        if s is None:
            continue

        # 1) Exit vs prior-bar trail/liq
        pos = state["positions"].get(base)
        if pos:
            ex = _check_exit(pos, bar)
            if ex:
                price, reason = ex
                rec = _close(state, base, price, str(bar["t"]), reason)
                tag = "TRAIL🛑" if reason == "trail_stop" else "LIQ❌"
                print(f"{base}: {tag} {'LONG' if pos['side'] > 0 else 'SHORT'} @ {price:.2f} "
                      f"net=${rec['pnl']:+.2f} ({rec['pnl_pct_margin']:+.1f}% margin)")
                acted += 1

        # 2) Trend-flip exit at bar close
        pos = state["positions"].get(base)
        want = lp._target_side(bar["c"], s)
        if pos and pos["side"] != want:
            rec = _close(state, base, bar["c"], str(bar["t"]), "flip")
            print(f"{base}: FLIP-CLOSE {'LONG' if pos['side'] > 0 else 'SHORT'} "
                  f"@ {bar['c']:.2f} net=${rec['pnl']:+.2f}")
            acted += 1

        # 3) Ratchet the survivors from this bar's extremes
        pos = state["positions"].get(base)
        if pos:
            _ratchet(pos, bar)

        # 4) Re-enter only if filters pass
        if not state["positions"].get(base):
            skip_reasons: list = []
            bars_to_idx = bars[: idx + 1]
            closes_to_idx = closes[: idx + 1]
            if lp._news_halted():
                print(f"{base}: SKIP entry — news halt active")
            elif lp._entry_filter(bars_to_idx, closes_to_idx, skip_reasons):
                _open(state, base, want, bar["c"], str(bar["t"]), bars_to_idx, closes_to_idx)
                p = state["positions"][base]
                print(f"{base}: OPEN {'LONG' if want > 0 else 'SHORT'} {p['leverage']:g}x "
                      f"@ {bar['c']:.2f} (margin ${p['margin_usd']:.0f} trail {p['trail']:.2f})")
                acted += 1
            else:
                print(f"{base}: SKIP entry — {', '.join(skip_reasons)}")

        state["last_bar_t"][base] = bar["t"]
    return acted


def main():
    state = _load_state()
    total = 0
    for base, pair in lp.KRAKEN_PAIRS.items():
        try:
            bars = lp.fetch_closed_daily(pair)
        except Exception as e:
            print(f"{base}: fetch failed - {e}")
            continue
        total += process_symbol(base, bars, state)
    _save_state(state)
    eq    = state.get("equity", lp.STARTING_EQUITY)
    start = state.get("starting_equity", lp.STARTING_EQUITY)
    held  = {b: (("L" if p["side"] > 0 else "S") + f"{p['leverage']:g}x")
             for b, p in state["positions"].items()}
    print(f"[lev_perp_v2] {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC  "
          f"equity=${eq:.2f} (start ${start:.0f}, {eq-start:+.2f}) "
          f"exit=chandelier {ATR_MULT:g}xATR({ATR_N}) "
          f"universe={list(lp.KRAKEN_PAIRS)} acted={total} held={held} "
          f"closed={len(state['closed'])}")


if __name__ == "__main__":
    main()

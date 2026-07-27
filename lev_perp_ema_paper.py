#!/usr/bin/env python3
"""
LEVERAGED perp paper arm EMA — same mechanics as lev_perp_v2, DIFFERENT
directional-bias signal.

RESEARCH BASIS (2026-07-27, backtest_directional_bias.py): v1/v2's directional
signal is sign(close - SMA(50)) -- a plain, lagging trend sign. Tested it
against four alternatives (Supertrend(10,2.5), MACD(12,26,9) histogram sign,
DMI/DI+-DI-, EMA(21) vs EMA(55) cross) over 5 years of daily OKX bars
(BTC/ETH/SOL), holding every other mechanic fixed (same 4 entry filters, same
chandelier exit, same costs, same leverage). EMA(21/55) cross + chandelier
exit was the standout: +$3,250 vs SMA50's +$1,279 over the same window,
positive in EVERY year 2021-2026 (SMA50 had two losing years) and positive on
all three symbols. Raw t-stat 2.12 does NOT clear the family-wise bar (2.80)
given 10 configurations were tried -- a real backtest lead, not proof. This
arm exists to earn that proof on the forward clock, same as v1-vs-v2.

  * ENTRY: side = sign(EMA(21) - EMA(55)) instead of sign(close - SMA(50)).
    Same four filters as v1/v2 (RSI<45, trend-age>=8d -- now measured on the
    EMA-cross's own persistence, not SMA's, volume>=1.2x, ADX dead-zone skip),
    same vol-targeted leverage, correlation-capped margin, news-halt gate.
    RSI/ADX/volume filter functions are IMPORTED from lev_perp_paper unchanged
    (one source of truth).
  * EXIT: identical ATR(14) x2 chandelier trail to v2 -- _atr/_open/_close/
    _check_exit/_ratchet are IMPORTED from lev_perp_v2_paper unchanged (they
    are side-agnostic: side lives in the position dict, not in these
    functions), so the exit engine is byte-for-byte the same proven mechanism.
  * Liquidation, trend-flip exit, costs and funding drag: identical to v1/v2.

Own $1k book -> data/lev_perp_ema_state.json; register _lev_perp_ema_forward
in proof_scorecard.py (same pre-registered bar: n>=30, family-wise t) for the
three-way verdict against v1 and v2. PAPER ONLY.

    python lev_perp_ema_paper.py     # process any newly-closed daily bar, then exit
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

import lev_perp_paper as lp
import lev_perp_v2_paper as v2

EMA_FAST    = int(os.getenv("LEV_PERP_EMA_FAST", "21"))
EMA_SLOW    = int(os.getenv("LEV_PERP_EMA_SLOW", "55"))
STATE_FILE  = Path(os.getenv("LEV_PERP_EMA_STATE_FILE", "data/lev_perp_ema_state.json"))


def _ema(closes: list[float], n: int) -> list[float] | None:
    """Full EMA series (not just latest) -- needed for the persistence/age scan."""
    if len(closes) < n:
        return None
    k = 2.0 / (n + 1)
    out = [sum(closes[:n]) / n]  # seed with SMA
    for c in closes[n:]:
        out.append(c * k + out[-1] * (1 - k))
    return out


def _ema_cross_side(closes: list[float]) -> int | None:
    """+1 if EMA(fast) > EMA(slow), -1 otherwise. None during warm-up."""
    ef, es = _ema(closes, EMA_FAST), _ema(closes, EMA_SLOW)
    if ef is None or es is None:
        return None
    # align: ef has len(closes)-EMA_FAST+1 points, es has len(closes)-EMA_SLOW+1
    return 1 if ef[-1] >= es[-1] else -1


def _trend_age_ema(closes: list[float]) -> int:
    """How many consecutive bars has the EMA-cross side been in place?"""
    if len(closes) < EMA_SLOW + 1:
        return 0
    current = _ema_cross_side(closes)
    if current is None:
        return 0
    age = 0
    for i in range(len(closes), EMA_SLOW - 1, -1):
        side = _ema_cross_side(closes[:i])
        if side is None or side != current:
            break
        age += 1
    return age


def _entry_filter_ema(bars: list[dict], closes: list[float], reason_out: list) -> bool:
    """Same as lev_perp_paper._entry_filter, but trend-age uses the EMA cross."""
    rsi_val = lp._rsi(closes, lp.RSI_PERIOD)
    if rsi_val is not None and rsi_val >= lp.RSI_MAX:
        reason_out.append(f"RSI={rsi_val:.1f}>={lp.RSI_MAX}")
        return False

    age = _trend_age_ema(closes)
    if age < lp.MIN_TREND_AGE:
        reason_out.append(f"trend_age={age}<{lp.MIN_TREND_AGE}")
        return False

    vr = lp._vol_ratio(bars, lp.VOL_LOOKBACK)
    if vr is not None and vr < lp.VOL_MULT:
        reason_out.append(f"vol_ratio={vr:.2f}<{lp.VOL_MULT}")
        return False

    if lp.SKIP_ADX_DEAD_ZONE:
        adx_val = lp._adx(bars[-(lp.ADX_PERIOD * 2 + 1):], lp.ADX_PERIOD)
        if adx_val is not None and lp.ADX_DEAD_LOW <= adx_val <= lp.ADX_DEAD_HIGH:
            reason_out.append(f"ADX={adx_val:.1f} in dead-zone [{lp.ADX_DEAD_LOW},{lp.ADX_DEAD_HIGH}]")
            return False

    return True


def _load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"positions": {}, "closed": [], "last_bar_t": {},
            "leverage": lp.LEVERAGE, "atr_mult": v2.ATR_MULT,
            "ema_fast": EMA_FAST, "ema_slow": EMA_SLOW,
            "starting_equity": lp.STARTING_EQUITY, "equity": lp.STARTING_EQUITY,
            "started_at": datetime.now(timezone.utc).isoformat()}


def _save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(exist_ok=True)
    tmp = STATE_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2))
    tmp.replace(STATE_FILE)


def process_symbol(base: str, bars: list[dict], state: dict) -> int:
    closes = [b["c"] for b in bars]
    if len(closes) < EMA_SLOW + 1:
        print(f"{base}: warm-up ({len(closes)}/{EMA_SLOW + 1} daily bars)")
        return 0
    last_t = state["last_bar_t"].get(base)
    latest = bars[-1]
    side_now = _ema_cross_side(closes)

    if last_t is None:
        state["last_bar_t"][base] = latest["t"]
        skip_reasons: list = []
        if lp._news_halted():
            print(f"{base}: SEED SKIPPED — news halt active")
        elif _entry_filter_ema(bars, closes, skip_reasons):
            v2._open(state, base, side_now, latest["c"], str(latest["t"]), bars, closes)
            p = state["positions"][base]
            print(f"{base}: SEED {'LONG' if side_now > 0 else 'SHORT'} {p['leverage']:g}x "
                  f"@ {latest['c']:.2f} (trail {p['trail']:.2f} liq {p['liq']:.2f})")
        else:
            print(f"{base}: SEED SKIPPED — filters: {', '.join(skip_reasons)}")
        return 0

    new = [b for b in bars if b["t"] > last_t]
    acted = 0
    for bar in new:
        idx = bars.index(bar)
        closes_to_idx = closes[: idx + 1]
        s = _ema_cross_side(closes_to_idx)
        if s is None:
            continue
        bars_to_idx = bars[: idx + 1]

        # 1) Exit vs prior-bar trail/liq
        pos = state["positions"].get(base)
        if pos:
            ex = v2._check_exit(pos, bar)
            if ex:
                price, reason = ex
                rec = v2._close(state, base, price, str(bar["t"]), reason)
                tag = "TRAIL🛑" if reason == "trail_stop" else "LIQ❌"
                print(f"{base}: {tag} {'LONG' if pos['side'] > 0 else 'SHORT'} @ {price:.2f} "
                      f"net=${rec['pnl']:+.2f} ({rec['pnl_pct_margin']:+.1f}% margin)")
                acted += 1

        # 2) Trend-flip exit at bar close (EMA cross flips)
        pos = state["positions"].get(base)
        if pos and pos["side"] != s:
            rec = v2._close(state, base, bar["c"], str(bar["t"]), "flip")
            print(f"{base}: FLIP-CLOSE {'LONG' if pos['side'] > 0 else 'SHORT'} "
                  f"@ {bar['c']:.2f} net=${rec['pnl']:+.2f}")
            acted += 1

        # 3) Ratchet the survivors from this bar's extremes
        pos = state["positions"].get(base)
        if pos:
            v2._ratchet(pos, bar)

        # 4) Re-enter only if filters pass
        if not state["positions"].get(base):
            skip_reasons: list = []
            if lp._news_halted():
                print(f"{base}: SKIP entry — news halt active")
            elif _entry_filter_ema(bars_to_idx, closes_to_idx, skip_reasons):
                v2._open(state, base, s, bar["c"], str(bar["t"]), bars_to_idx, closes_to_idx)
                p = state["positions"][base]
                print(f"{base}: OPEN {'LONG' if s > 0 else 'SHORT'} {p['leverage']:g}x "
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
    print(f"[lev_perp_ema] {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC  "
          f"equity=${eq:.2f} (start ${start:.0f}, {eq-start:+.2f}) "
          f"signal=EMA({EMA_FAST}/{EMA_SLOW})cross exit=chandelier {v2.ATR_MULT:g}xATR({v2.ATR_N}) "
          f"universe={list(lp.KRAKEN_PAIRS)} acted={total} held={held} "
          f"closed={len(state['closed'])}")


if __name__ == "__main__":
    main()

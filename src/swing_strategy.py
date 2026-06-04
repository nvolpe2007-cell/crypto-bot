"""
Swing strategy — long-only majors, higher timeframe (4h/daily).

WHY THIS EXISTS (read this before touching it):
The scalper loses because at 2-second timeframes the ~0.30% round-trip cost is
bigger than the move it chases — structurally negative-EV, unfixable by tuning.
This strategy lives where cost is noise: 4h bars, winners of +3-8%, held for
days. It is LONG-ONLY because a US Kraken-spot account cannot short — so every
trade here is actually executable with real money.

WHAT IT IS (and is NOT):
It does NOT predict where price is going — nothing can. It harvests the single
most evidence-backed anomaly in every asset class: time-series momentum (trend
persistence). The edge is small and statistical; the money comes from R:R and
risk control, not from being right often. A ~40% win rate with 3:1 winners is
profitable. That is the whole game.

THE EDGE STACK (fixed, deliberately simple — complexity is how you overfit):
  1. Trend filter      — only trade WITH a confirmed uptrend (close>EMA50, EMA20>EMA50)
  2. Momentum confirm  — rate-of-change positive over the lookback
  3. Pullback entry    — enter as RSI resumes UP through 50 (buy the dip in an uptrend,
                         not the breakout top)
  4. Overbought veto   — never chase (RSI must be below the overbought band)
  5. Cost gate         — the ATR-based target must dwarf round-trip cost (>=3x)

Exit: ATR stop (risk), ATR target (reward), or trend break (close<EMA50). The
stop/target are tracked intrabar by the runner; evaluate() flags trend-break
exits on bar close.

Every gate's pass/fail and every indicator value is returned on the decision so
nothing the strategy "thinks" is hidden. See decision_log.py.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Dict, List, Optional

# Round-trip cost as a fraction of price (taker fee + spread + slippage). Same
# basis as the rest of the system. At 4h ATR the target clears this easily; the
# gate exists to refuse the rare low-vol setup where it wouldn't.
ROUND_TRIP_COST_FRAC = float(os.getenv("SWING_ROUND_TRIP_COST_FRAC", "0.003"))


# ── indicator helpers (dependency-light, list-based, unit-testable) ──────────

def _ema(values: List[float], period: int) -> Optional[List[float]]:
    """Full EMA series (same length as values, None-free) or None if too short."""
    if len(values) < period:
        return None
    k = 2.0 / (period + 1.0)
    ema = sum(values[:period]) / period          # seed with SMA
    out = [None] * (period - 1) + [ema]
    for v in values[period:]:
        ema = v * k + ema * (1.0 - k)
        out.append(ema)
    return out


def _rsi_series(closes: List[float], period: int = 14) -> Optional[List[float]]:
    """Wilder RSI series; out[i] aligned to closes[i] (None during warm-up)."""
    if len(closes) < period + 1:
        return None
    gains, losses = [], []
    for i in range(1, len(closes)):
        ch = closes[i] - closes[i - 1]
        gains.append(max(ch, 0.0))
        losses.append(max(-ch, 0.0))
    out: List[Optional[float]] = [None] * len(closes)
    avg_g = sum(gains[:period]) / period
    avg_l = sum(losses[:period]) / period
    def _rsi(g, l):
        if l == 0:
            return 100.0
        rs = g / l
        return 100.0 - 100.0 / (1.0 + rs)
    out[period] = _rsi(avg_g, avg_l)
    for i in range(period + 1, len(closes)):
        avg_g = (avg_g * (period - 1) + gains[i - 1]) / period
        avg_l = (avg_l * (period - 1) + losses[i - 1]) / period
        out[i] = _rsi(avg_g, avg_l)
    return out


def _atr(bars: List[dict], period: int = 14) -> Optional[float]:
    """Wilder ATR over the last `period` true ranges. bars: dicts with h/l/c."""
    if len(bars) < period + 1:
        return None
    trs = []
    for i in range(1, len(bars)):
        h, l, pc = bars[i]['h'], bars[i]['l'], bars[i - 1]['c']
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    atr = sum(trs[:period]) / period
    for tr in trs[period:]:
        atr = (atr * (period - 1) + tr) / period
    return atr


def _roc(closes: List[float], period: int) -> Optional[float]:
    """Rate of change over `period` bars, as a fraction."""
    if len(closes) <= period or closes[-1 - period] == 0:
        return None
    return (closes[-1] - closes[-1 - period]) / closes[-1 - period]


# ── decision type ────────────────────────────────────────────────────────────

@dataclass
class SwingDecision:
    symbol: str
    ts: str
    price: float
    action: str                       # ENTER | HOLD | SKIP | EXIT
    reason: str
    gates: Dict[str, bool] = field(default_factory=dict)
    indicators: Dict[str, float] = field(default_factory=dict)
    # populated on ENTER
    stop_price: float = 0.0
    target_price: float = 0.0
    stop_pct: float = 0.0
    target_pct: float = 0.0
    rr: float = 0.0

    @property
    def is_enter(self) -> bool:
        return self.action == "ENTER"


# ── strategy ─────────────────────────────────────────────────────────────────

class SwingStrategy:
    """Stateless evaluator. Feed it the ascending OHLC history; it judges the
    LAST (just-closed) bar. The caller owns position state and passes it in."""

    def __init__(self, *, ema_fast=20, ema_slow=50, rsi_period=14, atr_period=14,
                 roc_period=20, atr_stop_mult=2.0, atr_target_mult=3.0,
                 rsi_overbought=68.0, min_target_cost_mult=3.0,
                 round_trip_cost=ROUND_TRIP_COST_FRAC):
        self.ema_fast = ema_fast
        self.ema_slow = ema_slow
        self.rsi_period = rsi_period
        self.atr_period = atr_period
        self.roc_period = roc_period
        self.atr_stop_mult = atr_stop_mult
        self.atr_target_mult = atr_target_mult
        self.rsi_overbought = rsi_overbought
        self.min_target_cost_mult = min_target_cost_mult
        self.round_trip_cost = round_trip_cost
        self.min_bars = max(ema_slow, rsi_period + 1, atr_period + 1, roc_period + 1) + 2

    def evaluate(self, bars: List[dict], position_open: bool) -> SwingDecision:
        """bars: ascending list of {t,o,h,l,c}. Evaluates bars[-1]."""
        last = bars[-1]
        ts, price = str(last.get('t', '')), float(last['c'])

        if len(bars) < self.min_bars:
            return SwingDecision(last.get('symbol', '?'), ts, price, "SKIP",
                                 f"warm-up ({len(bars)}/{self.min_bars} bars)")

        symbol = last.get('symbol', '?')
        closes = [b['c'] for b in bars]
        ema_f = _ema(closes, self.ema_fast)[-1]
        ema_s = _ema(closes, self.ema_slow)[-1]
        rsi_ser = _rsi_series(closes, self.rsi_period)
        rsi, rsi_prev = rsi_ser[-1], rsi_ser[-2]
        atr = _atr(bars, self.atr_period)
        roc = _roc(closes, self.roc_period)

        ind = {"close": price, "ema_fast": ema_f, "ema_slow": ema_s,
               "rsi": rsi, "rsi_prev": rsi_prev, "atr": atr, "roc": roc}

        # ── EXIT path (we hold a position) ──────────────────────────────────
        if position_open:
            if price < ema_s:
                return SwingDecision(symbol, ts, price, "EXIT",
                                     f"trend break: close {price:.2f} < EMA{self.ema_slow} {ema_s:.2f}",
                                     indicators=ind)
            return SwingDecision(symbol, ts, price, "HOLD",
                                 "trend intact — let it run (stop/target tracked intrabar)",
                                 indicators=ind)

        # ── ENTRY path (flat) — every gate explicit ─────────────────────────
        target_pct = (atr * self.atr_target_mult) / price
        stop_pct = (atr * self.atr_stop_mult) / price
        gates = {
            "trend_up":        price > ema_s and ema_f > ema_s,
            "momentum_pos":    roc is not None and roc > 0,
            "pullback_resume": rsi_prev < 50.0 <= rsi,        # RSI crossing UP through 50
            "not_overbought":  rsi < self.rsi_overbought,
            "cost_clears":     target_pct >= self.round_trip_cost * self.min_target_cost_mult,
        }
        if all(gates.values()):
            stop_price = price - atr * self.atr_stop_mult
            target_price = price + atr * self.atr_target_mult
            rr = target_pct / stop_pct if stop_pct > 0 else 0.0
            return SwingDecision(
                symbol, ts, price, "ENTER",
                f"uptrend pullback resuming (RSI {rsi_prev:.0f}→{rsi:.0f}, ROC {roc*100:+.1f}%)",
                gates=gates, indicators=ind,
                stop_price=stop_price, target_price=target_price,
                stop_pct=stop_pct, target_pct=target_pct, rr=rr,
            )

        failed = [k for k, v in gates.items() if not v]
        return SwingDecision(symbol, ts, price, "SKIP",
                             "failed: " + ", ".join(failed),
                             gates=gates, indicators=ind)

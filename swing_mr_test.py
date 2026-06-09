#!/usr/bin/env python3
"""
THESIS TEST: mean-reversion (buy-the-dip) vs momentum (buy-the-breakout), head to
head on the same ~417d of 4h-majors data, same risk framework, honest cost.

The locked SwingStrategy is MOMENTUM: enter on uptrend + ROC>=2% + RSI crossing UP
through 50. Its one-year backtest was NET NEGATIVE (-$0.47/trade), and the 0%-win
quarters looked like "buying breakouts that revert". So we test the OPPOSITE entry
trigger with everything else held constant:

  MeanReversionSwing — buy a DIP in an uptrend:
    * price > EMA100   (long-term uptrend intact — don't catch falling knives)
    * price < EMA20    (pulled back below the short-term mean — a dip, not a breakout)
    * RSI crossing UP through oversold (rsi_prev < 35 <= rsi — the bounce is starting)
    * same ATR stop (2x) / target (3x) and same cost gate (target >= 3x round-trip)

ONE pre-specified variant, ONE test — deliberately NOT a parameter search (that is
the overfit we are trying to avoid). NOT proof; an in-sample thesis screen. If MR is
also flat/negative, that is strong evidence the 0.54% cost wall beats retail
directional signals on majors at this size — a final, valuable answer.

    python swing_mr_test.py
"""
from __future__ import annotations
import time
from pathlib import Path

import swing_backtest as bt
from swing_backtest_1y import fetch_4h, SYMBOLS, TARGET_DAYS
from src.swing_strategy import (
    SwingStrategy, SwingDecision, _ema, _rsi_series, _atr, ROUND_TRIP_COST_FRAC,
)
from src.decision_log import DecisionLog


class MeanReversionSwing:
    """Same interface as SwingStrategy; OPPOSITE entry thesis (buy dips, not breakouts)."""

    def __init__(self, *, ema_fast=20, ema_trend=100, rsi_period=14, atr_period=14,
                 rsi_oversold=35.0, atr_stop_mult=2.0, atr_target_mult=3.0,
                 min_target_cost_mult=3.0, round_trip_cost=ROUND_TRIP_COST_FRAC):
        self.ema_fast = ema_fast
        self.ema_trend = ema_trend
        self.rsi_period = rsi_period
        self.atr_period = atr_period
        self.rsi_oversold = rsi_oversold
        self.atr_stop_mult = atr_stop_mult
        self.atr_target_mult = atr_target_mult
        self.min_target_cost_mult = min_target_cost_mult
        self.round_trip_cost = round_trip_cost
        self.min_bars = max(ema_trend, rsi_period + 1, atr_period + 1) + 2

    def evaluate(self, bars, position_open: bool) -> SwingDecision:
        last = bars[-1]
        ts, price = str(last.get("t", "")), float(last["c"])
        symbol = last.get("symbol", "?")
        if len(bars) < self.min_bars:
            return SwingDecision(symbol, ts, price, "SKIP",
                                 f"warm-up ({len(bars)}/{self.min_bars})")
        closes = [b["c"] for b in bars]
        ema_f = _ema(closes, self.ema_fast)[-1]
        ema_t = _ema(closes, self.ema_trend)[-1]
        rsi_ser = _rsi_series(closes, self.rsi_period)
        rsi, rsi_prev = rsi_ser[-1], rsi_ser[-2]
        atr = _atr(bars, self.atr_period)
        ind = {"close": price, "ema_fast": ema_f, "ema_trend": ema_t,
               "rsi": rsi, "rsi_prev": rsi_prev, "atr": atr}

        if position_open:
            # discretionary exit only on a LONG-TERM trend break; ATR stop/target
            # (tracked intrabar by the runner) handle the normal exits.
            if price < ema_t:
                return SwingDecision(symbol, ts, price, "EXIT",
                                     f"LT trend break: {price:.2f} < EMA{self.ema_trend} {ema_t:.2f}",
                                     indicators=ind)
            return SwingDecision(symbol, ts, price, "HOLD", "in-trade", indicators=ind)

        target_pct = (atr * self.atr_target_mult) / price
        stop_pct = (atr * self.atr_stop_mult) / price
        gates = {
            "uptrend_lt":   price > ema_t,                      # above long-term mean
            "in_dip":       price < ema_f,                      # below short-term mean
            "oversold_turn": rsi_prev < self.rsi_oversold <= rsi,  # RSI recovering from oversold
            "cost_clears":  target_pct >= self.round_trip_cost * self.min_target_cost_mult,
        }
        if all(gates.values()):
            return SwingDecision(
                symbol, ts, price, "ENTER",
                f"dip-buy in uptrend (RSI {rsi_prev:.0f}->{rsi:.0f}, below EMA{self.ema_fast})",
                gates=gates, indicators=ind,
                stop_price=price - atr * self.atr_stop_mult,
                target_price=price + atr * self.atr_target_mult,
                stop_pct=stop_pct, target_pct=target_pct,
                rr=(target_pct / stop_pct if stop_pct > 0 else 0.0))
        return SwingDecision(symbol, ts, price, "SKIP",
                             "no dip-buy setup", gates=gates, indicators=ind)


def run(strat_factory, series, dlog):
    nets = []
    for s in SYMBOLS:
        nets.append((s, bt.backtest_symbol(s, series[s], strat_factory(), dlog)))
    return nets


def report(title, per_symbol):
    print(f"\n{title}")
    all_nets = []
    for s, nets in per_symbol:
        st = bt.stats(nets); all_nets += nets
        print(f"  {s:<4} trades={st['n']:<3} net=${st['total']:+8.2f} "
              f"win={st['win']*100:3.0f}% exp=${st['exp']:+.4f} t={st['t']:+.2f}")
    c = bt.stats(all_nets)
    print(f"  COMBINED trades={c['n']} net=${c['total']:+.2f} win={c['win']*100:.0f}% "
          f"exp=${c['exp']:+.4f}/trade t={c['t']:+.2f} maxDD=${c['dd']:+.2f}")
    return c


def main():
    print("Fetching ~400d of 4h bars (CryptoCompare) for 6 majors...")
    series = {}
    for s in SYMBOLS:
        series[s] = fetch_4h(s, TARGET_DAYS); time.sleep(0.3)
    print(f"  done ({sum(len(v) for v in series.values())} bars)")

    bt.ROUND_TRIP_COST_FRAC = 0.0055
    dlog = DecisionLog(path=Path("data/_mr_test.jsonl"))
    print("\n" + "=" * 72)
    print("THESIS HEAD-TO-HEAD  (4h majors, ~417d, honest 0.55% cost)  [in-sample screen]")
    print("=" * 72)
    mom = report("MOMENTUM (locked SwingStrategy — buy breakouts):",
                 run(SwingStrategy, series, dlog))
    mr = report("MEAN-REVERSION (buy dips in uptrend):",
                run(MeanReversionSwing, series, dlog))
    print("\n" + "-" * 72)
    print(f"MOMENTUM       exp=${mom['exp']:+.4f}/trade  t={mom['t']:+.2f}  n={mom['n']}")
    print(f"MEAN-REVERSION exp=${mr['exp']:+.4f}/trade  t={mr['t']:+.2f}  n={mr['n']}")
    if mr['exp'] > 0 and mr['t'] > 1:
        print("=> MR shows in-sample signal — worth a forward test (NOT yet proof).")
    elif mr['exp'] > mom['exp']:
        print("=> MR is less-bad than momentum but still no clear edge.")
    else:
        print("=> MR also fails. Strong evidence the 0.54% cost wall beats retail")
        print("   directional signals on majors at this size.")


if __name__ == "__main__":
    main()

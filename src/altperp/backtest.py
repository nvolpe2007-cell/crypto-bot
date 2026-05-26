"""
Backtest — funding-extreme + trend-filter SHORT core (the faithfully-reproducible
part, per research). Data: Bybit funding history + 4h OHLCV (both free/exact).

DELIBERATE SIMPLIFICATIONS (so results aren't overstated):
  • SHORT setup only. The OI-spike gate, CVD, liq-proximity, and flush-LONG
    cannot be faithfully reconstructed from free history, so they're omitted.
    This backtests funding≥threshold AND not-strong-uptrend — a SUPERSET of the
    live short entries, so live will trade less, not more.
  • Bar-based intrabar exits, STOP checked before TP within a bar (conservative).
  • Kraken taker fee + slippage applied on every fill; funding paid while short.
Treat output as a sanity check on the core edge, not a precise P&L forecast.
"""

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Dict, List

from . import config
from .signals import trend_signal

logger = logging.getLogger(__name__)


@dataclass
class BTStats:
    trades: int = 0
    wins: int = 0
    net_pnl: float = 0.0
    gross_profit: float = 0.0
    gross_loss: float = 0.0
    fees: float = 0.0
    funding_paid: float = 0.0
    return_pct: float = 0.0
    max_drawdown_pct: float = 0.0
    exits: Dict[str, int] = field(default_factory=dict)

    @property
    def win_rate(self) -> float:
        return self.wins / self.trades * 100 if self.trades else 0.0

    @property
    def profit_factor(self) -> float:
        return self.gross_profit / abs(self.gross_loss) if self.gross_loss else float("inf")

    def render(self) -> str:
        return (
            f"trades={self.trades} win_rate={self.win_rate:.1f}% "
            f"net=${self.net_pnl:+.2f} ({self.return_pct:+.1f}%) "
            f"PF={self.profit_factor:.2f} maxDD={self.max_drawdown_pct:.1f}% "
            f"fees=${self.fees:.2f} funding=${self.funding_paid:.2f} exits={self.exits}"
        )


def _fill(price: float, side: str) -> float:
    s = config.PAPER_SLIPPAGE_PCT
    return price * (1 + s) if side == "buy" else price * (1 - s)


def run_backtest(klines: List[Dict], starting_equity: float = None) -> BTStats:
    """klines: oldest→newest, each {ts, open, high, low, close, volume, funding}
    where `funding` is the 8h funding rate active during that bar."""
    eq = starting_equity if starting_equity is not None else config.PAPER_STARTING_EQUITY
    st = BTStats()
    peak = eq
    pos = None
    warm = config.TREND_EMA_PERIOD + config.TREND_SLOPE_LOOKBACK + 1
    bars_per_hour = 0.25  # 4h bars
    time_stop_bars = int(config.TIME_STOP_HOURS / 4) or 1

    for i in range(len(klines)):
        bar = klines[i]
        funding = bar.get("funding", 0.0) or 0.0

        if pos is not None:
            closed_pnl = _process_short_bar(pos, bar, funding, st, i)
            if closed_pnl is not None:
                eq += closed_pnl
                st.net_pnl += closed_pnl
                st.wins += 1 if closed_pnl > 0 else 0
                if closed_pnl > 0:
                    st.gross_profit += closed_pnl
                else:
                    st.gross_loss += closed_pnl
                peak = max(peak, eq)
                dd = (peak - eq) / peak * 100 if peak else 0.0
                st.max_drawdown_pct = max(st.max_drawdown_pct, dd)
                pos = None
            continue

        if i < warm:
            continue
        trend = trend_signal(klines[: i + 1])
        if funding >= config.FUNDING_THRESHOLD_SHORT and not trend.get("strong_uptrend"):
            entry = _fill(bar["close"], "sell")
            risk = eq * config.BASE_RISK_PCT
            notional = min(risk / config.SHORT_STOP_PCT, eq * config.MAX_LEVERAGE)
            qty = notional / entry
            fee = notional * config.KRAKEN_TAKER_FEE
            eq -= fee
            st.fees += fee
            pos = SimpleNamespace(
                entry_price=entry, qty=qty, notional=notional, entry_bar=i,
                remaining=1.0, tp1=False, tp2=False, tp3=False,
                trail_active=False, anchor=None, realized=0.0, time_stop_bars=time_stop_bars,
            )
            st.trades += 1

    return st


def _close(pos, frac, price, reason, st):
    """Close `frac` of original at `price` (short → buy back). Returns pnl delta."""
    side = "buy"
    fp = _fill(price, side)
    qty = pos.qty * frac
    pnl = (pos.entry_price - fp) * qty
    fee = qty * fp * config.KRAKEN_TAKER_FEE
    st.fees += fee
    pos.realized += pnl - fee
    pos.remaining = round(pos.remaining - frac, 8)
    st.exits[reason] = st.exits.get(reason, 0) + 1
    return pnl - fee


def _process_short_bar(pos, bar, funding, st, i):
    """Apply exits for one bar. Returns total realized pnl IF the position fully
    closes this bar, else None. Stop checked before TP (conservative)."""
    entry = pos.entry_price
    high, low = bar["high"], bar["low"]

    # accrue funding while short (we are short → if funding +ve we RECEIVE it; the
    # crowd pays shorts. Model it as a credit per 8h funding event ≈ every 2 bars.)
    if i > pos.entry_bar and (i - pos.entry_bar) % 2 == 0:
        credit = funding * pos.notional * pos.remaining
        pos.realized += credit
        st.funding_paid -= credit  # negative funding_paid = net received

    # 1. Funding stop — setup invalidated
    if funding < config.FUNDING_EXIT_THRESHOLD:
        pos.realized += _close(pos, pos.remaining, bar["close"], "FUNDING_STOP", st)
        return pos.realized

    # 2. Stop / trail at the adverse extreme (bar high)
    if pos.trail_active and pos.anchor is not None:
        trail_stop = pos.anchor * (1 + config.SHORT_TRAIL_PCT)
        if high >= trail_stop:
            pos.realized += _close(pos, pos.remaining, trail_stop, "TRAIL", st)
            return pos.realized
    else:
        hard = entry * (1 + config.SHORT_STOP_PCT)
        if high >= hard:
            pos.realized += _close(pos, pos.remaining, hard, "STOP", st)
            return pos.realized

    # 3. Take-profits at the favorable extreme (bar low), in order
    if not pos.tp1 and low <= entry * (1 - config.SHORT_TP1_PCT):
        pos.tp1 = True
        pos.trail_active = True
        _close(pos, config.SHORT_TP1_CLOSE_PCT, entry * (1 - config.SHORT_TP1_PCT), "TP1", st)
    if not pos.tp2 and low <= entry * (1 - config.SHORT_TP2_PCT):
        pos.tp2 = True
        _close(pos, config.SHORT_TP2_CLOSE_PCT, entry * (1 - config.SHORT_TP2_PCT), "TP2", st)
    if not pos.tp3 and low <= entry * (1 - config.SHORT_TP3_PCT):
        pos.tp3 = True
        _close(pos, pos.remaining, entry * (1 - config.SHORT_TP3_PCT), "TP3", st)
        return pos.realized

    if pos.trail_active:
        pos.anchor = low if pos.anchor is None else min(pos.anchor, low)

    # 4. Time stop
    if (i - pos.entry_bar) >= pos.time_stop_bars:
        profit = (entry - bar["close"]) / entry
        if profit < config.TIME_STOP_MIN_PROFIT_PCT:
            pos.realized += _close(pos, pos.remaining, bar["close"], "TIME_STOP", st)
            return pos.realized

    if pos.remaining <= 1e-6:
        return pos.realized
    return None


async def fetch_history(coin: str, days: int = 90) -> List[Dict]:
    """Pull 4h klines + funding history from Bybit and merge funding onto bars.
    Runs where Bybit is reachable (the VPS). Returns bars with a `funding` field."""
    from .data import BybitData
    dc = BybitData()
    try:
        klines = await dc.klines(coin, interval="240", limit=min(days * 6, 1000))
        fhist = await dc._get("/v5/market/history-fund-rate",
                              {"category": "linear", "symbol": coin, "limit": 200})
        funds = []
        if fhist:
            for r in fhist.get("list", []):
                funds.append((int(r.get("fundingRateTimestamp", 0)), float(r.get("fundingRate", 0) or 0)))
        funds.sort()
        # assign each bar the latest funding rate at/just before its timestamp
        for bar in klines:
            rate = 0.0
            for ts, fr in funds:
                if ts <= bar["ts"]:
                    rate = fr
                else:
                    break
            bar["funding"] = rate
        return klines
    finally:
        await dc.close()


async def _amain(coin: str):
    bars = await fetch_history(coin)
    if not bars:
        print(f"no data for {coin} (Bybit reachable here?)")
        return
    st = run_backtest(bars)
    print(f"[BACKTEST {coin}] {len(bars)} 4h bars\n  {st.render()}")


def _selftest():
    # Synthetic: extreme funding + a clean down-move → a winning short
    bars = []
    price = 100.0
    for i in range(60):  # warmup, calm, modest uptrend-free
        price += (-0.02 if i % 2 else 0.02)
        bars.append({"ts": i * 14400000, "open": price, "high": price + 0.3,
                     "low": price - 0.3, "close": price, "volume": 1000, "funding": 0.0001})
    # extreme funding fires a short here, then price falls 5% over next bars
    bars.append({"ts": 60 * 14400000, "open": 100, "high": 100.2, "low": 99.8,
                 "close": 100.0, "volume": 1000, "funding": 0.0008})  # short entry
    p = 100.0
    for i in range(61, 70):
        p *= 0.992
        bars.append({"ts": i * 14400000, "open": p, "high": p + 0.2, "low": p - 0.5,
                     "close": p, "volume": 1000, "funding": 0.0006})
    st = run_backtest(bars, starting_equity=1000)
    # Funding stays extreme through the down-move, so it may re-enter — that's valid.
    assert st.trades >= 1, st.render()
    assert st.net_pnl > 0, st.render()       # the down-move should profit the short(s)
    assert st.exits, st.render()
    print("backtest selftest OK —", st.render())


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO)
    if len(sys.argv) > 1 and sys.argv[1] == "--selftest":
        _selftest()
    else:
        coin = sys.argv[1] if len(sys.argv) > 1 else "SOLUSDT"
        asyncio.run(_amain(coin))

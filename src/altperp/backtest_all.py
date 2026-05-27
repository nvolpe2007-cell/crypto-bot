"""
All-weather backtest — replays the live router (regime → strategy → entry → exits)
over historical 4h klines, so we can MEASURE per-strategy edge after costs instead
of assuming it. This is the evidence gate before trusting anything live.

HONEST SCOPE (so results aren't overstated):
  • trend-following (TRENDING_UP/DOWN) and mean-reversion (RANGING) are reconstructed
    FAITHFULLY from klines alone — these are the never-before-tested strategies that
    carry most market conditions, so they're the priority here.
  • fade-short (VOLATILE) needs the OI gate, which can't be rebuilt from history →
    measured separately by backtest.py (funding-only superset).
  • flush-long (CRASH) needs OI history → NOT backtestable; validate forward in paper.
  • The AI gate-keeper is NOT replayed (per-bar API cost). This measures the
    STRUCTURAL edge the brain sits on top of — the brain can only ever trade LESS.

One position at a time per coin. Costs: Kraken taker fee + slippage on every fill.
Stop checked before target within a bar (conservative). Regime size-scaling applied.
"""

import asyncio
import logging
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Dict, List, Optional

from . import config, regime as rg, router, trend as trend_mod, mean_reversion as mr_mod
from .math_utils import atr

logger = logging.getLogger(__name__)


@dataclass
class StratStats:
    trades: int = 0
    wins: int = 0
    net_pnl: float = 0.0
    gross_profit: float = 0.0
    gross_loss: float = 0.0
    fees: float = 0.0
    exits: Dict[str, int] = field(default_factory=dict)

    @property
    def win_rate(self) -> float:
        return self.wins / self.trades * 100 if self.trades else 0.0

    @property
    def profit_factor(self) -> float:
        return self.gross_profit / abs(self.gross_loss) if self.gross_loss else float("inf")

    def record(self, pnl: float, reason: str):
        self.trades += 1
        self.net_pnl += pnl
        if pnl > 0:
            self.wins += 1
            self.gross_profit += pnl
        else:
            self.gross_loss += pnl
        self.exits[reason] = self.exits.get(reason, 0) + 1

    def render(self) -> str:
        return (f"trades={self.trades:3d} win={self.win_rate:5.1f}% "
                f"net=${self.net_pnl:+8.2f} PF={self.profit_factor:4.2f} "
                f"exits={self.exits}")


def _fill(price: float, side: str) -> float:
    s = config.PAPER_SLIPPAGE_PCT
    return price * (1 + s) if side == "buy" else price * (1 - s)


def _open(setup, strat: str, bar: Dict, eq: float, scale: float, i: int):
    """Open a position from a strategy setup. Returns (pos, entry_fee)."""
    direction = setup.direction
    entry = _fill(bar["close"], "sell" if direction == "short" else "buy")
    stop_frac = getattr(setup, "stop_frac", 0.0) or config.MR_STOP_PCT
    risk = eq * config.BASE_RISK_PCT
    notional = min(risk / stop_frac * setup.size_multiplier * scale, eq * config.MAX_LEVERAGE)
    qty = notional / entry if entry else 0.0
    fee = notional * config.KRAKEN_TAKER_FEE
    pos = SimpleNamespace(
        strat=strat, setup_type=setup.setup_type, direction=direction,
        entry_price=entry, qty=qty, notional=notional, entry_bar=i, stop_frac=stop_frac,
        atr=getattr(setup, "atr", 0.0),
        anchor=(bar["high"] if direction == "long" else bar["low"]),
        target=getattr(setup, "target_price", 0.0),
        realized=-fee,  # entry fee paid up front
        time_stop_bars=max(1, int(config.MR_TIME_STOP_HOURS / 4)),
    )
    return pos, fee


def _close(pos, price: float, reason: str):
    """Fully close `pos` at `price`. Returns (total_pnl_incl_fees, reason)."""
    fp = _fill(price, "buy" if pos.direction == "short" else "sell")
    pnl = (fp - pos.entry_price) * pos.qty if pos.direction == "long" \
        else (pos.entry_price - fp) * pos.qty
    exit_fee = pos.qty * fp * config.KRAKEN_TAKER_FEE
    return pos.realized + pnl - exit_fee, reason


def _exit_trend(pos, bar: Dict):
    """ATR chandelier: trail a stop TREND_ATR_STOP_MULT×ATR from the best extreme
    since entry. Let winners run; cut when the trend breaks."""
    m = config.TREND_ATR_STOP_MULT
    if pos.direction == "long":
        pos.anchor = max(pos.anchor, bar["high"])
        stop = pos.anchor - m * pos.atr
        if bar["low"] <= stop:
            return _close(pos, stop, "TREND_STOP")
    else:
        pos.anchor = min(pos.anchor, bar["low"])
        stop = pos.anchor + m * pos.atr
        if bar["high"] >= stop:
            return _close(pos, stop, "TREND_STOP")
    return None


def _exit_mr(pos, bar: Dict, i: int):
    """Mean-reversion: revert-to-mean target, hard stop, time stop. Stop first."""
    entry = pos.entry_price
    if pos.direction == "long":
        stop = entry * (1 - pos.stop_frac)
        if bar["low"] <= stop:
            return _close(pos, stop, "MR_STOP")
        if pos.target and bar["high"] >= pos.target:
            return _close(pos, pos.target, "MR_TARGET")
    else:
        stop = entry * (1 + pos.stop_frac)
        if bar["high"] >= stop:
            return _close(pos, stop, "MR_STOP")
        if pos.target and bar["low"] <= pos.target:
            return _close(pos, pos.target, "MR_TARGET")
    if (i - pos.entry_bar) >= pos.time_stop_bars:
        return _close(pos, bar["close"], "MR_TIME")
    return None


def run_all_weather_backtest(klines: List[Dict], coin: str = "SOLUSDT",
                             starting_equity: float = None) -> Dict[str, StratStats]:
    """Replay the router over `klines` (oldest→newest). Returns per-strategy stats
    plus a 'COMBINED' bucket. Only trend + mean-reversion are simulated here."""
    eq = starting_equity if starting_equity is not None else config.PAPER_STARTING_EQUITY
    stats = {"trend": StratStats(), "mean_reversion": StratStats(), "COMBINED": StratStats()}
    warm = max(config.TREND_EMA_PERIOD + config.TREND_SLOPE_LOOKBACK + 2,
               config.MR_LOOKBACK + 1, config.TREND_BREAKOUT_BARS + 2)
    pos = None

    for i in range(len(klines)):
        bar = klines[i]
        if pos is not None:
            res = _exit_trend(pos, bar) if pos.strat == "trend" else _exit_mr(pos, bar, i)
            if res is not None:
                pnl, reason = res
                eq += pnl
                stats[pos.strat].record(pnl, reason)
                stats["COMBINED"].record(pnl, reason)
                pos = None
            continue
        if i < warm:
            continue

        window = klines[: i + 1]
        reg = rg.classify(window)
        scale = router.REGIME_SIZE_SCALE.get(reg.regime, 0.0)
        if scale <= 0:
            continue
        for name in router.eligible_strategies(reg.regime):
            if name == "trend":
                setup = trend_mod.evaluate(coin, window, reg)
            elif name == "mean_reversion":
                setup = mr_mod.evaluate(coin, window, reg)
            else:
                continue  # fade/flush handled elsewhere (see module docstring)
            if setup.should_enter:
                pos, fee = _open(setup, name, bar, eq, scale, i)
                eq -= fee
                break

    return stats


async def _amain(coin: str):
    from .backtest import fetch_history
    bars = await fetch_history(coin, days=166)  # ~1000 4h bars, Bybit max
    if not bars:
        print(f"no data for {coin} (Bybit reachable here? run on the VPS)")
        return
    stats = run_all_weather_backtest(bars, coin=coin)
    print(f"[ALL-WEATHER BACKTEST {coin}] {len(bars)} 4h bars (~{len(bars)//6}d)")
    for name in ("trend", "mean_reversion", "COMBINED"):
        print(f"  {name:15s} {stats[name].render()}")


def _selftest():
    # A clean uptrend then a sharp reversal: trend-long should enter and exit on the
    # chandelier with a net gain after the ride.
    closes = [100 + i * 2.0 for i in range(80)] + [260 - i * 4.0 for i in range(15)]
    bars = [{"ts": i * 14400000, "open": c, "high": c + 1.0, "low": c - 1.0,
             "close": c, "volume": 1000.0} for i, c in enumerate(closes)]
    stats = run_all_weather_backtest(bars, starting_equity=1000)
    assert stats["trend"].trades >= 1, stats["trend"].render()
    assert stats["trend"].net_pnl > 0, stats["trend"].render()  # rode the trend up
    assert "TREND_STOP" in stats["trend"].exits, stats["trend"].exits

    # A RANGING tape (moderate vol so it isn't classed CALM) past the warmup, then a
    # spike below the mean → mr_long that reverts to the target (a win). Bar ranges
    # of ±1.25 give ATR ≈ 2.5% (between CALM 1.5% and VOLATILE 4%).
    rng = [99.0 if i % 2 else 101.0 for i in range(60)]
    bars2 = [{"ts": i * 14400000, "open": c, "high": c + 1.25, "low": c - 1.25,
              "close": c, "volume": 1000.0} for i, c in enumerate(rng)]
    bars2.append({"ts": 60 * 14400000, "open": 99, "high": 99, "low": 97,
                  "close": 97.5, "volume": 1000.0})    # ~2.4% dip: z≤-2 but still RANGING
    bars2.append({"ts": 61 * 14400000, "open": 97.5, "high": 100.5, "low": 97.5,
                  "close": 100.0, "volume": 1000.0})   # reverts to mean → MR_TARGET
    st2 = run_all_weather_backtest(bars2, starting_equity=1000)
    assert st2["mean_reversion"].trades >= 1, st2["mean_reversion"].render()
    print("backtest_all selftest OK")
    print("  trend:", stats["trend"].render())
    print("  mr:   ", st2["mean_reversion"].render())


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO)
    if len(sys.argv) > 1 and sys.argv[1] == "--selftest":
        _selftest()
    else:
        coin = sys.argv[1] if len(sys.argv) > 1 else "SOLUSDT"
        asyncio.run(_amain(coin))

"""6-step TJR confluence checklist → EntrySignal."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

import pandas as pd
import numpy as np

from .sessions import is_tradeable, current_session
from .levels import KeyLevels
from .swing_points import find_swings, last_n_swings
from .sweep import scan_all_levels, Sweep
from .structure import detect_bos, BOS
from .fvg import scan_fvgs, nearest_fvg, FVG
from .order_block import find_order_block, OrderBlock
from .htf_bias import compute_bias, bias_allows

logger = logging.getLogger(__name__)


@dataclass
class EntrySignal:
    symbol: str
    direction: str        # "long" | "short"
    entry_price: float    # limit order price (FVG mid or OB 50%)
    stop_price: float     # stop-loss
    target_price: float   # take-profit
    rr: float             # reward:risk ratio
    setup_type: str       # "fvg" | "ob" | "fvg+ob"
    session: str
    sweep: Sweep
    bos: BOS
    fvg: Optional[FVG]
    order_block: Optional[OrderBlock]
    htf_bias: str
    timestamp: datetime


@dataclass
class CheckResult:
    passed: bool
    reason: str           # human-readable trace


def _atr(bars: pd.DataFrame, period: int = 14) -> float:
    """Simple ATR calculation."""
    if len(bars) < period + 1:
        return float(bars["high"].iloc[-1] - bars["low"].iloc[-1])
    tr = pd.DataFrame({
        "hl": bars["high"] - bars["low"],
        "hc": (bars["high"] - bars["close"].shift(1)).abs(),
        "lc": (bars["low"] - bars["close"].shift(1)).abs(),
    }).max(axis=1)
    return float(tr.tail(period).mean())


class TJRConfluence:
    """Evaluates the 6-step TJR checklist for a single symbol."""

    def __init__(
        self,
        symbol: str,
        sweep_lookback: int = 10,
        bos_lookback: int = 10,
        swing_n: int = 3,
        impulse_body_ratio: float = 0.5,
        fvg_max_age: int = 50,
        ob_max_age: int = 30,
        htf_sma_period: int = 20,
        htf_neutral_band: float = 0.001,
        min_rr: float = 2.0,
        min_pm_range_pct: float = 0.001,
    ):
        self.symbol = symbol
        self.sweep_lookback = sweep_lookback
        self.bos_lookback = bos_lookback
        self.swing_n = swing_n
        self.impulse_body_ratio = impulse_body_ratio
        self.fvg_max_age = fvg_max_age
        self.ob_max_age = ob_max_age
        self.htf_sma_period = htf_sma_period
        self.htf_neutral_band = htf_neutral_band
        self.min_rr = min_rr
        self.min_pm_range_pct = min_pm_range_pct

    def evaluate(
        self,
        bars_5m: pd.DataFrame,
        daily_bars: pd.DataFrame,
        levels: Optional[KeyLevels],
        now: Optional[datetime] = None,
    ) -> Optional[EntrySignal]:
        """Run all 6 checks. Returns EntrySignal or None (with DEBUG log of failure)."""

        # ── Step 1: Session filter ──────────────────────────────────────────
        if not is_tradeable(now):
            logger.debug("[%s] Step 1 FAIL: not in tradeable session (%s)",
                         self.symbol, current_session(now))
            return None
        session = current_session(now)

        # ── Step 2: HTF bias ───────────────────────────────────────────────
        htf_bias = compute_bias(daily_bars, self.htf_sma_period, self.htf_neutral_band)
        if htf_bias == "neutral":
            logger.debug("[%s] Step 2 FAIL: HTF bias neutral", self.symbol)
            return None

        direction = "long" if htf_bias == "bull" else "short"

        # ── Validate pre-market range ──────────────────────────────────────
        if levels is None:
            logger.debug("[%s] FAIL: key levels not yet available", self.symbol)
            return None

        pm_range_pct = (levels.pm_high - levels.pm_low) / max(levels.pm_low, 1e-9)
        if pm_range_pct < self.min_pm_range_pct:
            logger.debug("[%s] FAIL: pre-market range too tight (%.4f%%)",
                         self.symbol, pm_range_pct * 100)
            return None

        # ── Step 3: Liquidity sweep ────────────────────────────────────────
        sweep = scan_all_levels(
            bars_5m,
            {k: v for k, v in levels.all_levels().items() if v > 0},
            self.sweep_lookback,
        )
        if sweep is None or sweep.direction != direction:
            logger.debug("[%s] Step 3 FAIL: no matching sweep (found=%s direction=%s)",
                         self.symbol, sweep.direction if sweep else None, direction)
            return None

        # ── Step 4: Break of Structure ─────────────────────────────────────
        swings = find_swings(bars_5m, self.swing_n)
        relevant_swings = last_n_swings(swings, 10)
        bos = detect_bos(bars_5m, sweep, relevant_swings, self.bos_lookback,
                         self.impulse_body_ratio)
        if bos is None:
            logger.debug("[%s] Step 4 FAIL: no BOS after sweep", self.symbol)
            return None

        # ── Steps 5 & 6: FVG and/or Order Block, price in zone ─────────────
        fvgs = scan_fvgs(bars_5m, self.fvg_max_age)
        current_price = float(bars_5m["close"].iloc[-1])

        fvg_kind = "bullish" if direction == "long" else "bearish"
        active_fvg = nearest_fvg(fvgs, current_price, fvg_kind)
        active_ob = find_order_block(bars_5m, bos, self.ob_max_age)

        price_in_fvg = active_fvg is not None and active_fvg.approaching(current_price)
        price_in_ob = active_ob is not None and active_ob.approaching(current_price)

        if not price_in_fvg and not price_in_ob:
            logger.debug(
                "[%s] Step 5/6 FAIL: price not approaching FVG or OB "
                "(fvg=%s ob=%s price=%.4f)",
                self.symbol,
                f"{active_fvg.midpoint:.4f}" if active_fvg else None,
                f"{active_ob.midpoint:.4f}" if active_ob else None,
                current_price,
            )
            return None

        # ── Compute entry, stop, target ────────────────────────────────────
        atr = _atr(bars_5m)

        if price_in_fvg and active_fvg is not None:
            entry = active_fvg.midpoint
            setup_type = "fvg"
            if price_in_ob and active_ob is not None:
                entry = (active_fvg.midpoint + active_ob.midpoint) / 2
                setup_type = "fvg+ob"
        else:
            assert active_ob is not None
            entry = active_ob.midpoint
            setup_type = "ob"

        if direction == "long":
            stop = entry - 1.0 * atr
            target = levels.pdh if levels.pdh > entry else entry + 2 * atr
            if active_fvg and active_fvg.kind == "bullish":
                stop = min(stop, active_fvg.bottom - 0.5 * atr)
        else:
            stop = entry + 1.0 * atr
            target = levels.pdl if levels.pdl < entry else entry - 2 * atr
            if active_fvg and active_fvg.kind == "bearish":
                stop = max(stop, active_fvg.top + 0.5 * atr)

        risk = abs(entry - stop)
        reward = abs(target - entry)
        rr = reward / max(risk, 1e-9)

        if rr < self.min_rr:
            logger.debug("[%s] RR FAIL: %.2f < %.2f required", self.symbol, rr, self.min_rr)
            return None

        logger.info(
            "[%s] SIGNAL %s | session=%s bias=%s sweep=%s bos=✓ setup=%s "
            "entry=%.4f stop=%.4f target=%.4f rr=%.1f",
            self.symbol, direction.upper(), session, htf_bias,
            sweep.level_name, setup_type, entry, stop, target, rr,
        )

        return EntrySignal(
            symbol=self.symbol,
            direction=direction,
            entry_price=entry,
            stop_price=stop,
            target_price=target,
            rr=rr,
            setup_type=setup_type,
            session=session,
            sweep=sweep,
            bos=bos,
            fvg=active_fvg,
            order_block=active_ob,
            htf_bias=htf_bias,
            timestamp=now or datetime.utcnow(),
        )

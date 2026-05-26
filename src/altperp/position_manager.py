"""
Position manager — owns open positions, applies exits, tracks paper equity, and
enforces the risk circuit breakers (per-coin / concurrent limits, daily and
all-time-high drawdown halts, the "don't add while an old position is underwater"
rule). Persists every open/close to the database and fires alerts via an optional
alerter (telegram.py).
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Dict, Optional

from . import config, database, exits
from .orders import PaperExecutor

logger = logging.getLogger(__name__)


@dataclass
class Position:
    coin: str
    direction: str                 # 'short' | 'long'
    setup_type: str
    entry_price: float
    qty: float                     # original base units
    notional_at_entry: float
    leverage: float
    size_multiplier: float
    opened_at: datetime
    # exit state (read by exits.check_exit)
    remaining_fraction: float = 1.0
    tp1_hit: bool = False
    tp2_hit: bool = False
    tp3_hit: bool = False
    trail_active: bool = False
    trail_anchor: Optional[float] = None
    # context / accounting
    funding_at_entry: float = 0.0
    oi_change_at_entry: float = 0.0
    cvd_confirmed: bool = False
    liq_proximity: bool = False
    tier2_active: bool = False
    db_trade_id: Optional[int] = None
    realized_pnl: float = 0.0      # gross realized PnL (before fees)
    fees_paid: float = 0.0
    last_price: float = 0.0
    atr_at_entry: float = 0.0      # for trend chandelier trailing stop

    def unrealized(self, price: float) -> float:
        live_qty = self.qty * self.remaining_fraction
        if self.direction == "short":
            return (self.entry_price - price) * live_qty
        return (price - self.entry_price) * live_qty


class PositionManager:
    def __init__(self, executor: PaperExecutor, db_path: Optional[str] = None,
                 alerter=None, starting_equity: float = config.PAPER_STARTING_EQUITY):
        self.executor = executor
        self.db_path = db_path or config.DB_PATH
        self.alerter = alerter
        self.equity = starting_equity
        self.ath_equity = starting_equity
        self.day_start_equity = starting_equity
        self.current_day = datetime.now(timezone.utc).date()
        self.halted_until: Optional[datetime] = None
        self.halt_reason: Optional[str] = None
        self.positions: Dict[str, Position] = {}
        database.init_db(self.db_path)

    # ── halt / day roll ──────────────────────────────────────────────────────
    def _roll_day(self, now: datetime):
        if now.date() != self.current_day:
            self.current_day = now.date()
            self.day_start_equity = self.equity

    def is_halted(self, now: Optional[datetime] = None) -> bool:
        now = now or datetime.now(timezone.utc)
        if self.halted_until and now >= self.halted_until:
            self.halted_until = None
            self.halt_reason = None
        return self.halted_until is not None

    # ── entry gating ───────────────────────────────────────────────────────────
    def can_open(self, coin: str, now: Optional[datetime] = None) -> tuple:
        now = now or datetime.now(timezone.utc)
        self._roll_day(now)
        if self.is_halted(now):
            return False, f"halted ({self.halt_reason})"
        if coin in self.positions:
            return False, "position_exists"
        if len(self.positions) >= config.MAX_CONCURRENT_POSITIONS:
            return False, "max_concurrent"
        # don't pile on while an old position is underwater
        for p in self.positions.values():
            age_min = (now - p.opened_at).total_seconds() / 60.0
            if age_min >= config.MAX_POSITION_AGE_MINS and p.last_price and p.unrealized(p.last_price) < 0:
                return False, "existing_position_underwater"
        return True, "ok"

    # ── open ─────────────────────────────────────────────────────────────────
    def open_position(self, setup, size_plan, price: float, now: Optional[datetime] = None) -> Optional[Position]:
        now = now or datetime.now(timezone.utc)
        self.executor.set_isolated_leverage(setup.coin, min(size_plan.leverage_used, config.MAX_LEVERAGE) or config.BASE_LEVERAGE)
        side = "sell" if setup.direction == "short" else "buy"
        fill = self.executor.execute(setup.coin, side, size_plan.qty, price)

        ctx = setup.context
        pos = Position(
            coin=setup.coin, direction=setup.direction, setup_type=setup.setup_type,
            entry_price=fill.price, qty=fill.qty, notional_at_entry=fill.notional,
            leverage=size_plan.leverage_used, size_multiplier=setup.size_multiplier,
            opened_at=now, last_price=fill.price,
            funding_at_entry=ctx.get("funding", {}).get("funding_rate", 0.0),
            oi_change_at_entry=ctx.get("oi", {}).get("oi_4hr_change") or 0.0,
            cvd_confirmed=setup.cvd_confirmed, liq_proximity=setup.liq_proximity,
            tier2_active=setup.tier2_score > 0,
        )
        # Trend positions trail from entry (chandelier) and need ATR for the stop.
        if pos.setup_type.startswith("trend"):
            pos.trail_active = True
            pos.trail_anchor = fill.price
            pos.atr_at_entry = getattr(setup, "atr", 0.0) or 0.0

        self.equity -= fill.fee
        pos.fees_paid += fill.fee

        pos.db_trade_id = database.open_trade({
            "coin": pos.coin, "direction": pos.direction, "setup_type": pos.setup_type,
            "entry_price": pos.entry_price, "position_size_usdt": pos.notional_at_entry,
            "leverage": round(pos.leverage), "tier2_active": int(pos.tier2_active),
            "funding_at_entry": pos.funding_at_entry, "oi_change_at_entry": pos.oi_change_at_entry,
            "cvd_confirmed": int(pos.cvd_confirmed), "liq_proximity": int(pos.liq_proximity),
        }, db_path=self.db_path)

        self.positions[pos.coin] = pos
        logger.info("[ALTPERP] OPEN %s %s @ %.4f size=$%.2f mult=%.2fx lev=%.2fx",
                    pos.coin, pos.direction.upper(), pos.entry_price, pos.notional_at_entry,
                    pos.size_multiplier, pos.leverage)
        if self.alerter:
            self.alerter.trade_opened(pos, setup)
        return pos

    # ── per-tick exit processing ───────────────────────────────────────────────
    def on_tick(self, coin: str, price: float, funding_rate: Optional[float],
                oi_4hr_change: Optional[float], now: Optional[datetime] = None):
        now = now or datetime.now(timezone.utc)
        pos = self.positions.get(coin)
        if not pos:
            return
        pos.last_price = price
        action = exits.check_exit(pos, price, funding_rate, oi_4hr_change, now)
        if action:
            self._apply_exit(pos, action, price, now)

    def _apply_exit(self, pos: Position, action, price: float, now: datetime):
        frac = min(action.fraction, pos.remaining_fraction)
        if frac <= 0:
            return
        close_qty = pos.qty * frac
        side = "buy" if pos.direction == "short" else "sell"
        fill = self.executor.execute(pos.coin, side, close_qty, price)

        if pos.direction == "short":
            pnl = (pos.entry_price - fill.price) * close_qty
        else:
            pnl = (fill.price - pos.entry_price) * close_qty
        self.equity += pnl - fill.fee
        pos.realized_pnl += pnl
        pos.fees_paid += fill.fee
        pos.remaining_fraction = round(pos.remaining_fraction - frac, 8)

        # flags
        if action.reason == "TP1":
            pos.tp1_hit = True
            pos.trail_active = True
        elif action.reason == "TP2":
            pos.tp2_hit = True
        elif action.reason == "TP3":
            pos.tp3_hit = True

        logger.info("[ALTPERP] %s %s %s frac=%.2f @ %.4f pnl=$%+.4f rem=%.2f",
                    pos.coin, action.reason, action.kind, frac, fill.price, pnl, pos.remaining_fraction)

        if action.kind == "full" or pos.remaining_fraction <= 1e-6:
            self._finalize(pos, action.reason, fill.price, now)

    def _finalize(self, pos: Position, reason: str, exit_price: float, now: datetime):
        net = pos.realized_pnl - pos.fees_paid
        pnl_pct = (net / pos.notional_at_entry * 100) if pos.notional_at_entry else 0.0
        database.close_trade(pos.db_trade_id, {
            "exit_price": exit_price, "exit_reason": reason,
            "tp1_hit": int(pos.tp1_hit), "tp2_hit": int(pos.tp2_hit), "tp3_hit": int(pos.tp3_hit),
            "pnl_usdt": round(pos.realized_pnl, 4), "pnl_pct": round(pnl_pct, 4),
            "fees_usdt": round(pos.fees_paid, 4), "net_pnl_usdt": round(net, 4),
        }, db_path=self.db_path)
        logger.info("[ALTPERP] CLOSE %s %s net=$%+.4f (%.2f%%) equity=$%.2f",
                    pos.coin, reason, net, pnl_pct, self.equity)
        if self.alerter:
            self.alerter.trade_closed(pos, reason, net, pnl_pct, self.equity)
        self.positions.pop(pos.coin, None)
        self._check_circuit_breakers(now)

    # ── circuit breakers ───────────────────────────────────────────────────────
    def _check_circuit_breakers(self, now: datetime):
        self.ath_equity = max(self.ath_equity, self.equity)
        # daily drawdown
        if self.equity <= self.day_start_equity * (1 - config.DAILY_DRAWDOWN_HALT_PCT):
            self._halt(now + timedelta(hours=24), "daily_drawdown_5pct")
        # all-time-high drawdown (more serious — longer halt + alert)
        if self.equity <= self.ath_equity * (1 - config.MAX_DRAWDOWN_HALT_PCT):
            self._halt(now + timedelta(hours=72), "max_drawdown_10pct")

    def _halt(self, until: datetime, reason: str):
        # keep the most severe (latest) halt
        if not self.halted_until or until > self.halted_until:
            self.halted_until = until
            self.halt_reason = reason
            logger.warning("[ALTPERP] CIRCUIT BREAKER: %s — halted until %s", reason, until.isoformat())
            if self.alerter:
                self.alerter.circuit_breaker(reason, self.equity, until)

    def summary(self) -> dict:
        return {
            "equity": round(self.equity, 2),
            "ath_equity": round(self.ath_equity, 2),
            "open_positions": len(self.positions),
            "halted": self.is_halted(),
            "halt_reason": self.halt_reason,
        }

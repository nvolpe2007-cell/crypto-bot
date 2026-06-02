"""Tracks open positions and manages SL/TP/trail/EOD close."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional

from .broker import AlpacaBroker
from .risk import DailyCircuit
from ..utils.journal import TradeJournal, TradeRecord
from ..utils.notifications import Notifier

logger = logging.getLogger(__name__)


@dataclass
class OpenPosition:
    symbol: str
    side: str            # "long" | "short"
    entry_price: float
    qty: float
    stop_price: float
    target_price: float
    setup_type: str
    session: str
    sweep_level: str
    order_id: str
    entry_time: datetime
    breakeven_moved: bool = False

    @property
    def rr_at_target(self) -> float:
        reward = abs(self.target_price - self.entry_price)
        risk = abs(self.entry_price - self.stop_price)
        return reward / max(risk, 1e-9)


class PositionManager:
    """Monitors all open positions; call `update(symbol, current_price)` each tick."""

    def __init__(
        self,
        broker: AlpacaBroker,
        circuit: DailyCircuit,
        journal: TradeJournal,
        notifier: Notifier,
    ):
        self._broker = broker
        self._circuit = circuit
        self._journal = journal
        self._notifier = notifier
        self._positions: Dict[str, OpenPosition] = {}

    def add(self, pos: OpenPosition) -> None:
        self._positions[pos.symbol] = pos
        self._circuit.on_entry()
        logger.info(
            "Position opened: %s %s qty=%.4f entry=%.4f stop=%.4f target=%.4f",
            pos.symbol, pos.side, pos.qty,
            pos.entry_price, pos.stop_price, pos.target_price,
        )

    def remove(self, symbol: str) -> None:
        if symbol in self._positions:
            del self._positions[symbol]

    def has_position(self, symbol: str) -> bool:
        return symbol in self._positions

    @property
    def symbols_with_positions(self) -> List[str]:
        return list(self._positions.keys())

    def update(self, symbol: str, current_price: float) -> None:
        """Evaluate SL/TP/trail for symbol; close if triggered."""
        pos = self._positions.get(symbol)
        if pos is None:
            return

        # Move stop to breakeven once price reaches 50% of the way to target
        if not pos.breakeven_moved:
            halfway = pos.entry_price + 0.5 * (pos.target_price - pos.entry_price)
            if (pos.side == "long" and current_price >= halfway) or \
               (pos.side == "short" and current_price <= halfway):
                pos.stop_price = pos.entry_price
                pos.breakeven_moved = True
                logger.info("[%s] Stop moved to breakeven @ %.4f", symbol, pos.entry_price)

        # Check stop-loss
        if pos.side == "long" and current_price <= pos.stop_price:
            self._close(pos, current_price, reason="sl")
            return

        if pos.side == "short" and current_price >= pos.stop_price:
            self._close(pos, current_price, reason="sl")
            return

        # Check take-profit
        if pos.side == "long" and current_price >= pos.target_price:
            self._close(pos, current_price, reason="tp")
            return

        if pos.side == "short" and current_price <= pos.target_price:
            self._close(pos, current_price, reason="tp")
            return

    def close_all(self, reason: str = "eod") -> None:
        """Force-close all positions (used at EOD or circuit halt)."""
        for symbol in list(self._positions.keys()):
            pos = self._positions[symbol]
            try:
                alpaca_pos = next(
                    (p for p in self._broker.get_open_positions()
                     if p.symbol == symbol), None
                )
                if alpaca_pos:
                    current_price = float(alpaca_pos.current_price)
                else:
                    current_price = pos.entry_price  # fallback
                self._close(pos, current_price, reason=reason)
            except Exception as exc:
                logger.error("Force close %s failed: %s", symbol, exc)

    def _close(self, pos: OpenPosition, exit_price: float, reason: str) -> None:
        symbol = pos.symbol
        try:
            self._broker.close_position(symbol)
        except Exception as exc:
            logger.error("Broker close %s failed: %s", symbol, exc)

        direction = 1 if pos.side == "long" else -1
        pnl = direction * (exit_price - pos.entry_price) * pos.qty
        pnl_pct = pnl / (pos.entry_price * pos.qty) * 100

        was_stop = reason == "sl"
        self._circuit.on_exit(was_stop=was_stop)

        record = TradeRecord(
            trade_id=pos.order_id[:8],
            symbol=symbol,
            side=pos.side,
            entry_time=pos.entry_time.isoformat(),
            exit_time=datetime.utcnow().isoformat(),
            entry_price=pos.entry_price,
            exit_price=exit_price,
            qty=pos.qty,
            pnl=round(pnl, 4),
            pnl_pct=round(pnl_pct, 2),
            stop_price=pos.stop_price,
            target_price=pos.target_price,
            exit_reason=reason,
            setup_type=pos.setup_type,
            session=pos.session,
            sweep_level=pos.sweep_level,
            rr_achieved=round(abs(exit_price - pos.entry_price) /
                               max(abs(pos.entry_price - pos.stop_price), 1e-9), 2),
        )
        self._journal.record(record)
        self._notifier.exit(symbol, pos.side, pos.entry_price, exit_price, reason, pnl)

        self.remove(symbol)
        logger.info(
            "Position closed: %s %s exit=%.4f reason=%s pnl=%+.4f",
            symbol, pos.side, exit_price, reason, pnl,
        )

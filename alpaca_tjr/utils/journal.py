"""Trade journal — appends to CSV, provides daily stats."""
from __future__ import annotations

import csv
import logging
import os
from dataclasses import dataclass, asdict
from datetime import datetime, date
from typing import List, Optional

logger = logging.getLogger(__name__)

FIELDS = [
    "trade_id", "symbol", "side", "entry_time", "exit_time",
    "entry_price", "exit_price", "qty", "pnl", "pnl_pct",
    "stop_price", "target_price", "exit_reason",
    "setup_type", "session", "sweep_level", "rr_achieved",
]


@dataclass
class TradeRecord:
    trade_id: str
    symbol: str
    side: str          # long | short
    entry_time: str
    exit_time: str
    entry_price: float
    exit_price: float
    qty: float
    pnl: float
    pnl_pct: float
    stop_price: float
    target_price: float
    exit_reason: str   # tp | sl | trail | eod | manual
    setup_type: str    # fvg | ob | fvg+ob
    session: str       # primary | secondary
    sweep_level: str   # pm_high | pm_low | pdh | pdl | swing
    rr_achieved: float


class TradeJournal:
    def __init__(self, path: str = "data/alpaca_tjr_journal.csv"):
        self._path = path
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self._ensure_header()
        self._today_trades: List[TradeRecord] = []

    def _ensure_header(self) -> None:
        if not os.path.exists(self._path):
            with open(self._path, "w", newline="") as f:
                csv.DictWriter(f, fieldnames=FIELDS).writeheader()

    def record(self, trade: TradeRecord) -> None:
        self._today_trades.append(trade)
        with open(self._path, "a", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=FIELDS)
            writer.writerow(asdict(trade))
        logger.info(
            "TRADE %s %s %s entry=%.4f exit=%.4f pnl=%+.2f reason=%s",
            trade.trade_id, trade.symbol, trade.side,
            trade.entry_price, trade.exit_price, trade.pnl, trade.exit_reason,
        )

    def daily_stats(self) -> dict:
        trades = self._today_trades
        if not trades:
            return {"trades": 0, "wins": 0, "losses": 0, "pnl": 0.0, "win_rate": 0.0}
        wins = sum(1 for t in trades if t.pnl > 0)
        pnl = sum(t.pnl for t in trades)
        return {
            "trades": len(trades),
            "wins": wins,
            "losses": len(trades) - wins,
            "pnl": pnl,
            "win_rate": wins / len(trades) * 100,
        }

    def reset_daily(self) -> None:
        self._today_trades = []

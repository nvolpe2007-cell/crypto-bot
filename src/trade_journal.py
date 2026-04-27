"""
Trade Journal — records every trade with full entry conditions.
The learner reads this to avoid repeating losing setups.
"""

import json
import os
from datetime import datetime, timezone
from typing import List, Dict, Optional
from dataclasses import dataclass, asdict

JOURNAL_FILE = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'data', 'trade_journal.json')


@dataclass
class TradeRecord:
    # Identity
    trade_id:      str
    symbol:        str
    opened_at:     str
    closed_at:     str

    # Entry conditions — these are what the learner analyses
    rsi:           float
    adx:           float
    volume_ratio:  float
    regime:        str       # TRENDING | RANGING | NEUTRAL | BEAR
    atr_pct:       float     # ATR as % of price — volatility level
    ema100_gap:    float     # % distance of price from EMA100
    ema200_gap:    float     # % distance of price from EMA200
    hour_utc:      int       # 0-23, crypto has session patterns
    day_of_week:   int       # 0=Mon, 6=Sun

    # Outcome
    pnl:           float
    pnl_pct:       float
    won:           bool
    reason:        str       # SIGNAL | STOP_LOSS | TAKE_PROFIT

    def to_dict(self):
        return asdict(self)

    def features(self) -> Dict[str, float]:
        """Numeric features used for similarity comparison."""
        return {
            'rsi':          self.rsi,
            'adx':          self.adx,
            'volume_ratio': self.volume_ratio,
            'atr_pct':      self.atr_pct,
            'ema100_gap':   self.ema100_gap,
            'ema200_gap':   self.ema200_gap,
            'hour_utc':     float(self.hour_utc),
            'day_of_week':  float(self.day_of_week),
        }


class TradeJournal:
    def __init__(self):
        self.records: List[TradeRecord] = []
        self._load()

    def _load(self):
        try:
            os.makedirs(os.path.dirname(JOURNAL_FILE), exist_ok=True)
            with open(JOURNAL_FILE) as f:
                raw = json.load(f)
            self.records = [TradeRecord(**r) for r in raw]
        except Exception:
            self.records = []

    def save(self):
        os.makedirs(os.path.dirname(JOURNAL_FILE), exist_ok=True)
        with open(JOURNAL_FILE, 'w') as f:
            json.dump([r.to_dict() for r in self.records], f, indent=2)

    def add(self, record: TradeRecord):
        self.records.append(record)
        self.save()

    def losses(self) -> List[TradeRecord]:
        return [r for r in self.records if not r.won]

    def wins(self) -> List[TradeRecord]:
        return [r for r in self.records if r.won]

    def stats(self) -> dict:
        total = len(self.records)
        if total == 0:
            return {'total': 0}
        wins  = len(self.wins())
        return {
            'total':    total,
            'wins':     wins,
            'losses':   total - wins,
            'win_rate': round(wins / total * 100, 1),
        }

    def build_record(self, trade, symbol: str, reason: str,
                     signal) -> TradeRecord:
        """Build a TradeRecord from a closed trade + the signal that triggered entry."""
        now = datetime.now(timezone.utc)
        price = signal.close if signal else 1.0
        return TradeRecord(
            trade_id    = f"{symbol}_{int(now.timestamp())}",
            symbol      = symbol,
            opened_at   = trade.entry_time.isoformat() if hasattr(trade.entry_time, 'isoformat') else str(trade.entry_time),
            closed_at   = trade.exit_time.isoformat()  if hasattr(trade.exit_time,  'isoformat') else str(trade.exit_time),
            rsi          = signal.rsi          if signal else 50.0,
            adx          = signal.adx          if signal else 20.0,
            volume_ratio = signal.volume_ratio if signal and hasattr(signal, 'volume_ratio') else 1.0,
            regime       = signal.regime       if signal and hasattr(signal, 'regime') else 'UNKNOWN',
            atr_pct      = (signal.atr / price * 100) if signal and signal.atr else 1.0,
            ema100_gap   = ((price - signal.ema100) / signal.ema100 * 100) if signal and hasattr(signal, 'ema100') else 0.0,
            ema200_gap   = ((price - signal.ema200) / signal.ema200 * 100) if signal and hasattr(signal, 'ema200') else 0.0,
            hour_utc     = now.hour,
            day_of_week  = now.weekday(),
            pnl          = round(trade.pnl, 4),
            pnl_pct      = round(trade.pnl_pct, 2),
            won          = trade.pnl > 0,
            reason       = reason,
        )

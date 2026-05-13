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

    # Extended ML features (optional — default 0 for backward-compat with old JSON)
    ofi:               float = 0.0
    lead_lag_strength: float = 0.0
    lead_lag_aligned:  bool  = False
    confidence:        float = 0.0
    ofi_score:         float = 0.0
    lead_lag_score:    float = 0.0
    regime_score:      float = 0.0
    regime_confidence: float = 0.5
    funding_rate:      float = 0.0
    direction:         str   = 'buy'

    # Post-mortem fields — recorded at exit so the ML/learner can diagnose *why* a trade lost,
    # not just whether the entry conditions looked good. Optional with safe defaults.
    mfe_pct:            float = 0.0   # max favorable excursion (best % move in our favor)
    mae_pct:            float = 0.0   # max adverse excursion (worst % move against us)
    time_in_trade_sec:  float = 0.0
    regime_at_exit:     str   = ''
    rsi_at_exit:        float = 0.0
    adx_at_exit:        float = 0.0
    exit_price:         float = 0.0

    def to_dict(self):
        return asdict(self)

    def features(self) -> Dict[str, float]:
        """Numeric features used for similarity comparison and ML training."""
        return {
            'rsi':               self.rsi,
            'adx':               self.adx,
            'volume_ratio':      self.volume_ratio,
            'atr_pct':           self.atr_pct,
            'ema100_gap':        self.ema100_gap,
            'ema200_gap':        self.ema200_gap,
            'hour_utc':          float(self.hour_utc),
            'day_of_week':       float(self.day_of_week),
            'ofi':               self.ofi,
            'lead_lag_strength': self.lead_lag_strength,
            'lead_lag_aligned':  float(self.lead_lag_aligned),
            'regime_confidence': self.regime_confidence,
            'funding_rate':      self.funding_rate,
            'ofi_score':         self.ofi_score,
            'lead_lag_score':    self.lead_lag_score,
            'regime_score':      self.regime_score,
            'confidence':        self.confidence,
            'is_buy':            float(self.direction == 'buy'),
        }


class TradeJournal:
    def __init__(self):
        self.records: List[TradeRecord] = []
        self._load()

    # Fields added after initial release — provide defaults for old records
    _DEFAULTS = {
        'ofi': 0.0, 'lead_lag_strength': 0.0, 'lead_lag_aligned': False,
        'confidence': 0.0, 'ofi_score': 0.0, 'lead_lag_score': 0.0,
        'regime_score': 0.0, 'regime_confidence': 0.5,
        'funding_rate': 0.0, 'direction': 'buy',
        'mfe_pct': 0.0, 'mae_pct': 0.0, 'time_in_trade_sec': 0.0,
        'regime_at_exit': '', 'rsi_at_exit': 0.0, 'adx_at_exit': 0.0,
        'exit_price': 0.0,
    }

    def _load(self):
        try:
            os.makedirs(os.path.dirname(JOURNAL_FILE), exist_ok=True)
            with open(JOURNAL_FILE) as f:
                raw = json.load(f)
            self.records = [TradeRecord(**{**self._DEFAULTS, **r}) for r in raw]
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

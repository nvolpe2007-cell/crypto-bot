"""
Trade Journal — records every trade with full entry conditions.
The learner reads this to avoid repeating losing setups.
"""

import csv
import json
import os
from datetime import datetime, timezone
from typing import List, Dict, Optional
from dataclasses import dataclass, asdict, fields

JOURNAL_FILE = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'data', 'trade_journal.json')
CSV_FILE     = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'data', 'trade_journal.csv')


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

    # Entry pathway and pricing context
    entry_path:         str   = 'main'   # main | mr | mr-extreme | fast-track
    entry_price:        float = 0.0
    position_size_usd:  float = 0.0
    stop_loss_price:    float = 0.0
    take_profit_price:  float = 0.0
    fees_paid:          float = 0.0      # total fees in/out
    slippage_cost:      float = 0.0      # estimated slippage cost ($)
    spread_at_entry:    float = 0.0      # bid-ask spread at entry

    # Complete sub-score breakdown (was only partially saved)
    rsi_score:          float = 0.0
    technical_score:    float = 0.0
    funding_score:      float = 0.0

    # Indicator snapshot at entry
    ema_fast:           float = 0.0
    ema_slow:           float = 0.0
    atr_at_entry:       float = 0.0

    # Market context
    sentiment_fng:      Optional[int]   = None   # Fear & Greed 0-100
    sentiment_btc_dom:  Optional[float] = None   # BTC dominance %

    # Realized return / risk metrics
    r_multiple:         float = 0.0   # pnl / planned_risk
    fees_pct_of_pnl:    float = 0.0   # fee drag

    # Probability gate output (stacked-edge reasoning)
    prob_win:           float = 0.0   # P(win) at entry
    edges_used:         str   = ''    # comma-joined edge names that were present
    prob_model_version: int   = 0     # combiner version (see probability_gate.PROB_MODEL_VERSION);
                                       # 0 = legacy noisy-OR records, excluded from calibration

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
        # New extended tracking fields
        'entry_path': 'main', 'entry_price': 0.0, 'position_size_usd': 0.0,
        'stop_loss_price': 0.0, 'take_profit_price': 0.0,
        'fees_paid': 0.0, 'slippage_cost': 0.0, 'spread_at_entry': 0.0,
        'rsi_score': 0.0, 'technical_score': 0.0, 'funding_score': 0.0,
        'ema_fast': 0.0, 'ema_slow': 0.0, 'atr_at_entry': 0.0,
        'sentiment_fng': None, 'sentiment_btc_dom': None,
        'r_multiple': 0.0, 'fees_pct_of_pnl': 0.0,
        'prob_win': 0.0, 'edges_used': '', 'prob_model_version': 0,
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
        # Atomic write: a crash mid-write would otherwise truncate the journal
        # and lose all trade history. Write to a tmp file then os.replace.
        os.makedirs(os.path.dirname(JOURNAL_FILE), exist_ok=True)
        tmp = JOURNAL_FILE + '.tmp'
        with open(tmp, 'w') as f:
            json.dump([r.to_dict() for r in self.records], f, indent=2)
        os.replace(tmp, JOURNAL_FILE)

    def add(self, record: TradeRecord):
        self.records.append(record)
        self.save()
        self.append_csv(record)

    def append_csv(self, record: TradeRecord):
        """Append one trade row to the CSV file. Writes header if file is new."""
        try:
            os.makedirs(os.path.dirname(CSV_FILE), exist_ok=True)
            header_needed = not os.path.exists(CSV_FILE) or os.path.getsize(CSV_FILE) == 0
            fieldnames = [f.name for f in fields(TradeRecord)]
            with open(CSV_FILE, 'a', newline='', encoding='utf-8') as f:
                writer = csv.DictWriter(f, fieldnames=fieldnames)
                if header_needed:
                    writer.writeheader()
                writer.writerow(record.to_dict())
        except Exception:
            pass   # don't let CSV failure break trade flow

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

    def _group_stats(self, key_fn) -> dict:
        """Group records by a key and compute win rate + avg PnL per group."""
        groups: Dict[str, List[TradeRecord]] = {}
        for r in self.records:
            k = str(key_fn(r))
            groups.setdefault(k, []).append(r)
        out = {}
        for k, recs in groups.items():
            n = len(recs)
            w = sum(1 for r in recs if r.won)
            pnl = sum(r.pnl for r in recs)
            out[k] = {
                'n':        n,
                'win_rate': round(w / n * 100, 1),
                'avg_pnl':  round(pnl / n, 4),
                'total_pnl': round(pnl, 4),
            }
        return out

    def detailed_stats(self) -> dict:
        """Rich breakdown: by entry_path, regime, hour, day, symbol."""
        if not self.records:
            return {'total': 0}

        wins = self.wins()
        losses = self.losses()

        avg_mfe_win  = (sum(r.mfe_pct for r in wins)   / len(wins))   if wins   else 0.0
        avg_mae_win  = (sum(r.mae_pct for r in wins)   / len(wins))   if wins   else 0.0
        avg_mfe_loss = (sum(r.mfe_pct for r in losses) / len(losses)) if losses else 0.0
        avg_mae_loss = (sum(r.mae_pct for r in losses) / len(losses)) if losses else 0.0

        total_pnl    = sum(r.pnl for r in self.records)
        total_fees   = sum(r.fees_paid for r in self.records)
        avg_hold_sec = sum(r.time_in_trade_sec for r in self.records) / len(self.records)
        avg_conf_win = (sum(r.confidence for r in wins) / len(wins)) if wins else 0.0
        avg_conf_los = (sum(r.confidence for r in losses) / len(losses)) if losses else 0.0

        return {
            'overall':       self.stats(),
            'total_pnl':     round(total_pnl, 4),
            'total_fees':    round(total_fees, 4),
            'fee_drag_pct':  round((total_fees / abs(total_pnl) * 100), 1) if total_pnl else 0.0,
            'avg_hold_sec':  round(avg_hold_sec, 1),
            'avg_mfe_winners': round(avg_mfe_win,  3),
            'avg_mae_winners': round(avg_mae_win,  3),
            'avg_mfe_losers':  round(avg_mfe_loss, 3),
            'avg_mae_losers':  round(avg_mae_loss, 3),
            'avg_conf_winners': round(avg_conf_win, 1),
            'avg_conf_losers':  round(avg_conf_los, 1),
            'by_entry_path': self._group_stats(lambda r: r.entry_path),
            'by_regime':     self._group_stats(lambda r: r.regime),
            'by_symbol':     self._group_stats(lambda r: r.symbol),
            'by_hour':       self._group_stats(lambda r: r.hour_utc),
            'by_day':        self._group_stats(lambda r: r.day_of_week),
            'by_direction':  self._group_stats(lambda r: r.direction),
        }

    def print_report(self):
        """Print a human-readable summary report."""
        s = self.detailed_stats()
        if s.get('total', 0) == 0 and not s.get('overall'):
            print("No trades recorded yet.")
            return
        o = s['overall']
        print("\n" + "=" * 60)
        print(f"TRADE JOURNAL REPORT ({o['total']} trades)")
        print("=" * 60)
        print(f"Win rate: {o['win_rate']}%  ({o['wins']}W / {o['losses']}L)")
        print(f"Total PnL: ${s['total_pnl']:+.2f}  |  Fees: ${s['total_fees']:.2f}  |  Fee drag: {s['fee_drag_pct']}%")
        print(f"Avg hold: {s['avg_hold_sec']:.0f}s  |  Avg conf win/loss: {s['avg_conf_winners']:.0f}/{s['avg_conf_losers']:.0f}")
        print(f"Avg MFE winners {s['avg_mfe_winners']:.2f}% / losers {s['avg_mfe_losers']:.2f}%")
        print(f"Avg MAE winners {s['avg_mae_winners']:.2f}% / losers {s['avg_mae_losers']:.2f}%")

        def _print_section(title, data):
            print(f"\n— {title} —")
            for k, v in sorted(data.items(), key=lambda kv: -kv[1]['n']):
                print(f"  {k:<18} n={v['n']:<3} WR={v['win_rate']:>5}%  PnL=${v['total_pnl']:+.2f}")

        _print_section('By entry path', s['by_entry_path'])
        _print_section('By regime',     s['by_regime'])
        _print_section('By symbol',     s['by_symbol'])
        _print_section('By direction',  s['by_direction'])
        _print_section('By hour UTC',   s['by_hour'])
        print("=" * 60)

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

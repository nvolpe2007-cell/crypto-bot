""" 
Paper Trading Engine
Uses ScientificStrategy (OFI + BTC Lead-Lag primary) with confidence-scaled sizing.
All other strategies (EMA, BB, regime, funding) contribute to the confidence score.

Tick-driven: evaluates every 2 seconds per symbol using live WebSocket price
injected into a cached OHLCV DataFrame.  REST API only called on candle close
(once per minute) — eliminates rate-limit pressure and gives near-real-time signals.
"""

import asyncio
import json
import os
import time
from collections import deque
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Deque
from dataclasses import dataclass, field
import logging

import pandas as pd
import pandas_ta as _pta

from .indicators import Signal, prepare_ohlcv_dataframe
from .scientific_strategy import ScientificStrategy, ScientificSignal, compute_position_size, _size_multiplier as _get_size_mult
from .microstructure_strategy import (
    MicrostructureStrategy, MicrostructureSignal,
    _ENTRY_GRACE_SECS as _MICRO_ENTRY_GRACE_SECS,
)
from .exchange import ExchangeConnection, CircuitBreakerOpen
from .backtester import Trade
from .notifications import TelegramNotifier
from .market_sentiment import SentimentMonitor
from .kraken_ws import KrakenPublicWS
from .regime_detector import RegimeDetector
from .portfolio_optimizer import PortfolioOptimizer
from .crypto_vol import CryptoVolMonitor
from .order_flow import OrderFlowImbalance
from .wick_analyzer import detect_rejection, detect_stop_hunt
from .lead_lag_detector import LeadLagDetector
from .trade_journal import TradeJournal, TradeRecord
from .learner import Learner
from .state import write_state, read_state
from .ml_scorer import MLScorer
from .multi_timeframe import MultiTimeframeFilter
from .mean_reversion_strategy import MeanReversionStrategy, MRSignal
from .probability_gate import ProbabilityGate, ENABLED as PROB_GATE_ENABLED, PROB_MODEL_VERSION
from .expectancy_gate import ExpectancyGate
from .macro_data import MacroDataProvider, alt_beta
from .daily_circuit import DailyCircuitBreaker
from .trailing_stop import update_trailing_stop
from .entry_checklist import (
    CheckContext,
    SpreadTracker,
    build_long_checklist,
    build_short_checklist,
)
from .task_supervisor import supervised, get_health as _get_subsystem_health

logger = logging.getLogger(__name__)

# (PaperTrader + paper fill/PnL model extracted to paper_trader.py)

# ── Funding rate helper ────────────────────────────────────────────────────────
_SYMBOL_TO_FUNDING = {
    'BTC/USD': 'BTCUSDT',
    'ETH/USD': 'ETHUSDT',
    'SOL/USD': 'SOLUSDT',
}

def _get_funding_rate(symbol: str) -> Optional[float]:
    try:
        state = read_state()
        opps  = state.get('funding_opportunities', [])
        usdt  = _SYMBOL_TO_FUNDING.get(symbol, '')
        for o in opps:
            if o.get('symbol') == usdt:
                return o.get('rate_8h', 0) / 100
    except Exception as e:
        logger.debug(f"[FUNDING] state read failed for {symbol}: {e}")
    return None
"""
Loss Pattern Learner
Compares current trade conditions against past losses.
If the setup looks too similar to past losing trades, it raises the
confidence threshold — making the bot more selective.
"""

import math
import logging
from typing import Dict, Optional
from .trade_journal import TradeJournal, TradeRecord

logger = logging.getLogger(__name__)

# Weights: how much each feature matters for similarity
FEATURE_WEIGHTS = {
    'rsi':          2.0,   # RSI level matters a lot
    'adx':          1.5,   # trend strength
    'volume_ratio': 1.0,
    'atr_pct':      1.0,   # volatility regime
    'ema100_gap':   1.5,   # position relative to trend
    'ema200_gap':   1.5,
    'hour_utc':     0.5,   # time of day matters less
    'day_of_week':  0.3,
}

# Feature scale (for normalisation — prevents one big-range feature dominating)
FEATURE_SCALE = {
    'rsi':          30.0,
    'adx':          20.0,
    'volume_ratio':  1.0,
    'atr_pct':       1.5,
    'ema100_gap':    5.0,
    'ema200_gap':    8.0,
    'hour_utc':     12.0,
    'day_of_week':   3.5,
}

# Thresholds
BASE_CONFIDENCE    = 75     # normal minimum confidence
MAX_CONFIDENCE     = 92     # ceiling when conditions look very bad
MIN_TRADES_TO_LEARN = 5     # don't adjust until we have enough data
SIMILARITY_DANGER  = 0.80   # similarity score above which we flag as risky


def _distance(a: Dict[str, float], b: Dict[str, float]) -> float:
    """Weighted normalised Euclidean distance between two feature dicts."""
    total = 0.0
    for key, weight in FEATURE_WEIGHTS.items():
        scale = FEATURE_SCALE.get(key, 1.0)
        diff  = (a.get(key, 0) - b.get(key, 0)) / scale
        total += weight * diff * diff
    return math.sqrt(total)


def _similarity(dist: float) -> float:
    """Convert distance to 0-1 similarity (1 = identical)."""
    return math.exp(-dist)


class Learner:
    """
    Analyses past losses and adjusts the confidence threshold for new trades.

    Logic:
    - Find the K most similar past losses to the current setup (K-NN style)
    - The more similar and the more losses, the higher the required confidence
    - Also tracks regime-level win rates to avoid weak regimes
    """

    def __init__(self, journal: TradeJournal, k: int = 5):
        self.journal = journal
        self.k = k

    def required_confidence(self, current_features: Dict[str, float],
                             regime: str, symbol: str) -> int:
        """
        Returns the minimum confidence score required for this trade.
        Higher = harder to enter = more selective.
        """
        losses = self.journal.losses()
        wins   = self.journal.wins()
        total  = len(self.journal.records)

        if total < MIN_TRADES_TO_LEARN:
            return BASE_CONFIDENCE

        # ── 1. Regime-level win rate ──────────────────────────────────────────
        regime_trades = [r for r in self.journal.records if r.regime == regime]
        if len(regime_trades) >= 3:
            regime_wr = sum(1 for r in regime_trades if r.won) / len(regime_trades)
            if regime_wr < 0.35:
                # This regime loses 65%+ of the time — raise bar significantly
                logger.info(f"[LEARNER] Regime {regime} win rate {regime_wr:.0%} — raising threshold")
                return min(MAX_CONFIDENCE, BASE_CONFIDENCE + 12)
            elif regime_wr < 0.45:
                return min(MAX_CONFIDENCE, BASE_CONFIDENCE + 6)

        # ── 2. Symbol-level win rate ──────────────────────────────────────────
        sym_trades = [r for r in self.journal.records if r.symbol == symbol]
        if len(sym_trades) >= 3:
            sym_wr = sum(1 for r in sym_trades if r.won) / len(sym_trades)
            if sym_wr < 0.35:
                logger.info(f"[LEARNER] {symbol} win rate {sym_wr:.0%} — raising threshold")
                return min(MAX_CONFIDENCE, BASE_CONFIDENCE + 8)

        if not losses:
            return BASE_CONFIDENCE

        # ── 3. K-NN similarity to past losses ────────────────────────────────
        similarities = []
        for loss in losses:
            dist = _distance(current_features, loss.features())
            sim  = _similarity(dist)
            similarities.append(sim)

        similarities.sort(reverse=True)
        top_k = similarities[:self.k]

        # Weighted average of top-K similarities
        avg_sim = sum(top_k) / len(top_k)

        if avg_sim >= SIMILARITY_DANGER:
            # Very similar to past losses — make it hard to enter
            extra = int((avg_sim - SIMILARITY_DANGER) / (1.0 - SIMILARITY_DANGER) * (MAX_CONFIDENCE - BASE_CONFIDENCE))
            threshold = min(MAX_CONFIDENCE, BASE_CONFIDENCE + extra)
            logger.info(f"[LEARNER] {symbol} setup {avg_sim:.0%} similar to past losses — threshold → {threshold}")
            return threshold

        return BASE_CONFIDENCE

    def log_summary(self):
        stats = self.journal.stats()
        if stats['total'] == 0:
            logger.info("[LEARNER] No trades recorded yet.")
            return

        logger.info(f"[LEARNER] Journal: {stats['total']} trades | "
                    f"WR={stats['win_rate']}% | "
                    f"Wins={stats['wins']} Losses={stats['losses']}")

        # Log worst regimes
        for regime in ['TRENDING', 'RANGING', 'NEUTRAL', 'BEAR']:
            rt = [r for r in self.journal.records if r.regime == regime]
            if len(rt) >= 2:
                wr = sum(1 for r in rt if r.won) / len(rt) * 100
                logger.info(f"[LEARNER]   {regime}: {len(rt)} trades, {wr:.0f}% WR")

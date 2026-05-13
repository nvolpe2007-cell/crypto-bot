"""
Loss Pattern Learner - Optimized Version
Implementing critical fixes from audit report
"""

import math
import logging
from typing import Dict, Tuple
from .trade_journal import TradeJournal, TradeRecord

logger = logging.getLogger(__name__)

# Updated weights - includes post-trade features
_FEATURE_WEIGHTS_UPDATED = {
    'rsi': 2.0,
    'adx': 1.5,
    'volume_ratio': 1.0,
    'atr_pct': 1.0,
    'ema100_gap': 1.5,
    'ema200_gap': 1.5,
    'hour_utc': 0.5,
    'day_of_week': 0.3,
    'mfe_pct': 2.5,  # NEW: How far trade went in our favor
    'mae_pct': 2.5,  # NEW: How far trade went against us
    'confidence': 1.0,  # NEW: Original confidence at entry
    'mfe_to_mae_ratio': 1.5,  # NEW: Ratio measure
}

_FEATURE_SCALE_UPDATED = {
    'rsi': 30.0,
    'adx': 20.0,
    'volume_ratio': 1.5,
    'atr_pct': 1.5,
    'ema100_gap': 5.0,
    'ema200_gap': 8.0,
    'hour_utc': 12.0,
    'day_of_week': 3.5,
    'mfe_pct': 10.0,  # Percentage measure
    'mae_pct': 10.0,
    'confidence': 15.0,
    'mfe_to_mae_ratio': 2.0,
}

# More conservative learning parameters (from audit)
_BASE_CONFIDENCE = 80  # Higher base = more selective (was 75)
_MAX_CONFIDENCE = 95  # Maximum barrier (was 92)
_MIN_TRADES_TO_LEARN = 10  # Need more data (was 5)
_SIMILARITY_DANGER = 0.75  # Stricter threshold (was 0.80)
_LEARNING_RATE = 0.10  # How quickly to adjust thresholds


def _distance_post_trade(current: Dict[str, float], historical: Dict[str, float]) -> float:
    """Weighted normalised Euclidean distance including post-trade features."""
    total_sq = 0.0

    for key, weight in _FEATURE_WEIGHTS_UPDATED.items():
        scale = _FEATURE_SCALE_UPDATED.get(key, 1.0)

        # Handle missing features gracefully
        current_val = current.get(key, 0.0)
        historical_val = historical.get(key, 0.0)

        # Calculate normalized difference
        diff = (current_val - historical_val) / max(scale, 1e-8)

        # Square and weight
        weighted_sq = weight * (diff * diff)
        total_sq += weighted_sq

    distance = math.sqrt(max(total_sq, 0.0))
    return distance


def _similarity_from_distance(dist: float) -> float:
    """Convert distance to 0-1 similarity (1 = identical)."""
    # Use gaussian kernel with sigma=1.2
    sigma = 1.2
    similarity = math.exp(-(dist * dist) / (2 * sigma * sigma))
    return similarity


def _weighted_similarity_score(current: Dict[str, float], records: list) -> Tuple[float, dict]:
    """
    Compute weighted similarity to all records, with outcome weighting.

    Returns:
        Tuple of (net_similarity, stats_dict)
    """
    similarities = []

    # Limit to recent records (avoid O(n^2) performance issues)
    max_records = min(50, len(records))  # Last 50 trades
    recent_records = records[-max_records:]

    for record in recent_records:
        # Get features from record
        try:
            record_features = record.features() if hasattr(record, 'features') else {}
        except Exception as e:
            logger.debug(f"[LEARNER] Error getting features: {e}")
            continue

        # Calculate similarity
        dist = _distance_post_trade(current, record_features)
        sim = _similarity_from_distance(dist)

        # Apply outcome-based weighting (wins positive, losses negative)
        weight = 1.0 if record.won else -3.0  # Losses weighted 3x more heavily

        similarities.append((sim, weight, record.won))

    if not similarities:
        return 0.0, {'error': 'No similarity data'}

    # Sort by weighted similarity
    similarities.sort(key=lambda x: abs(x[0]*x[1]), reverse=True)

    # Take top K (default 5)
    k = min(5, len(similarities))
    top_k = similarities[:k]

    # Calculate net similarity (wins positive, losses negative)
    net_similarity = sum(sim * weight for sim, weight, _ in top_k) / k

    # Calculate stats
    avg_similarity_losses = sum(sim for sim, _, won in top_k if not won) / max(1, sum(1 for _, _, won in top_k if not won))
    avg_similarity_wins = sum(sim for sim, _, won in top_k if won) / max(1, sum(1 for _, _, won in top_k if won))

    stats = {
        'net_similarity': net_similarity,
        'top_k_count': k,
        'avg_loss_similarity': avg_similarity_losses,
        'avg_win_similarity': avg_similarity_wins,
        'looks_like_loss': net_similarity < -0.5,
        'looks_like_win': net_similarity > 0.3,
    }

    return net_similarity, stats


def required_confidence_optimized(
    current_features: Dict[str, float],
    regime: str,
    symbol: str,
    journal: TradeJournal,
    learner_k: int = 5
) -> Tuple[int, Dict]:
    """
    Returns the minimum confidence score required for this trade.
    Bidirectional learning: raises barrier for losing patterns, lowers for winning patterns.

    Changes from audit:
    1. Higher base confidence (80 vs 75)
    2. More data required (10 vs 5 trades)
    3. Stricter similarity threshold (0.75 vs 0.80)
    4. Bidirectional adjustment (wins AND losses)
    5. Includes post-trade features (MFE/MAE)
    6. Regime-aware threshold adjustments

    Returns:
        Tuple[int, float] = (required_confidence, global_stats_dict)
    """
    total_trades = len(journal.records)

    if total_trades < _MIN_TRADES_TO_LEARN:
        logger.debug(f"[LEARNER] {symbol} Not enough data ({total_trades} < {_MIN_TRADES_TO_LEARN}) → base={_BASE_CONFIDENCE}")
        return _BASE_CONFIDENCE, {'status': 'not_enough_data'}

    required = _BASE_CONFIDENCE

    # 1. Regime-level performance analysis
    regime_trades = [r for r in journal.records if r.regime == regime]
    if len(regime_trades) >= 5:  # Need at least 5 trades in regime
        regime_wr = sum(1 for r in regime_trades if r.won) / len(regime_trades)

        if regime_wr < 0.35:  # Loses 65%+ → punitive
            regime_penalty = int((0.35 - regime_wr) / 0.35 * 15)
            required = max(required, _BASE_CONFIDENCE + regime_penalty)
            logger.warning(f"[LEARNER] {symbol} Regime {regime} WR={regime_wr:.0%} → penalty +{regime_penalty}")
        elif regime_wr > 0.60:  # Wins 60%+ → lenient
            regime_bonus = int((regime_wr - 0.60) / 0.40 * 5)
            required = max(60, required - regime_bonus)  # Can't go below 60
            logger.info(f"[LEARNER] {symbol} Regime {regime} WR={regime_wr:.0%} → bonus -{regime_bonus}")

    # 2. Pattern similarity analysis (bidirectional)
    try:
        net_similarity, similarity_stats = _weighted_similarity_score(
            current_features, journal.records, learner_k
        )

        if 'error' in similarity_stats:
            logger.debug(f"[LEARNER] {symbol} Similarity error: {similarity_stats['error']}")
        else:
            # Looks like losses - raise barrier
            if net_similarity < -0.50:  # More similar to losses
                penalty = int(abs(net_similarity) * (_MAX_CONFIDENCE - _BASE_CONFIDENCE))
                required = min(_MAX_CONFIDENCE, required + penalty)
                logger.warning(
                    f"[LEARNER] {symbol} Setup {abs(net_similarity):.0%} similar to losses → "
                    f"penalty +{penalty}\n"
                    f"       avg_loss_sim={similarity_stats['avg_loss_similarity']:.2f} "
                    f"avg_win_sim={similarity_stats['avg_win_similarity']:.2f}"
                )
            # Looks like wins - lower barrier slightly
            elif net_similarity > 0.30:
                bonus = int(net_similarity * 8)
                required = max(60, required - bonus)
                logger.info(
                    f"[LEARNER] {symbol} Setup {net_similarity:.0%} similar to wins → "
                    f"bonus -{bonus}$AGEN_ACCESS$
                    f"avg_loss_sim={similarity_stats['avg_loss_similarity']:.2f} "
                    f"avg_win_sim={similarity_stats['avg_win_similarity']:.2f}"
                )
            else:
                logger.debug(
                    f"[LEARNER] {symbol} Setup net_sim={net_similarity:.2f} (neutral)\n"
                    f"       avg_loss_sim={similarity_stats['avg_loss_similarity']:.2f} "
                    f"avg_win_sim={similarity_stats['avg_win_similarity']:.2f}"
                )
    except Exception as e:
        logger.error(f"[LEARNER] {symbol} Similarity error: {e}")

    # 3. Symbol-specific performance
    sym_trades = [r for r in journal.records if r.symbol == symbol]
    if len(sym_trades) >= 5:  # At least 5 trades in this symbol
        sym_wr = sum(1 for r in sym_trades if r.won) / len(sym_trades)
        avg_pnl = sum(r.pnl for r in sym_trades) / len(sym_trades)

        if sym_wr < 0.30:  # Loses 70%+
            required = min(_MAX_CONFIDENCE, required + 12)  # Strong penalty
            logger.warning(
                f"[LEARNER] {symbol} WR={sym_wr:.0%} avg_pnl=${avg_pnl:.4f} → +12 "
                f"(cursed symbol)"
            )
        elif sym_wr < 0.45 and avg_pnl < -0.02:  # Bad performance
            required = min(_MAX_CONFIDENCE, required + 6)
            logger.warning(
                f"[LEARNER] {symbol} WR={sym_wr:.0%} avg_pnl=${avg_pnl:.4f} → +6"
            )
        elif sym_wr > 0.60 and avg_pnl > 0.01:  # Good performance
            required = max(60, required - 3)
            logger.info(
                f"[LEARNER] {symbol} WR={sym_wr:.0%} avg_pnl=${avg_pnl:.4f} → -3"
            )

    # 4. Check against extremes
    required = max(60, min(_MAX_CONFIDENCE, required))

    logger.info(
        f"[LEARNER] {symbol} Required confidence: {required}\n"
        f"       regime={regime} | total_trades={total_trades}"
    )

    global_stats = {
        'regime_wr': len([r for r in journal.records if r.regime == regime]) / max(1, len(journal.records)),
        'similarity_stats': similarity_stats,
        'symbol': symbol,
        'regime': regime,
    }

    return required, global_stats

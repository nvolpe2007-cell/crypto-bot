"""
ML-based trade outcome predictor using XGBoost.

Learns from closed trades in the TradeJournal to predict win probability.
Blends the resulting P(win) with the rule-based confidence score (55/45 split).

Lifecycle:
  - Falls back to rule-based confidence when fewer than MIN_TRADES records exist.
  - Auto-retrains every RETRAIN_INTERVAL new closed trades.
  - Model persisted to data/ml_model.pkl between sessions.
"""

import logging
import os
import pickle
from typing import Dict, List, Optional

import numpy as np

from .trade_journal import TradeJournal, TradeRecord

logger = logging.getLogger(__name__)

MODEL_FILE       = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'data', 'ml_model.pkl')
MIN_TRADES       = 30    # minimum closed trades before ML activates
RETRAIN_INTERVAL = 20    # retrain after this many new trades since last fit

# Regime → numeric encoding
_REGIME_ENC = {
    'TRENDING_UP':   2,
    'TRENDING_DOWN': -2,
    'RANGING':        0,
    'VOLATILE':       1,
    'CRASH':         -3,
    'UNKNOWN':        0,
}

FEATURE_NAMES = [
    'rsi', 'adx', 'volume_ratio', 'atr_pct', 'ema100_gap', 'ema200_gap',
    'hour_utc', 'day_of_week',
    'ofi', 'lead_lag_strength', 'lead_lag_aligned',
    'regime_encoded', 'regime_confidence', 'funding_rate',
    'ofi_score', 'lead_lag_score', 'regime_score',
    'rule_confidence', 'is_buy',
]


def _record_to_vec(r: TradeRecord) -> List[float]:
    return [
        r.rsi,
        r.adx,
        r.volume_ratio,
        r.atr_pct,
        r.ema100_gap,
        r.ema200_gap,
        float(r.hour_utc),
        float(r.day_of_week),
        r.ofi or 0.0,
        r.lead_lag_strength or 0.0,
        float(r.lead_lag_aligned),
        float(_REGIME_ENC.get(r.regime, 0)),
        r.regime_confidence or 0.5,
        r.funding_rate or 0.0,
        r.ofi_score or 0.0,
        r.lead_lag_score or 0.0,
        r.regime_score or 0.0,
        r.confidence or 50.0,
        float(r.direction == 'buy'),
    ]


def _features_to_vec(f: Dict) -> List[float]:
    return [
        f.get('rsi', 50.0),
        f.get('adx', 20.0),
        f.get('volume_ratio', 1.0),
        f.get('atr_pct', 1.0),
        f.get('ema100_gap', 0.0),
        f.get('ema200_gap', 0.0),
        float(f.get('hour_utc', 12)),
        float(f.get('day_of_week', 0)),
        f.get('ofi', 0.0) or 0.0,
        f.get('lead_lag_strength', 0.0) or 0.0,
        float(f.get('lead_lag_aligned', False)),
        float(_REGIME_ENC.get(f.get('regime', 'UNKNOWN'), 0)),
        f.get('regime_confidence', 0.5) or 0.5,
        f.get('funding_rate', 0.0) or 0.0,
        f.get('ofi_score', 0.0),
        f.get('lead_lag_score', 0.0),
        f.get('regime_score', 0.0),
        f.get('rule_confidence', 50.0),
        float(f.get('is_buy', True)),
    ]


class MLScorer:
    """
    XGBoost win-probability predictor.

    Usage:
        scorer = MLScorer(journal)
        # After each trade closes:
        if scorer.should_retrain():
            scorer.train()
        # When evaluating a new setup:
        final_conf = scorer.blend_confidence(rule_conf, feature_dict)
    """

    def __init__(self, journal: TradeJournal):
        self.journal = journal
        self._model  = None
        self._scaler = None
        self._n_at_last_train = 0
        self._load()

    # ── Persistence ────────────────────────────────────────────────────────────

    def _load(self):
        try:
            if os.path.exists(MODEL_FILE):
                with open(MODEL_FILE, 'rb') as f:
                    saved = pickle.load(f)
                self._model  = saved['model']
                self._scaler = saved['scaler']
                self._n_at_last_train = saved.get('n_trades', 0)
                logger.info(f"[ML] Loaded model (trained on {self._n_at_last_train} trades)")
        except Exception as e:
            logger.debug(f"[ML] Could not load model: {e}")

    def _save(self):
        try:
            os.makedirs(os.path.dirname(MODEL_FILE), exist_ok=True)
            with open(MODEL_FILE, 'wb') as f:
                pickle.dump({
                    'model':    self._model,
                    'scaler':   self._scaler,
                    'n_trades': self._n_at_last_train,
                }, f)
        except Exception as e:
            logger.warning(f"[ML] Save failed: {e}")

    # ── Training ───────────────────────────────────────────────────────────────

    def should_retrain(self) -> bool:
        n = len(self.journal.records)
        return n >= MIN_TRADES and (n - self._n_at_last_train) >= RETRAIN_INTERVAL

    def train(self) -> bool:
        """Train on all records in the journal. Returns True on success."""
        try:
            from xgboost import XGBClassifier
            from sklearn.preprocessing import StandardScaler
            from sklearn.model_selection import cross_val_score

            records = self.journal.records
            if len(records) < MIN_TRADES:
                logger.info(f"[ML] Need {MIN_TRADES} trades to train, have {len(records)}")
                return False

            X = np.array([_record_to_vec(r) for r in records], dtype=float)
            y = np.array([int(r.won) for r in records])

            n_pos = int(y.sum())
            n_neg = len(y) - n_pos
            scale_pos = (n_neg / n_pos) if n_pos > 0 else 1.0

            scaler = StandardScaler()
            Xs = scaler.fit_transform(X)

            model = XGBClassifier(
                n_estimators=150,
                max_depth=4,
                learning_rate=0.08,
                subsample=0.8,
                colsample_bytree=0.8,
                scale_pos_weight=scale_pos,
                random_state=42,
                eval_metric='logloss',
                verbosity=0,
            )
            model.fit(Xs, y)

            # Cross-validation AUC when we have enough data
            if len(records) >= 60:
                scores = cross_val_score(model, Xs, y, cv=3, scoring='roc_auc')
                logger.info(
                    f"[ML] Trained on {len(records)} trades  "
                    f"AUC={scores.mean():.3f}±{scores.std():.3f}  "
                    f"class_balance={n_pos}/{n_neg}"
                )
            else:
                logger.info(f"[ML] Trained on {len(records)} trades (CV requires 60+)")

            # Log top predictive features
            importances = model.feature_importances_
            top5 = sorted(zip(FEATURE_NAMES, importances), key=lambda x: -x[1])[:5]
            logger.info("[ML] Top features: " + ", ".join(f"{n}={v:.3f}" for n, v in top5))

            self._model  = model
            self._scaler = scaler
            self._n_at_last_train = len(records)
            self._save()
            return True

        except ImportError:
            logger.warning("[ML] xgboost not installed — run: pip install xgboost")
            return False
        except Exception as e:
            logger.warning(f"[ML] Training failed: {e}")
            return False

    # ── Inference ──────────────────────────────────────────────────────────────

    def predict_win_prob(self, features: Dict) -> Optional[float]:
        """Return P(win) in [0, 1], or None if model not ready."""
        if self._model is None or self._scaler is None:
            return None
        try:
            X = np.array([_features_to_vec(features)], dtype=float)
            Xs = self._scaler.transform(X)
            return float(self._model.predict_proba(Xs)[0, 1])
        except Exception as e:
            logger.debug(f"[ML] Prediction failed: {e}")
            return None

    def blend_confidence(self, rule_confidence: float, features: Dict) -> float:
        """
        Blend rule-based confidence with ML win probability.
        Returns final confidence (0–100).
        Falls back to rule_confidence unchanged when the model isn't ready.
        """
        ml_prob = self.predict_win_prob(features)
        if ml_prob is None:
            return rule_confidence

        ml_conf  = ml_prob * 100.0
        blended  = 0.55 * rule_confidence + 0.45 * ml_conf
        blended  = max(0.0, min(100.0, blended))

        logger.debug(
            f"[ML] rule={rule_confidence:.0f}  ml={ml_conf:.0f}  "
            f"→ blended={blended:.0f}  P(win)={ml_prob:.2f}"
        )
        return blended

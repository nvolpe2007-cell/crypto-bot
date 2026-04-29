"""
CVaR Portfolio Optimizer
Sizes positions across BTC/ETH/SOL by minimising Conditional Value at Risk
(Expected Shortfall) at the 95th percentile.

Instead of fixed $50 per trade, this allocates capital based on:
- Correlation between pairs (don't over-expose to one move)
- Tail risk of each pair (volatile pairs get smaller allocation)
- Current regime (regime-aware sizing)

Usage:
    opt = PortfolioOptimizer()
    weights = opt.optimize(returns_dict)  # {'BTC/USD': [...], 'ETH/USD': [...]}
    # weights = {'BTC/USD': 0.55, 'ETH/USD': 0.35, 'SOL/USD': 0.10}
"""

import logging
import numpy as np
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

_CONFIDENCE_LEVEL = 0.95   # CVaR at 95th percentile
_MIN_WEIGHT       = 0.05   # no pair gets less than 5%
_MAX_WEIGHT       = 0.70   # no pair gets more than 70%
_MIN_HISTORY      = 20     # minimum daily return observations needed


def _cvar(weights: np.ndarray, returns: np.ndarray, confidence: float = 0.95) -> float:
    """CVaR (Expected Shortfall) — mean loss in the worst (1-confidence)% of days."""
    portfolio_returns = returns @ weights
    cutoff = np.percentile(portfolio_returns, (1 - confidence) * 100)
    tail   = portfolio_returns[portfolio_returns <= cutoff]
    return float(-tail.mean()) if len(tail) > 0 else 0.0


def _cvar_gradient(weights: np.ndarray, returns: np.ndarray,
                   confidence: float = 0.95) -> np.ndarray:
    """Numerical gradient of CVaR for optimisation."""
    eps = 1e-5
    grad = np.zeros_like(weights)
    base = _cvar(weights, returns, confidence)
    for i in range(len(weights)):
        w_plus = weights.copy()
        w_plus[i] += eps
        w_plus /= w_plus.sum()
        grad[i] = (_cvar(w_plus, returns, confidence) - base) / eps
    return grad


class PortfolioOptimizer:
    """
    CVaR-minimising portfolio optimiser for the 3-pair crypto bot.

    Call optimize() after each closed trade to get fresh allocations.
    Falls back to equal weights when insufficient history.
    """

    def __init__(self, confidence: float = _CONFIDENCE_LEVEL,
                 max_cvar_pct: float = 0.03):
        self.confidence   = confidence
        self.max_cvar_pct = max_cvar_pct   # max 3% CVaR of equity per cycle
        self._last_weights: Optional[Dict[str, float]] = None
        self._last_cvar:    float = 0.0
        self._correlations: Optional[np.ndarray] = None

    def optimize(self, returns: Dict[str, List[float]]) -> Dict[str, float]:
        """
        Compute optimal CVaR weights.

        Args:
            returns: {symbol: [daily_return, ...]}  (plain fractions, e.g. 0.02 = +2%)

        Returns:
            {symbol: weight}  where weights sum to 1.0
        """
        symbols = list(returns.keys())
        n = len(symbols)

        if n == 0:
            return {}
        if n == 1:
            return {symbols[0]: 1.0}

        # Check we have enough data
        min_len = min(len(v) for v in returns.values())
        if min_len < _MIN_HISTORY:
            logger.debug(f"[CVaR] Not enough history ({min_len} < {_MIN_HISTORY}) — equal weights")
            equal = 1.0 / n
            return {s: round(equal, 4) for s in symbols}

        try:
            from scipy.optimize import minimize

            # Build returns matrix (rows=observations, cols=assets)
            max_len = max(len(v) for v in returns.values())
            R = np.zeros((max_len, n))
            for j, sym in enumerate(symbols):
                r = np.array(returns[sym])
                # Align to same length by padding with zeros at start
                R[max_len - len(r):, j] = r

            # Store correlation matrix for display
            self._correlations = np.corrcoef(R.T)

            # Initial guess: equal weights
            w0 = np.ones(n) / n

            # Constraints: weights sum to 1
            constraints = [{'type': 'eq', 'fun': lambda w: w.sum() - 1.0}]

            # Bounds: min/max per asset
            bounds = [(_MIN_WEIGHT, _MAX_WEIGHT)] * n

            result = minimize(
                fun=lambda w: _cvar(w, R, self.confidence),
                x0=w0,
                method='SLSQP',
                bounds=bounds,
                constraints=constraints,
                options={'maxiter': 200, 'ftol': 1e-9}
            )

            if result.success:
                raw_weights = result.x
                # Normalise to sum exactly to 1
                raw_weights = np.clip(raw_weights, _MIN_WEIGHT, _MAX_WEIGHT)
                raw_weights /= raw_weights.sum()

                weights = {sym: round(float(w), 4) for sym, w in zip(symbols, raw_weights)}
                self._last_weights = weights
                self._last_cvar    = _cvar(raw_weights, R, self.confidence)

                logger.info(
                    f"[CVaR] Optimised: {weights} | "
                    f"Portfolio CVaR={self._last_cvar*100:.2f}%"
                )
                return weights
            else:
                logger.warning(f"[CVaR] Optimisation failed: {result.message}")
                return self._equal_weights(symbols)

        except Exception as e:
            logger.warning(f"[CVaR] Error: {e}")
            return self._equal_weights(symbols)

    def _equal_weights(self, symbols: List[str]) -> Dict[str, float]:
        w = round(1.0 / len(symbols), 4)
        return {s: w for s in symbols}

    def get_position_size(self, symbol: str, base_usd: float) -> float:
        """Scale base_usd by the symbol's CVaR weight."""
        if not self._last_weights or symbol not in self._last_weights:
            return base_usd
        return round(base_usd * self._last_weights[symbol] * len(self._last_weights), 2)

    def to_dict(self) -> dict:
        return {
            'weights':      self._last_weights or {},
            'portfolio_cvar': round(self._last_cvar * 100, 3),
            'correlations': self._correlations.tolist() if self._correlations is not None else [],
        }

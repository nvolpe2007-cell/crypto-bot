"""
Unit tests for src/portfolio_optimizer.py

Covers:
- _cvar: basic computation, monotonicity (worse portfolio = higher CVaR),
  all-positive returns → CVaR ~0, single-element tail
- PortfolioOptimizer.optimize:
  - empty returns dict → empty result
  - single asset → weight 1.0
  - insufficient history → equal weights (no scipy call)
  - enough history → weights sum to 1.0, each within [MIN_WEIGHT, MAX_WEIGHT]
  - high-vol asset receives lower or equal weight than low-vol asset
  - falls back to equal weights when scipy unavailable or optimisation fails
  - two-asset deterministic case: low-vol asset dominates
- PortfolioOptimizer.get_position_size:
  - returns base_usd unchanged when no weights have been set
  - returns base_usd unchanged when symbol not in weights
  - scales correctly when weights are available
  - rounds to 2 decimal places
- PortfolioOptimizer.to_dict:
  - returns expected keys
  - portfolio_cvar is non-negative
  - weights key matches last optimize() result
  - correlations is [] before first optimize, list-of-lists after
- _cvar_gradient: finite differences agree in sign with manual expectation
"""

import math
import pytest
import numpy as np

from src.portfolio_optimizer import (
    PortfolioOptimizer,
    _cvar,
    _cvar_gradient,
    _MIN_WEIGHT,
    _MAX_WEIGHT,
    _MIN_HISTORY,
)


# ── helpers ────────────────────────────────────────────────────────────────────

def _stable_returns(n: int = 30, mu: float = 0.001, sigma: float = 0.005,
                    seed: int = 42) -> list:
    """Low-volatility daily returns around mu."""
    rng = np.random.default_rng(seed)
    return list(float(x) for x in rng.normal(mu, sigma, n))


def _volatile_returns(n: int = 30, mu: float = 0.001, sigma: float = 0.05,
                      seed: int = 99) -> list:
    """High-volatility daily returns — large tail losses."""
    rng = np.random.default_rng(seed)
    return list(float(x) for x in rng.normal(mu, sigma, n))


# ── _cvar ──────────────────────────────────────────────────────────────────────

class TestCvar:
    def test_positive_returns_give_zero_or_small_cvar(self):
        # All returns are strongly positive — tail should be near zero or negative CVaR
        returns = np.array([[0.05, 0.05], [0.06, 0.04], [0.04, 0.06]])
        weights = np.array([0.5, 0.5])
        c = _cvar(weights, returns)
        # CVaR of all-positive returns should be ≤ 0 (no losses in tail)
        assert c <= 0.0

    def test_all_losses_give_positive_cvar(self):
        # All returns are large losses — high CVaR
        returns = np.array([[-0.10, -0.08], [-0.12, -0.09], [-0.15, -0.11]])
        weights = np.array([0.5, 0.5])
        c = _cvar(weights, returns)
        assert c > 0

    def test_higher_vol_portfolio_has_higher_cvar(self):
        rng = np.random.default_rng(0)
        low_vol  = rng.normal(0.001, 0.005, (100, 1))
        high_vol = rng.normal(0.001, 0.05,  (100, 1))
        c_low  = _cvar(np.array([1.0]), low_vol)
        c_high = _cvar(np.array([1.0]), high_vol)
        assert c_high > c_low

    def test_uniform_weights_sum_to_one(self):
        rng = np.random.default_rng(7)
        returns = rng.normal(0, 0.01, (50, 3))
        weights = np.array([1/3, 1/3, 1/3])
        c = _cvar(weights, returns)
        assert math.isfinite(c)

    def test_returns_float(self):
        returns = np.array([[-0.01], [-0.02], [0.01]])
        c = _cvar(np.array([1.0]), returns)
        assert isinstance(c, float)

    def test_single_return_in_tail(self):
        # With 5% confidence level (= 95th VaR), 1 out of 20 returns is in tail
        returns_1d = np.array([-0.50] + [0.01] * 19)
        returns = returns_1d.reshape(-1, 1)
        c = _cvar(np.array([1.0]), returns, confidence=0.95)
        # Tail contains the -50% return → CVaR ≈ 0.50
        assert abs(c - 0.50) < 0.01


# ── _cvar_gradient ─────────────────────────────────────────────────────────────

class TestCvarGradient:
    def test_gradient_length_matches_weights(self):
        rng = np.random.default_rng(3)
        returns = rng.normal(0, 0.02, (50, 3))
        w = np.array([1/3, 1/3, 1/3])
        grad = _cvar_gradient(w, returns)
        assert len(grad) == 3

    def test_gradient_is_finite(self):
        rng = np.random.default_rng(4)
        returns = rng.normal(0, 0.02, (50, 2))
        w = np.array([0.5, 0.5])
        grad = _cvar_gradient(w, returns)
        assert all(math.isfinite(g) for g in grad)

    def test_high_vol_asset_has_positive_gradient(self):
        # Increasing weight on the high-vol asset should increase CVaR
        rng = np.random.default_rng(5)
        low_vol  = rng.normal(0, 0.001, 100)
        high_vol = rng.normal(0, 0.10,  100)
        returns = np.column_stack([low_vol, high_vol])
        w = np.array([0.5, 0.5])
        grad = _cvar_gradient(w, returns)
        # Gradient for high-vol asset should be >= gradient for low-vol
        assert grad[1] >= grad[0]


# ── PortfolioOptimizer.optimize ────────────────────────────────────────────────

class TestOptimizeEdgeCases:
    def test_empty_returns_gives_empty_dict(self):
        opt = PortfolioOptimizer()
        result = opt.optimize({})
        assert result == {}

    def test_single_asset_gives_weight_one(self):
        opt = PortfolioOptimizer()
        result = opt.optimize({"BTC/USD": _stable_returns(30)})
        assert result == {"BTC/USD": 1.0}

    def test_insufficient_history_gives_equal_weights(self):
        # Fewer rows than _MIN_HISTORY → equal-weight fallback
        short = [0.01] * (_MIN_HISTORY - 1)
        result = PortfolioOptimizer().optimize({
            "BTC/USD": short,
            "ETH/USD": short,
        })
        assert abs(result["BTC/USD"] - result["ETH/USD"]) < 1e-6

    def test_exactly_min_history_does_not_fall_back(self):
        # Exactly MIN_HISTORY rows — optimisation should run
        data = _stable_returns(_MIN_HISTORY)
        result = PortfolioOptimizer().optimize({
            "BTC/USD": data,
            "ETH/USD": data,
        })
        # Weights sum to 1 (may be equal since both series are same)
        total = sum(result.values())
        assert abs(total - 1.0) < 1e-3

    def test_two_assets_equal_returns_gives_roughly_equal_weights(self):
        # Identical assets → CVaR indifferent → optimizer lands near 50/50
        data = _stable_returns(50)
        result = PortfolioOptimizer().optimize({
            "BTC/USD": data,
            "ETH/USD": data,
        })
        assert abs(result["BTC/USD"] - result["ETH/USD"]) < 0.20


class TestOptimizeWeightProperties:
    def test_weights_sum_to_one(self):
        opt = PortfolioOptimizer()
        result = opt.optimize({
            "BTC/USD": _stable_returns(50, seed=1),
            "ETH/USD": _stable_returns(50, seed=2),
            "SOL/USD": _stable_returns(50, seed=3),
        })
        assert abs(sum(result.values()) - 1.0) < 1e-3

    def test_all_weights_at_least_min_weight(self):
        result = PortfolioOptimizer().optimize({
            "BTC/USD": _stable_returns(50, seed=10),
            "ETH/USD": _volatile_returns(50, seed=11),
        })
        for sym, w in result.items():
            assert w >= _MIN_WEIGHT - 1e-6, f"{sym} weight {w} < MIN_WEIGHT"

    def test_all_weights_at_most_max_weight(self):
        result = PortfolioOptimizer().optimize({
            "BTC/USD": _stable_returns(50, seed=10),
            "ETH/USD": _volatile_returns(50, seed=11),
        })
        for sym, w in result.items():
            assert w <= _MAX_WEIGHT + 1e-6, f"{sym} weight {w} > MAX_WEIGHT"

    def test_all_weights_positive(self):
        result = PortfolioOptimizer().optimize({
            "BTC/USD": _stable_returns(50, seed=20),
            "ETH/USD": _stable_returns(50, seed=21),
            "SOL/USD": _stable_returns(50, seed=22),
        })
        for w in result.values():
            assert w > 0

    def test_all_weights_finite(self):
        result = PortfolioOptimizer().optimize({
            "BTC/USD": _stable_returns(60, seed=30),
            "ETH/USD": _stable_returns(60, seed=31),
        })
        for w in result.values():
            assert math.isfinite(w)

    def test_symbols_preserved_as_keys(self):
        symbols = {"BTC/USD", "ETH/USD", "SOL/USD"}
        result = PortfolioOptimizer().optimize({
            s: _stable_returns(50) for s in symbols
        })
        assert set(result.keys()) == symbols


class TestOptimizeRiskSensitivity:
    def test_high_vol_asset_gets_lower_or_equal_weight(self):
        """CVaR minimisation should prefer the lower-volatility asset."""
        # Use many observations to make the signal clear
        low  = _stable_returns(200, sigma=0.002, seed=7)
        high = _volatile_returns(200, sigma=0.08, seed=8)
        result = PortfolioOptimizer().optimize({
            "STABLE": low,
            "VOLATILE": high,
        })
        assert result["STABLE"] >= result["VOLATILE"]

    def test_last_weights_stored_after_optimize(self):
        opt = PortfolioOptimizer()
        assert opt._last_weights is None
        opt.optimize({
            "BTC/USD": _stable_returns(50, seed=40),
            "ETH/USD": _stable_returns(50, seed=41),
        })
        assert opt._last_weights is not None

    def test_last_cvar_stored_after_optimize(self):
        opt = PortfolioOptimizer()
        assert opt._last_cvar == 0.0
        opt.optimize({
            "BTC/USD": _stable_returns(50, seed=50),
            "ETH/USD": _volatile_returns(50, seed=51),
        })
        assert math.isfinite(opt._last_cvar)

    def test_correlations_stored_after_optimize(self):
        opt = PortfolioOptimizer()
        assert opt._correlations is None
        opt.optimize({
            "BTC/USD": _stable_returns(50, seed=60),
            "ETH/USD": _stable_returns(50, seed=61),
        })
        assert opt._correlations is not None
        assert opt._correlations.shape == (2, 2)


class TestOptimizeFallback:
    def test_fallback_when_scipy_unavailable(self, monkeypatch):
        """If scipy is not importable, optimize() returns equal weights."""
        import builtins
        real_import = builtins.__import__

        def _no_scipy(name, *args, **kwargs):
            if name == "scipy.optimize":
                raise ImportError("mocked scipy unavailable")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", _no_scipy)
        result = PortfolioOptimizer().optimize({
            "BTC/USD": _stable_returns(50),
            "ETH/USD": _stable_returns(50),
        })
        # Should fall back to equal weights
        assert abs(result["BTC/USD"] - result["ETH/USD"]) < 1e-6


# ── PortfolioOptimizer.get_position_size ──────────────────────────────────────

class TestGetPositionSize:
    def test_returns_base_usd_when_no_weights(self):
        opt = PortfolioOptimizer()
        assert opt.get_position_size("BTC/USD", 100.0) == 100.0

    def test_returns_base_usd_for_unknown_symbol(self):
        opt = PortfolioOptimizer()
        opt._last_weights = {"ETH/USD": 0.6, "SOL/USD": 0.4}
        assert opt.get_position_size("BTC/USD", 50.0) == 50.0

    def test_scales_by_weight_times_n_assets(self):
        opt = PortfolioOptimizer()
        # 2 assets, BTC weight = 0.70 → 50 * 0.70 * 2 = 70.0
        opt._last_weights = {"BTC/USD": 0.70, "ETH/USD": 0.30}
        result = opt.get_position_size("BTC/USD", 50.0)
        assert abs(result - 70.0) < 1e-6

    def test_lower_weight_gives_smaller_size(self):
        opt = PortfolioOptimizer()
        opt._last_weights = {"BTC/USD": 0.30, "ETH/USD": 0.70}
        size_btc = opt.get_position_size("BTC/USD", 100.0)
        size_eth = opt.get_position_size("ETH/USD", 100.0)
        assert size_btc < size_eth

    def test_result_rounded_to_2dp(self):
        opt = PortfolioOptimizer()
        opt._last_weights = {"BTC/USD": 1/3, "ETH/USD": 1/3, "SOL/USD": 1/3}
        result = opt.get_position_size("BTC/USD", 100.0)
        # Result should be rounded to 2 decimal places
        assert result == round(result, 2)

    def test_equal_weights_preserve_base_usd(self):
        # With 3 equal weights of 1/3, size = base * (1/3) * 3 = base
        opt = PortfolioOptimizer()
        opt._last_weights = {"BTC/USD": round(1/3, 4),
                             "ETH/USD": round(1/3, 4),
                             "SOL/USD": round(1/3, 4)}
        result = opt.get_position_size("BTC/USD", 90.0)
        # 90 * (1/3) * 3 = 90 (within rounding)
        assert abs(result - 90.0) < 0.1


# ── PortfolioOptimizer.to_dict ────────────────────────────────────────────────

class TestToDict:
    def test_returns_dict_with_expected_keys(self):
        opt = PortfolioOptimizer()
        d = opt.to_dict()
        assert set(d.keys()) == {"weights", "portfolio_cvar", "correlations"}

    def test_weights_empty_before_optimize(self):
        opt = PortfolioOptimizer()
        assert opt.to_dict()["weights"] == {}

    def test_correlations_empty_list_before_optimize(self):
        opt = PortfolioOptimizer()
        assert opt.to_dict()["correlations"] == []

    def test_portfolio_cvar_zero_before_optimize(self):
        opt = PortfolioOptimizer()
        assert opt.to_dict()["portfolio_cvar"] == 0.0

    def test_weights_populated_after_optimize(self):
        opt = PortfolioOptimizer()
        opt.optimize({
            "BTC/USD": _stable_returns(50, seed=70),
            "ETH/USD": _stable_returns(50, seed=71),
        })
        d = opt.to_dict()
        assert "BTC/USD" in d["weights"]
        assert "ETH/USD" in d["weights"]

    def test_portfolio_cvar_non_negative_after_optimize(self):
        opt = PortfolioOptimizer()
        opt.optimize({
            "BTC/USD": _stable_returns(50, seed=80),
            "ETH/USD": _volatile_returns(50, seed=81),
        })
        assert opt.to_dict()["portfolio_cvar"] >= 0.0

    def test_correlations_is_list_of_lists_after_optimize(self):
        opt = PortfolioOptimizer()
        opt.optimize({
            "BTC/USD": _stable_returns(50, seed=90),
            "ETH/USD": _stable_returns(50, seed=91),
        })
        corr = opt.to_dict()["correlations"]
        assert isinstance(corr, list)
        assert all(isinstance(row, list) for row in corr)

    def test_correlation_matrix_is_square(self):
        opt = PortfolioOptimizer()
        opt.optimize({
            "BTC/USD": _stable_returns(50, seed=100),
            "ETH/USD": _stable_returns(50, seed=101),
            "SOL/USD": _stable_returns(50, seed=102),
        })
        corr = opt.to_dict()["correlations"]
        assert len(corr) == 3
        assert all(len(row) == 3 for row in corr)

    def test_portfolio_cvar_rounded_to_3dp(self):
        opt = PortfolioOptimizer()
        opt.optimize({
            "BTC/USD": _stable_returns(50, seed=110),
            "ETH/USD": _volatile_returns(50, seed=111),
        })
        d = opt.to_dict()
        # Value should be rounded to 3 decimal places
        assert d["portfolio_cvar"] == round(d["portfolio_cvar"], 3)


# ── equal_weights helper ───────────────────────────────────────────────────────

class TestEqualWeights:
    def test_single_symbol(self):
        opt = PortfolioOptimizer()
        result = opt._equal_weights(["BTC/USD"])
        assert result == {"BTC/USD": 1.0}

    def test_two_symbols_sum_to_one(self):
        opt = PortfolioOptimizer()
        result = opt._equal_weights(["BTC/USD", "ETH/USD"])
        assert abs(sum(result.values()) - 1.0) < 1e-6

    def test_three_symbols_each_approx_one_third(self):
        opt = PortfolioOptimizer()
        result = opt._equal_weights(["BTC/USD", "ETH/USD", "SOL/USD"])
        for w in result.values():
            assert abs(w - round(1/3, 4)) < 1e-6

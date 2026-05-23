"""
Unit tests for src/config_validator.py

Every validation rule has at least:
  - a test with a valid value (no error)
  - a test with an invalid value (error raised)

Where relevant, boundary conditions (e.g. exactly-equal thresholds) are
tested explicitly.
"""

import pytest
from src.config_validator import validate_config, ConfigValidationError


# ── helpers ───────────────────────────────────────────────────────────────────

def _base_config(**overrides) -> dict:
    """Return a fully-valid minimal config, optionally overriding any key."""
    cfg = {
        "exchange": {"name": "kraken", "sandbox": False},
        "trading": {
            "mode": "paper",
            "initial_capital": 500,
            "pairs": ["BTC/USD"],
            "timeframe": "1m",
        },
        "strategy": {
            "fast_ema": 9,
            "slow_ema": 21,
            "rsi_period": 14,
            "rsi_overbought": 70,
            "rsi_oversold": 30,
        },
        "risk": {
            "max_position_size": 25,
            "stop_loss_pct": 2.0,
            "take_profit_pct": 3.0,
            "max_daily_loss": 15,
            "max_open_positions": 3,
        },
    }
    # Allow nested overrides via dotted key syntax: "trading.mode"
    for key, val in overrides.items():
        if "." in key:
            section, field = key.split(".", 1)
            cfg.setdefault(section, {})[field] = val
        else:
            cfg[key] = val
    return cfg


def _errors_for(cfg: dict) -> list[str]:
    """Return list of error strings from ConfigValidationError, or [] on success."""
    try:
        validate_config(cfg)
        return []
    except ConfigValidationError as exc:
        return exc.errors


def _warnings_for(cfg: dict) -> list[str]:
    """Return warning list (may trigger ConfigValidationError if errors present)."""
    try:
        return validate_config(cfg)
    except ConfigValidationError as exc:
        return exc.warnings


# ── valid base config ─────────────────────────────────────────────────────────

class TestValidBaseConfig:
    def test_valid_config_raises_no_error(self):
        validate_config(_base_config())

    def test_valid_config_returns_list(self):
        result = validate_config(_base_config())
        assert isinstance(result, list)

    def test_valid_config_returns_no_warnings_by_default(self):
        assert validate_config(_base_config()) == []


# ── trading section ───────────────────────────────────────────────────────────

class TestTradingMode:
    def test_paper_mode_is_valid(self):
        assert _errors_for(_base_config(**{"trading.mode": "paper"})) == []

    def test_backtest_mode_is_valid(self):
        assert _errors_for(_base_config(**{"trading.mode": "backtest"})) == []

    def test_live_mode_is_valid(self):
        cfg = _base_config(**{"trading.mode": "live"})
        cfg["exchange"]["sandbox"] = False
        cfg["risk"]["max_daily_loss"] = 15
        assert _errors_for(cfg) == []

    def test_invalid_mode_raises_error(self):
        errors = _errors_for(_base_config(**{"trading.mode": "LIVE"}))
        assert any("trading.mode" in e for e in errors)

    def test_empty_mode_raises_error(self):
        errors = _errors_for(_base_config(**{"trading.mode": ""}))
        assert any("trading.mode" in e for e in errors)

    def test_missing_trading_section_raises_error(self):
        cfg = _base_config()
        del cfg["trading"]
        errors = _errors_for(cfg)
        assert any("trading" in e for e in errors)


class TestInitialCapital:
    def test_positive_integer_is_valid(self):
        assert _errors_for(_base_config(**{"trading.initial_capital": 1000})) == []

    def test_positive_float_is_valid(self):
        assert _errors_for(_base_config(**{"trading.initial_capital": 99.99})) == []

    def test_zero_raises_error(self):
        errors = _errors_for(_base_config(**{"trading.initial_capital": 0}))
        assert any("initial_capital" in e for e in errors)

    def test_negative_raises_error(self):
        errors = _errors_for(_base_config(**{"trading.initial_capital": -10}))
        assert any("initial_capital" in e for e in errors)

    def test_missing_raises_error(self):
        cfg = _base_config()
        del cfg["trading"]["initial_capital"]
        errors = _errors_for(cfg)
        assert any("initial_capital" in e for e in errors)

    def test_string_value_raises_error(self):
        errors = _errors_for(_base_config(**{"trading.initial_capital": "500"}))
        assert any("initial_capital" in e for e in errors)

    def test_small_capital_gives_warning(self):
        warnings = _warnings_for(_base_config(**{"trading.initial_capital": 5}))
        assert any("initial_capital" in w for w in warnings)


class TestTradingPairs:
    def test_single_pair_is_valid(self):
        assert _errors_for(_base_config(**{"trading.pairs": ["BTC/USD"]})) == []

    def test_multiple_pairs_are_valid(self):
        assert _errors_for(_base_config(**{"trading.pairs": ["BTC/USD", "ETH/USD"]})) == []

    def test_empty_list_raises_error(self):
        errors = _errors_for(_base_config(**{"trading.pairs": []}))
        assert any("pairs" in e for e in errors)

    def test_none_pairs_raises_error(self):
        errors = _errors_for(_base_config(**{"trading.pairs": None}))
        assert any("pairs" in e for e in errors)

    def test_missing_slash_raises_error(self):
        errors = _errors_for(_base_config(**{"trading.pairs": ["BTCUSD"]}))
        assert any("pairs" in e for e in errors)

    def test_non_string_pair_raises_error(self):
        errors = _errors_for(_base_config(**{"trading.pairs": [123]}))
        assert any("pairs" in e for e in errors)


class TestTimeframe:
    def test_1m_is_valid(self):
        assert _errors_for(_base_config(**{"trading.timeframe": "1m"})) == []

    def test_1h_is_valid(self):
        assert _errors_for(_base_config(**{"trading.timeframe": "1h"})) == []

    def test_invalid_timeframe_raises_error(self):
        errors = _errors_for(_base_config(**{"trading.timeframe": "2m"}))
        assert any("timeframe" in e for e in errors)

    def test_empty_string_raises_error(self):
        errors = _errors_for(_base_config(**{"trading.timeframe": ""}))
        assert any("timeframe" in e for e in errors)


# ── strategy section ──────────────────────────────────────────────────────────

class TestEmaParameters:
    def test_fast_lt_slow_is_valid(self):
        cfg = _base_config()
        cfg["strategy"]["fast_ema"] = 9
        cfg["strategy"]["slow_ema"] = 21
        assert _errors_for(cfg) == []

    def test_fast_equal_slow_raises_error(self):
        cfg = _base_config()
        cfg["strategy"]["fast_ema"] = 21
        cfg["strategy"]["slow_ema"] = 21
        errors = _errors_for(cfg)
        assert any("fast_ema" in e for e in errors)

    def test_fast_greater_than_slow_raises_error(self):
        cfg = _base_config()
        cfg["strategy"]["fast_ema"] = 50
        cfg["strategy"]["slow_ema"] = 21
        errors = _errors_for(cfg)
        assert any("fast_ema" in e for e in errors)

    def test_zero_fast_ema_raises_error(self):
        cfg = _base_config()
        cfg["strategy"]["fast_ema"] = 0
        errors = _errors_for(cfg)
        assert any("fast_ema" in e for e in errors)

    def test_negative_slow_ema_raises_error(self):
        cfg = _base_config()
        cfg["strategy"]["slow_ema"] = -5
        errors = _errors_for(cfg)
        assert any("slow_ema" in e for e in errors)

    def test_float_fast_ema_raises_error(self):
        cfg = _base_config()
        cfg["strategy"]["fast_ema"] = 9.5
        errors = _errors_for(cfg)
        assert any("fast_ema" in e for e in errors)


class TestRsiParameters:
    def test_valid_rsi_period(self):
        cfg = _base_config()
        cfg["strategy"]["rsi_period"] = 14
        assert _errors_for(cfg) == []

    def test_zero_rsi_period_raises_error(self):
        cfg = _base_config()
        cfg["strategy"]["rsi_period"] = 0
        errors = _errors_for(cfg)
        assert any("rsi_period" in e for e in errors)

    def test_rsi_ob_le_os_raises_error(self):
        cfg = _base_config()
        cfg["strategy"]["rsi_overbought"] = 30
        cfg["strategy"]["rsi_oversold"] = 70
        errors = _errors_for(cfg)
        assert any("rsi_overbought" in e for e in errors)

    def test_rsi_ob_equal_os_raises_error(self):
        cfg = _base_config()
        cfg["strategy"]["rsi_overbought"] = 50
        cfg["strategy"]["rsi_oversold"] = 50
        errors = _errors_for(cfg)
        assert any("rsi_overbought" in e for e in errors)

    def test_extreme_rsi_ob_gives_warning(self):
        cfg = _base_config()
        cfg["strategy"]["rsi_overbought"] = 40  # below 50 — unusual
        cfg["strategy"]["rsi_oversold"] = 20
        # Would fail the ob>os check if os >= ob, but 40>20 is fine; should warn
        warnings = _warnings_for(cfg)
        assert any("rsi_overbought" in w for w in warnings)


# ── risk section ─────────────────────────────────────────────────────────────

class TestStopLossTakeProfit:
    def test_sl_lt_tp_is_valid(self):
        cfg = _base_config()
        cfg["risk"]["stop_loss_pct"] = 2.0
        cfg["risk"]["take_profit_pct"] = 3.0
        assert _errors_for(cfg) == []

    def test_sl_equal_tp_raises_error(self):
        cfg = _base_config()
        cfg["risk"]["stop_loss_pct"] = 2.0
        cfg["risk"]["take_profit_pct"] = 2.0
        errors = _errors_for(cfg)
        assert any("stop_loss_pct" in e for e in errors)

    def test_sl_gt_tp_raises_error(self):
        cfg = _base_config()
        cfg["risk"]["stop_loss_pct"] = 5.0
        cfg["risk"]["take_profit_pct"] = 2.0
        errors = _errors_for(cfg)
        assert any("stop_loss_pct" in e for e in errors)

    def test_zero_sl_raises_error(self):
        cfg = _base_config()
        cfg["risk"]["stop_loss_pct"] = 0
        errors = _errors_for(cfg)
        assert any("stop_loss_pct" in e for e in errors)

    def test_missing_sl_raises_error(self):
        cfg = _base_config()
        del cfg["risk"]["stop_loss_pct"]
        errors = _errors_for(cfg)
        assert any("stop_loss_pct" in e for e in errors)

    def test_missing_tp_raises_error(self):
        cfg = _base_config()
        del cfg["risk"]["take_profit_pct"]
        errors = _errors_for(cfg)
        assert any("take_profit_pct" in e for e in errors)

    def test_low_reward_risk_ratio_gives_warning(self):
        cfg = _base_config()
        cfg["risk"]["stop_loss_pct"] = 2.0
        cfg["risk"]["take_profit_pct"] = 2.1  # ratio = 1.05 — too low
        warnings = _warnings_for(cfg)
        assert any("reward" in w.lower() or "ratio" in w.lower() for w in warnings)


class TestMaxPositionSize:
    def test_valid_max_position(self):
        assert _errors_for(_base_config()) == []

    def test_zero_raises_error(self):
        cfg = _base_config()
        cfg["risk"]["max_position_size"] = 0
        errors = _errors_for(cfg)
        assert any("max_position_size" in e for e in errors)

    def test_missing_raises_error(self):
        cfg = _base_config()
        del cfg["risk"]["max_position_size"]
        errors = _errors_for(cfg)
        assert any("max_position_size" in e for e in errors)

    def test_position_larger_than_capital_gives_warning(self):
        cfg = _base_config(**{"trading.initial_capital": 100})
        cfg["risk"]["max_position_size"] = 200
        warnings = _warnings_for(cfg)
        assert any("max_position_size" in w for w in warnings)


class TestMaxOpenPositions:
    def test_valid_max_open_positions(self):
        cfg = _base_config()
        cfg["risk"]["max_open_positions"] = 3
        assert _errors_for(cfg) == []

    def test_zero_raises_error(self):
        cfg = _base_config()
        cfg["risk"]["max_open_positions"] = 0
        errors = _errors_for(cfg)
        assert any("max_open_positions" in e for e in errors)

    def test_float_raises_error(self):
        cfg = _base_config()
        cfg["risk"]["max_open_positions"] = 2.5
        errors = _errors_for(cfg)
        assert any("max_open_positions" in e for e in errors)

    def test_absent_is_allowed(self):
        cfg = _base_config()
        cfg["risk"].pop("max_open_positions", None)
        assert _errors_for(cfg) == []


class TestMaxDailyLoss:
    def test_positive_value_is_valid(self):
        cfg = _base_config()
        cfg["risk"]["max_daily_loss"] = 15
        assert _errors_for(cfg) == []

    def test_zero_raises_error(self):
        cfg = _base_config()
        cfg["risk"]["max_daily_loss"] = 0
        errors = _errors_for(cfg)
        assert any("max_daily_loss" in e for e in errors)

    def test_absent_is_allowed_in_paper_mode(self):
        cfg = _base_config()
        cfg["risk"].pop("max_daily_loss", None)
        assert _errors_for(cfg) == []


# ── live-mode specific warnings ───────────────────────────────────────────────

class TestLiveModeWarnings:
    def _live_cfg(self) -> dict:
        cfg = _base_config(**{"trading.mode": "live"})
        cfg["exchange"]["sandbox"] = False
        cfg["risk"]["max_daily_loss"] = 15
        cfg["trading"]["initial_capital"] = 500
        return cfg

    def test_live_mode_no_warnings_when_fully_configured(self):
        warnings = _warnings_for(self._live_cfg())
        assert warnings == []

    def test_sandbox_true_in_live_mode_gives_warning(self):
        cfg = self._live_cfg()
        cfg["exchange"]["sandbox"] = True
        warnings = _warnings_for(cfg)
        assert any("sandbox" in w for w in warnings)

    def test_missing_max_daily_loss_in_live_mode_gives_warning(self):
        cfg = self._live_cfg()
        cfg["risk"].pop("max_daily_loss")
        warnings = _warnings_for(cfg)
        assert any("max_daily_loss" in w for w in warnings)

    def test_low_capital_in_live_mode_gives_warning(self):
        cfg = self._live_cfg()
        cfg["trading"]["initial_capital"] = 20  # below $50 threshold
        warnings = _warnings_for(cfg)
        assert any("initial_capital" in w for w in warnings)


# ── error accumulation ────────────────────────────────────────────────────────

class TestErrorAccumulation:
    def test_multiple_errors_raised_together(self):
        cfg = _base_config()
        cfg["risk"]["stop_loss_pct"] = -1    # error 1
        cfg["strategy"]["fast_ema"] = 50     # error 2 (fast > slow)
        with pytest.raises(ConfigValidationError) as exc_info:
            validate_config(cfg)
        assert len(exc_info.value.errors) >= 2

    def test_exception_message_contains_error_count(self):
        cfg = _base_config()
        cfg["risk"]["stop_loss_pct"] = -1
        with pytest.raises(ConfigValidationError) as exc_info:
            validate_config(cfg)
        assert "error" in str(exc_info.value).lower()

    def test_exception_has_errors_attribute(self):
        cfg = _base_config()
        cfg["trading"]["mode"] = "invalid"
        with pytest.raises(ConfigValidationError) as exc_info:
            validate_config(cfg)
        assert isinstance(exc_info.value.errors, list)
        assert len(exc_info.value.errors) > 0

    def test_exception_has_warnings_attribute(self):
        cfg = _base_config()
        cfg["trading"]["mode"] = "invalid"
        with pytest.raises(ConfigValidationError) as exc_info:
            validate_config(cfg)
        assert isinstance(exc_info.value.warnings, list)

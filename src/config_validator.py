"""
Startup configuration validator for the crypto scalping bot.

Called by bot.py before any exchange connection is opened.  All critical
problems are collected into a list and raised together as a single
ConfigValidationError so the operator sees every issue at once rather than
fixing them one by one.

Severity levels
---------------
ERROR  – bot cannot start safely; raised immediately.
WARNING – bot can start but the setting is unusual or risky.
"""

from __future__ import annotations

from typing import List, Tuple


VALID_MODES = {"paper", "backtest", "live"}
VALID_TIMEFRAMES = {"1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h", "6h", "12h", "1d"}


class ConfigValidationError(Exception):
    """Raised when one or more config fields fail validation.

    ``errors`` contains every blocking error message; ``warnings`` contains
    non-blocking advisory messages that are logged but do not prevent startup.
    """

    def __init__(self, errors: List[str], warnings: List[str] | None = None):
        self.errors = errors
        self.warnings = warnings or []
        bullet = "\n  • "
        msg = f"Config validation failed ({len(errors)} error(s)):{bullet}{bullet.join(errors)}"
        if self.warnings:
            msg += f"\nWarnings:{bullet}{bullet.join(self.warnings)}"
        super().__init__(msg)


def validate_config(config: dict) -> List[str]:
    """Validate *config* and return a (possibly empty) list of warning strings.

    Raises ConfigValidationError on any blocking error so the bot never starts
    with a bad configuration.  Non-blocking issues are returned as warnings and
    should be logged by the caller.

    Parameters
    ----------
    config:
        Dict loaded from config.yaml via yaml.safe_load().

    Returns
    -------
    list of warning strings (empty if no warnings).
    """
    errors: List[str] = []
    warnings: List[str] = []

    def _e(msg: str) -> None:
        errors.append(msg)

    def _w(msg: str) -> None:
        warnings.append(msg)

    # ── trading section ──────────────────────────────────────────────────────

    trading = config.get("trading")
    if not isinstance(trading, dict):
        _e("Missing required section: trading")
        # Can't validate sub-fields without the section; bail early on this block.
        trading = {}

    mode = trading.get("mode", "")
    if mode not in VALID_MODES:
        _e(
            f"trading.mode must be one of {sorted(VALID_MODES)}, got: {mode!r}. "
            "Check config.yaml — a typo here silently disables live trading."
        )

    capital = trading.get("initial_capital")
    if capital is None:
        _e("trading.initial_capital is required")
    elif not isinstance(capital, (int, float)):
        _e(f"trading.initial_capital must be a number, got {type(capital).__name__}")
    elif capital <= 0:
        _e(f"trading.initial_capital must be > 0, got {capital}")
    elif capital < 10:
        _w(f"trading.initial_capital={capital} is very small; minimum viable is ~$10")

    pairs = trading.get("pairs")
    if not pairs:
        _e("trading.pairs must be a non-empty list")
    elif not isinstance(pairs, list):
        _e(f"trading.pairs must be a list, got {type(pairs).__name__}")
    else:
        for p in pairs:
            if not isinstance(p, str) or "/" not in p:
                _e(f"trading.pairs: invalid symbol {p!r} — expected format 'BASE/QUOTE' e.g. 'BTC/USD'")

    tf = trading.get("timeframe", "1m")
    if tf not in VALID_TIMEFRAMES:
        _e(f"trading.timeframe {tf!r} is not supported. Valid values: {sorted(VALID_TIMEFRAMES)}")

    # ── strategy section ─────────────────────────────────────────────────────

    strategy = config.get("strategy", {})
    if not isinstance(strategy, dict):
        _e("strategy section must be a mapping")
        strategy = {}

    fast_ema = strategy.get("fast_ema")
    slow_ema = strategy.get("slow_ema")

    if fast_ema is not None and not isinstance(fast_ema, int):
        _e(f"strategy.fast_ema must be an integer, got {type(fast_ema).__name__}")
    elif isinstance(fast_ema, int) and fast_ema <= 0:
        _e(f"strategy.fast_ema must be > 0, got {fast_ema}")

    if slow_ema is not None and not isinstance(slow_ema, int):
        _e(f"strategy.slow_ema must be an integer, got {type(slow_ema).__name__}")
    elif isinstance(slow_ema, int) and slow_ema <= 0:
        _e(f"strategy.slow_ema must be > 0, got {slow_ema}")

    if isinstance(fast_ema, int) and isinstance(slow_ema, int) and fast_ema >= slow_ema:
        _e(
            f"strategy.fast_ema ({fast_ema}) must be strictly less than "
            f"strategy.slow_ema ({slow_ema}) — EMA crossover logic requires fast < slow"
        )

    rsi_period = strategy.get("rsi_period")
    if rsi_period is not None:
        if not isinstance(rsi_period, int):
            _e(f"strategy.rsi_period must be an integer, got {type(rsi_period).__name__}")
        elif rsi_period <= 0:
            _e(f"strategy.rsi_period must be > 0, got {rsi_period}")

    rsi_ob = strategy.get("rsi_overbought")
    rsi_os = strategy.get("rsi_oversold")
    if isinstance(rsi_ob, (int, float)) and isinstance(rsi_os, (int, float)):
        if rsi_ob <= rsi_os:
            _e(
                f"strategy.rsi_overbought ({rsi_ob}) must be > rsi_oversold ({rsi_os})"
            )
        if not (50 <= rsi_ob <= 100):
            _w(f"strategy.rsi_overbought={rsi_ob} is outside the typical 50-100 range")
        if not (0 <= rsi_os <= 50):
            _w(f"strategy.rsi_oversold={rsi_os} is outside the typical 0-50 range")

    # ── risk section ─────────────────────────────────────────────────────────

    risk = config.get("risk", {})
    if not isinstance(risk, dict):
        _e("risk section must be a mapping")
        risk = {}

    sl_pct = risk.get("stop_loss_pct")
    tp_pct = risk.get("take_profit_pct")

    if sl_pct is None:
        _e("risk.stop_loss_pct is required")
    elif not isinstance(sl_pct, (int, float)) or sl_pct <= 0:
        _e(f"risk.stop_loss_pct must be a positive number, got {sl_pct!r}")

    if tp_pct is None:
        _e("risk.take_profit_pct is required")
    elif not isinstance(tp_pct, (int, float)) or tp_pct <= 0:
        _e(f"risk.take_profit_pct must be a positive number, got {tp_pct!r}")

    if (
        isinstance(sl_pct, (int, float)) and sl_pct > 0
        and isinstance(tp_pct, (int, float)) and tp_pct > 0
    ):
        if sl_pct >= tp_pct:
            _e(
                f"risk.stop_loss_pct ({sl_pct}) must be < risk.take_profit_pct ({tp_pct}) "
                "— a reward:risk ratio below 1:1 is unprofitable over time"
            )
        rr = tp_pct / sl_pct
        if rr < 1.2:
            _w(
                f"Reward:risk ratio is {rr:.2f}x (TP {tp_pct}% / SL {sl_pct}%). "
                "Typical scalping targets ≥ 1.5x."
            )

    max_pos = risk.get("max_position_size")
    if max_pos is None:
        _e("risk.max_position_size is required")
    elif not isinstance(max_pos, (int, float)) or max_pos <= 0:
        _e(f"risk.max_position_size must be a positive number, got {max_pos!r}")
    elif isinstance(capital, (int, float)) and capital > 0 and max_pos > capital:
        _w(
            f"risk.max_position_size ({max_pos}) > trading.initial_capital ({capital}) — "
            "a single trade could exceed total account equity"
        )

    max_open = risk.get("max_open_positions")
    if max_open is not None:
        if not isinstance(max_open, int) or max_open <= 0:
            _e(f"risk.max_open_positions must be a positive integer, got {max_open!r}")

    max_daily_loss = risk.get("max_daily_loss")
    if max_daily_loss is not None:
        if not isinstance(max_daily_loss, (int, float)) or max_daily_loss <= 0:
            _e(f"risk.max_daily_loss must be a positive number, got {max_daily_loss!r}")

    # ── live-mode specific checks ────────────────────────────────────────────

    if mode == "live":
        exchange = config.get("exchange", {})
        if exchange.get("sandbox") is True:
            _w(
                "trading.mode=live but exchange.sandbox=true — "
                "the bot will connect to Kraken's sandbox (no real orders). "
                "Set exchange.sandbox: false for real trading."
            )

        if max_daily_loss is None:
            _w(
                "risk.max_daily_loss is not set. In live mode this means the bot "
                "has no daily drawdown circuit-breaker. Strongly recommended."
            )

        if isinstance(capital, (int, float)) and capital < 50:
            _w(
                f"trading.initial_capital={capital} is low for live trading. "
                "Minimum fees and minimum order sizes may prevent execution."
            )

    # ── raise if any blocking errors ─────────────────────────────────────────

    if errors:
        raise ConfigValidationError(errors, warnings)

    return warnings

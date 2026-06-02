"""AlpacaTJRBot — main orchestrator."""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, date
from typing import Dict, Optional

import pandas as pd
import pytz
import yaml

from .data.feed import AlpacaFeed
from .execution.broker import AlpacaBroker
from .execution.position_manager import PositionManager, OpenPosition
from .execution.risk import DailyCircuit, size_position
from .strategy.confluence import TJRConfluence
from .strategy.levels import KeyLevels, build_key_levels
from .strategy.sessions import (
    PremarketRange, is_premarket, is_tradeable, force_close_time, current_session
)
from .utils.candles import BarBuffer
from .utils.journal import TradeJournal
from .utils.notifications import Notifier

logger = logging.getLogger(__name__)

ET = pytz.timezone("US/Eastern")
POSITION_CHECK_INTERVAL = 5.0   # seconds
STRATEGY_CHECK_INTERVAL = 1.0   # seconds (actual eval gated by new-bar flag)


class AlpacaTJRBot:
    def __init__(self, config_path: str = "alpaca_tjr/config.yaml"):
        self._cfg = self._load_config(config_path)
        self._symbols: list[str] = self._cfg["trading"]["symbols"]

        paper = self._cfg["alpaca"].get("paper", True)
        api_key = os.environ.get("ALPACA_API_KEY", "")
        secret_key = os.environ.get("ALPACA_SECRET_KEY", "")

        if not api_key or not secret_key:
            raise RuntimeError(
                "\nAlpaca API keys not found.\n"
                "1. Sign up free at https://alpaca.markets\n"
                "2. Go to Paper Trading → API Keys\n"
                "3. Run:\n"
                "   export ALPACA_API_KEY=your_key\n"
                "   export ALPACA_SECRET_KEY=your_secret\n"
            )

        self._broker = AlpacaBroker(api_key, secret_key, paper=paper)
        self._feed = AlpacaFeed(api_key, secret_key, self._symbols)

        risk_cfg = self._cfg["risk"]
        self._circuit = DailyCircuit(
            max_daily_loss_pct=risk_cfg["max_daily_loss_pct"],
            max_trades_per_day=risk_cfg.get("max_trades_per_day", 6),
            max_open_positions=risk_cfg["max_open_positions"],
            cooldown_after_loss_sec=risk_cfg["cooldown_after_loss_sec"],
        )

        tg = self._cfg.get("telegram", {})
        self._notifier = Notifier(
            token=tg.get("token", ""),
            chat_id=tg.get("chat_id", ""),
            enabled=tg.get("enabled", False),
        )

        self._journal = TradeJournal()
        self._positions = PositionManager(
            self._broker, self._circuit, self._journal, self._notifier
        )

        strategy_cfg = self._cfg["strategy"]
        self._strategies: Dict[str, TJRConfluence] = {
            sym: TJRConfluence(
                symbol=sym,
                sweep_lookback=strategy_cfg["sweep_lookback_bars"],
                bos_lookback=strategy_cfg["bos_lookback_bars"],
                swing_n=strategy_cfg["swing_n"],
                impulse_body_ratio=strategy_cfg["impulse_body_ratio"],
                fvg_max_age=strategy_cfg["fvg_max_age_bars"],
                ob_max_age=strategy_cfg["ob_max_age_bars"],
                htf_sma_period=strategy_cfg["htf_sma_period"],
                htf_neutral_band=strategy_cfg["htf_neutral_band"],
                min_rr=risk_cfg["min_rr"],
                min_pm_range_pct=strategy_cfg["min_pm_range_pct"],
            )
            for sym in self._symbols
        }

        self._bar_buffer = BarBuffer(max_bars=500)
        self._pm_ranges: Dict[str, PremarketRange] = {s: PremarketRange() for s in self._symbols}
        self._key_levels: Dict[str, Optional[KeyLevels]] = {s: None for s in self._symbols}
        self._daily_bars: Dict[str, pd.DataFrame] = {s: pd.DataFrame() for s in self._symbols}
        self._new_bar_flags: Dict[str, bool] = {s: False for s in self._symbols}
        self._opening_prices: Dict[str, Optional[float]] = {s: None for s in self._symbols}

        self._force_close_sent = False

    @staticmethod
    def _load_config(path: str) -> dict:
        with open(path) as f:
            return yaml.safe_load(f)

    # ── Startup ──────────────────────────────────────────────────────────────

    def _bootstrap(self) -> None:
        """Load historical data, initialize circuit breaker."""
        logger.info("Bootstrapping bot for symbols: %s", self._symbols)
        acct = self._broker.get_account()
        self._circuit.start_day(acct.equity)
        logger.info("Account equity: %.2f", acct.equity)

        for sym in self._symbols:
            # Load daily bars for HTF bias (last 60 days)
            daily = self._feed.load_historical(sym, "1Day", days_back=60)
            self._daily_bars[sym] = daily
            logger.info("[%s] Loaded %d daily bars", sym, len(daily))

            # Seed the 1-minute buffer with recent history (last 2 days)
            hist_1m = self._feed.load_historical(sym, "1Min", days_back=2)
            if not hist_1m.empty:
                for ts, row in hist_1m.iterrows():
                    row.name = ts
                    self._bar_buffer.update(sym, row)
                logger.info("[%s] Seeded bar buffer with %d 1m bars", sym, len(hist_1m))

            # Build pre-market range from today's pre-market bars
            now_et = datetime.now(ET)
            pm = PremarketRange()
            pm.reset(now_et.date())
            today_bars = hist_1m[hist_1m.index.date == now_et.date()] if not hist_1m.empty else pd.DataFrame()
            if not today_bars.empty:
                pm_mask = today_bars.index.time < __import__("datetime").time(9, 30)
                pm_bars = today_bars[pm_mask]
                if not pm_bars.empty:
                    pm.update(float(pm_bars["high"].max()), float(pm_bars["low"].min()))
            self._pm_ranges[sym] = pm

        self._feed.add_bar_callback(self._on_bar)

    # ── Bar handler (called by WebSocket) ────────────────────────────────────

    async def _on_bar(self, symbol: str, bar: pd.Series) -> None:
        self._bar_buffer.update(symbol, bar)
        self._new_bar_flags[symbol] = True

        # Track pre-market range
        now_et = datetime.now(ET)
        if is_premarket(now_et):
            pm = self._pm_ranges.get(symbol)
            if pm:
                if pm.date != now_et.date():
                    pm.reset(now_et.date())
                pm.update(float(bar["high"]), float(bar["low"]))

        # Capture opening price at first 9:30 bar
        if now_et.hour == 9 and now_et.minute == 30 and \
                self._opening_prices.get(symbol) is None:
            self._opening_prices[symbol] = float(bar["open"])
            self._refresh_levels(symbol, float(bar["open"]))

        logger.debug("[%s] New bar: close=%.4f", symbol, bar["close"])

    def _refresh_levels(self, symbol: str, opening_price: float) -> None:
        """Build KeyLevels at market open (9:30 ET)."""
        pm = self._pm_ranges.get(symbol)
        daily = self._daily_bars.get(symbol, pd.DataFrame())
        # Previous day = last full daily bar (yesterday)
        prev_day = daily.iloc[:-1] if len(daily) > 1 else daily

        from .utils.candles import compute_vwap
        vwap = compute_vwap(self._bar_buffer.get_1m(symbol))

        self._key_levels[symbol] = build_key_levels(
            prev_day_bars=prev_day,
            pm_high=pm.high if (pm and pm.valid) else 0.0,
            pm_low=pm.low if (pm and pm.valid) else 0.0,
            vwap=vwap,
            opening_price=opening_price,
        )
        logger.info("[%s] Key levels: %s", symbol, self._key_levels[symbol])

    # ── Strategy loop ─────────────────────────────────────────────────────────

    async def _strategy_loop(self) -> None:
        """Evaluate confluence on each new bar during tradeable sessions."""
        while True:
            await asyncio.sleep(STRATEGY_CHECK_INTERVAL)

            if not is_tradeable():
                self._new_bar_flags = {s: False for s in self._symbols}
                continue

            for symbol in self._symbols:
                if not self._new_bar_flags.get(symbol):
                    continue
                self._new_bar_flags[symbol] = False

                if self._positions.has_position(symbol):
                    continue

                acct = self._broker.get_account()
                allowed, reason = self._circuit.ok(acct.equity)
                if not allowed:
                    logger.debug("[%s] Circuit blocked: %s", symbol, reason)
                    continue

                bars_5m = self._bar_buffer.get_5m(symbol)
                daily = self._daily_bars.get(symbol, pd.DataFrame())
                levels = self._key_levels.get(symbol)

                if len(bars_5m) < 20:
                    continue

                # Update VWAP in levels if available
                if levels is not None:
                    from .utils.candles import compute_vwap
                    levels.vwap = compute_vwap(self._bar_buffer.get_1m(symbol))

                signal = self._strategies[symbol].evaluate(
                    bars_5m=bars_5m,
                    daily_bars=daily,
                    levels=levels,
                    now=datetime.now(ET),
                )

                if signal is None:
                    continue

                risk_cfg = self._cfg["risk"]
                qty = size_position(
                    equity=acct.equity,
                    risk_pct=risk_cfg["risk_per_trade_pct"],
                    entry=signal.entry_price,
                    stop=signal.stop_price,
                )
                if qty <= 0:
                    continue

                try:
                    result = self._broker.place_bracket_order(
                        symbol=symbol,
                        qty=qty,
                        side=signal.direction,
                        limit_price=signal.entry_price,
                        stop_price=signal.stop_price,
                        take_profit_price=signal.target_price,
                    )
                    pos = OpenPosition(
                        symbol=symbol,
                        side=signal.direction,
                        entry_price=signal.entry_price,
                        qty=qty,
                        stop_price=signal.stop_price,
                        target_price=signal.target_price,
                        setup_type=signal.setup_type,
                        session=signal.session,
                        sweep_level=signal.sweep.level_name,
                        order_id=result.order_id,
                        entry_time=datetime.utcnow(),
                    )
                    self._positions.add(pos)
                    self._notifier.entry(
                        symbol, signal.direction, signal.entry_price,
                        signal.stop_price, signal.target_price, qty, signal.setup_type,
                    )
                except Exception as exc:
                    logger.error("[%s] Failed to place order: %s", symbol, exc)

    # ── Position management loop ──────────────────────────────────────────────

    async def _position_loop(self) -> None:
        """Check SL/TP for each open position every few seconds."""
        while True:
            await asyncio.sleep(POSITION_CHECK_INTERVAL)
            try:
                alpaca_positions = {
                    p.symbol: p for p in self._broker.get_open_positions()
                }
                for symbol in self._positions.symbols_with_positions:
                    ap = alpaca_positions.get(symbol)
                    if ap:
                        self._positions.update(symbol, float(ap.current_price))
            except Exception as exc:
                logger.error("Position loop error: %s", exc)

    # ── EOD loop ─────────────────────────────────────────────────────────────

    async def _eod_loop(self) -> None:
        """Close all positions at 15:45 ET and send daily summary."""
        while True:
            await asyncio.sleep(30)
            now_et = datetime.now(ET)
            close_time = force_close_time(now_et.date())

            if not self._force_close_sent and now_et >= close_time:
                logger.info("EOD force-close triggered")
                self._positions.close_all(reason="eod")
                stats = self._journal.daily_stats()
                acct = self._broker.get_account()
                self._notifier.daily_summary(
                    trades=stats["trades"],
                    wins=stats["wins"],
                    pnl=stats["pnl"],
                    equity=acct.equity,
                )
                logger.info(
                    "Daily summary: %d trades, %d wins, P&L=%.2f",
                    stats["trades"], stats["wins"], stats["pnl"],
                )
                self._journal.reset_daily()
                self._force_close_sent = True

            # Reset flag at midnight for the new day
            if now_et.hour == 0 and now_et.minute < 1:
                self._force_close_sent = False
                for sym in self._symbols:
                    self._opening_prices[sym] = None
                    self._key_levels[sym] = None

    # ── Run ──────────────────────────────────────────────────────────────────

    def run(self) -> None:
        """Start the bot (blocking)."""
        self._bootstrap()
        logger.info("Starting AlpacaTJRBot — paper=%s symbols=%s",
                    self._cfg["alpaca"].get("paper"), self._symbols)
        self._notifier.alert("AlpacaTJRBot started (paper mode)")

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        async def _main():
            await asyncio.gather(
                self._feed.run_stream_async(),
                self._strategy_loop(),
                self._position_loop(),
                self._eod_loop(),
            )

        try:
            loop.run_until_complete(_main())
        except KeyboardInterrupt:
            logger.info("Shutting down — closing all positions")
            self._positions.close_all(reason="manual")
        finally:
            loop.close()

#!/usr/bin/env python3
"""
Master Bot Runner
Launches the scalping bot (src.bot.ScalpingBot → paper_trading session, which
also runs the funding-arb arms) plus the dashboard. DEX/stablecoin arb are
wired but disabled below (Binance/Bybit geo-blocked for US).
"""

import sys
import logging
from types import ModuleType as _ModuleType

# Stub numba so pandas-ta loads on Python 3.14 (numba only supports up to 3.13)
if 'numba' not in sys.modules:
    _numba = _ModuleType('numba')
    _numba.njit = lambda *a, **kw: (a[0] if a and callable(a[0]) else lambda f: f)
    sys.modules['numba'] = _numba

# Configure logging with UTF-8 before any imports so sub-modules inherit it
import os as _os
_os.makedirs('logs', exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(
            stream=open(sys.stderr.fileno(), mode='w', encoding='utf-8', closefd=False)
        ),
        logging.FileHandler('logs/bot.log', encoding='utf-8'),
    ]
)
logger = logging.getLogger(__name__)

import asyncio
import signal
from pathlib import Path

# Add src to path
sys.path.insert(0, str(str(Path(__file__).parent / "src")))
sys.path.insert(0, str(str(Path(__file__).parent / "arbitrage")))

from src.bot import ScalpingBot
from src.dashboard import run_dashboard
from src.notifications import create_notifier_from_env
from arbitrage.dex_arb import DEXArbitrageBot, Chain
from arbitrage.stablecoin_arb import StablecoinArbBot
# NOTE: funding-rate arb runs inside the paper_trading session (two cost-aware
# arms), not here — the old standalone FundingRateArbBot stub was removed.


class MasterBotRunner:
    """Runs all trading bots together"""

    def __init__(self, config: dict):
        self.config = config
        self.running = False

        # Initialize bots
        self.scalping_bot = None
        self.dex_arb_bot = None
        self.stablecoin_arb_bot = None

        # asyncio only holds a WEAK reference to a Task once you discard the
        # `asyncio.create_task(...)` return value — the task can then be
        # garbage-collected mid-run with no error, no log line, no Telegram
        # alert (see the "Save a reference to the result" warning in the
        # asyncio docs). Every background task this runner starts gets parked
        # here so it lives for the lifetime of the runner instead of the
        # lifetime of whichever local variable briefly held it.
        self._background_tasks: list = []

    def _spawn(self, coro, *, name: str = None) -> asyncio.Task:
        """asyncio.create_task() that also keeps a strong reference, so the
        task can't be silently garbage-collected mid-run (see __init__)."""
        t = asyncio.create_task(coro, name=name)
        self._background_tasks.append(t)
        t.add_done_callback(self._background_tasks.remove)
        return t

    async def _check_critical_tasks(
        self,
        critical_tasks: list,
        notifier,
    ) -> None:
        """Check each critical task for unexpected exit.

        If a task crashed (stored exception), sends a Telegram alert then
        re-raises as RuntimeError so asyncio.run() propagates it to the OS.
        systemd's Restart=on-failure then restarts the whole service.

        If a long-running task returned cleanly (shouldn't happen), logs a
        warning and notifies but does NOT raise — that may be intentional
        during shutdown.
        """
        for name, task in critical_tasks:
            if not task.done():
                continue
            if task.cancelled():
                logger.warning("[Runner] Task '%s' was cancelled", name)
                continue
            exc = task.exception()
            if exc is not None:
                msg = (
                    f"⛔ Bot task '{name}' died: "
                    f"{type(exc).__name__}: {exc}"
                )
                logger.critical(msg, exc_info=exc)
                try:
                    await notifier.send(msg)
                except Exception:
                    pass
                raise RuntimeError(msg) from exc
            # Unexpected clean exit from a long-running task
            msg = (
                f"⚠️ Bot task '{name}' exited without error — "
                "this is unexpected for a long-running bot."
            )
            logger.warning(msg)
            try:
                await notifier.send(msg)
            except Exception:
                pass

    async def start(self):
        """Start all bots"""
        self.running = True
        logger.info("🚀 Starting Master Bot Runner...")

        # Setup signal handlers (add_signal_handler is Unix-only)
        loop = asyncio.get_event_loop()
        try:
            for sig in (signal.SIGTERM, signal.SIGINT):
                loop.add_signal_handler(
                    sig, lambda: self._spawn(self.shutdown(), name="shutdown"))
        except NotImplementedError:
            pass  # Windows: handled by KeyboardInterrupt in main()

        notifier = create_notifier_from_env()

        # Critical tasks are monitored every 30 s — if one dies the process
        # crashes so systemd can restart it cleanly.
        _critical_tasks: list = []

        # Start scalping bot (mode from config.yaml: paper or live)
        if self.config.get("scalping", {}).get("enabled", True):
            logger.info("Starting scalping bot...")
            self.scalping_bot = ScalpingBot(self.config)
            t = asyncio.create_task(self.scalping_bot.start(), name="scalping_bot")
            _critical_tasks.append(("scalping_bot", t))

        # Start DEX arb
        if self.config.get("dex_arb", {}).get("enabled", True):
            logger.info("Starting DEX arb bot...")
            self.dex_arb_bot = DEXArbitrageBot(
                chain=Chain.SOLANA,
                min_spread_pct=self.config.get("dex_arb", {}).get("min_spread", 0.5),
                trade_size_usd=self.config.get("dex_arb", {}).get("trade_size", 100)
            )
            self._spawn(self.dex_arb_bot.start(), name="dex_arb_bot")

        # Start stablecoin arb
        if self.config.get("stablecoin_arb", {}).get("enabled", True):
            logger.info("Starting stablecoin arb bot...")
            self.stablecoin_arb_bot = StablecoinArbBot(
                exchanges=self.config.get("stablecoin_arb", {}).get("exchanges", ["kraken"]),
                min_profit_pct=self.config.get("stablecoin_arb", {}).get("min_profit", 0.1),
                trade_size_usd=self.config.get("stablecoin_arb", {}).get("trade_size", 500)
            )
            self._spawn(self.stablecoin_arb_bot.start(), name="stablecoin_arb_bot")

        # Start alt-perp confluence strategy (src/altperp/). PAPER-only by design
        # — directional edge unproven per research (memory: altperp-strategy), so
        # this is forward-walk data collection alongside the funding arb. The
        # package's orders.py refuses to send live orders without a Kraken exec
        # client; leave that gate in place until edge is measured.
        altperp_cfg = self.config.get("altperp", {})
        if altperp_cfg.get("enabled", False):
            ai_on = bool(altperp_cfg.get("ai_brain_enabled", False))
            # Set env BEFORE importing the runner — altperp.config reads these at
            # module load time. setdefault so a hand-set .env value still wins.
            _os.environ.setdefault("ALTPERP_AI_ENABLED", "1" if ai_on else "0")
            _os.environ.setdefault("ALTPERP_PAPER", "1")
            logger.info("Starting altperp strategy (paper, AI gate-keeper=%s)...",
                        "on" if ai_on else "off")
            from src.altperp.runner import run as altperp_run
            altperp_notifier = create_notifier_from_env()
            self._spawn(altperp_run(notifier=altperp_notifier), name="altperp")

        logger.info("All bots started. Dashboard at http://localhost:8080  |  Press Ctrl+C to stop.")

        # Start dashboard server alongside bots
        self._spawn(run_dashboard(host="0.0.0.0", port=8080), name="dashboard")

        # Keep running; check critical task health every 30 s.
        while self.running:
            await asyncio.sleep(30)
            await self._check_critical_tasks(_critical_tasks, notifier)

    async def shutdown(self):
        """Gracefully shutdown all bots"""
        logger.info("🛑 Shutting down...")
        self.running = False

        if self.dex_arb_bot:
            await self.dex_arb_bot.stop()
        if self.stablecoin_arb_bot:
            await self.stablecoin_arb_bot.stop()

        logger.info("All bots stopped.")


def main():
    # Load config
    import yaml
    config_path = Path(__file__).parent / "config.yaml"

    if config_path.exists():
        with open(config_path) as f:
            config = yaml.safe_load(f)
    else:
        config = {}

    # DEX/stablecoin arb disabled — Binance/Bybit are geo-blocked for US users.
    # (Funding arb runs inside the paper_trading session, gated by its own env var.)
    config.setdefault("dex_arb", {})["enabled"] = False
    config.setdefault("stablecoin_arb", {})["enabled"] = False

    runner = MasterBotRunner(config)

    try:
        asyncio.run(runner.start())
    except KeyboardInterrupt:
        pass


if __name__ == '__main__':
    main()

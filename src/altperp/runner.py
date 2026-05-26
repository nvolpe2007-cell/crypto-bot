"""
Alt-perp strategy runner — the 5-minute evaluation loop.

For each coin: fetch Bybit market data → compute signals → confluence → log to
DB → manage open position's exits OR open a new one. Designed to run as a
background arm alongside the funding-arb majors arm. PAPER only until a Kraken
execution client is wired (orders.py refuses live).

`evaluate_and_act` is split out so it can be unit-tested with injected market
data (no network).
"""

import asyncio
import logging
from datetime import datetime, timezone
from typing import Dict, Optional

from . import config, database, time_utils
from .signals import (funding_signal, oi_signal, cvd_signal,
                      liq_proximity_signal, trend_signal)
from .math_utils import is_volume_spike
from .confluence import evaluate as evaluate_confluence
from .position_sizing import compute_size
from .orders import PaperExecutor
from .position_manager import PositionManager
from .telegram import AltperpAlerter

logger = logging.getLogger(__name__)


def evaluate_and_act(coin: str, market: Dict, pm: PositionManager,
                     btc_uptrend_ok: bool, now: datetime, db_path: Optional[str] = None):
    """Evaluate one coin and act (manage/open). Logs the evaluation regardless.
    Pure w.r.t. I/O except the DB log + pm side effects — unit-testable with a
    mock `market` dict."""
    price = market["price"]
    f = funding_signal(market["funding_rate"], market.get("funding_history", []))
    oi = oi_signal(market.get("oi_points", []), price)
    cvd = cvd_signal(market.get("perp_trades", []), market.get("spot_trades", []))
    liq = liq_proximity_signal(market.get("orderbook") or {}, price)
    trend = trend_signal(market.get("klines", []))

    vols = [c["volume"] for c in market.get("klines", [])]
    vol_spike = is_volume_spike(vols[-1], vols[-21:-1], config.VOLUME_SPIKE_MULTIPLIER) \
        if len(vols) > 1 else False
    mins = time_utils.get_minutes_to_next_funding_reset(now)
    post_block = time_utils.in_post_funding_block(now)

    setup = evaluate_confluence(coin, f, oi, cvd, liq, trend, vol_spike,
                                btc_uptrend_ok, mins, post_block)

    fired = 0
    if coin in pm.positions:
        pm.on_tick(coin, price, market["funding_rate"], oi.get("oi_4hr_change"), now)
    elif setup.should_enter:
        ok, reason = pm.can_open(coin, now)
        if ok:
            plan = compute_size(pm.equity, price, setup.direction, setup.setup_type, setup.size_multiplier)
            if plan:
                pm.open_position(setup, plan, price, now)
                fired = 1
        else:
            logger.debug("[ALTPERP] %s setup but can't open: %s", coin, reason)

    database.log_signal({
        "coin": coin, "price": price,
        "funding_rate": f["funding_rate"], "funding_rate_48hr_avg": f["funding_48hr_avg"],
        "oi_current": oi["oi_current_usd"], "oi_4hr_change_pct": oi["oi_4hr_change"],
        "oi_8hr_change_pct": oi["oi_8hr_change"],
        "perp_cvd_4hr": cvd["perp_cvd"], "spot_cvd_4hr": cvd["spot_cvd"],
        "cvd_divergence": int(setup.cvd_confirmed), "liq_proximity": int(setup.liq_proximity),
        "tier1_triggered": int(setup.tier1_ok), "tier2_score": setup.tier2_score,
        "minutes_to_funding_reset": mins, "setup_type": setup.setup_type, "trade_fired": fired,
    }, db_path=db_path)
    return setup


async def _fetch_market(dc, coin: str) -> Optional[Dict]:
    """Fetch all data one coin's evaluation needs from Bybit public API."""
    fn = await dc.funding_now(coin)
    if not fn or not fn.get("price"):
        return None
    return {
        "price": fn["price"],
        "funding_rate": fn["funding_rate"],
        "funding_history": await dc.funding_history(coin),
        "oi_points": await dc.open_interest(coin),
        "perp_trades": await dc.recent_trades(coin, category="linear"),
        "spot_trades": await dc.recent_trades(coin, category="spot"),
        "orderbook": await dc.orderbook(coin),
        "klines": await dc.klines(coin, interval="240", limit=60),
    }


def startup_checks(pm: PositionManager, alerter: AltperpAlerter) -> bool:
    """Validate config + DB + announce. Returns False to abort."""
    problems = config.validate()
    if problems:
        logger.error("[ALTPERP] config invalid: %s", problems)
        return False
    database.init_db(pm.db_path)
    mode = "PAPER" if config.PAPER_TRADING else "LIVE"
    logger.info("[ALTPERP] startup OK — %s mode, coins=%s, live-exec=%s, equity=$%.2f",
                mode, config.TARGET_COINS, config.LIVE_EXECUTION_COINS, pm.equity)
    if not config.PAPER_TRADING:
        logger.warning("[ALTPERP] LIVE mode set but execution is not implemented — "
                       "orders will refuse. Set ALTPERP_PAPER=1.")
    alerter._send(f"🤖 Alt-perp strategy started ({mode}) — watching {', '.join(config.TARGET_COINS)}")
    return True


async def run(notifier=None):
    """Main loop. `notifier` is an optional TelegramNotifier (reused from the bot)."""
    from .data import BybitData
    dc = BybitData()
    executor = PaperExecutor()
    alerter = AltperpAlerter(notifier=notifier)
    pm = PositionManager(executor, alerter=alerter)

    if not startup_checks(pm, alerter):
        await dc.close()
        return

    last_summary_day = datetime.now(timezone.utc).date()
    try:
        while True:
            now = datetime.now(timezone.utc)
            # BTC trend gate for flush longs
            btc_uptrend_ok = True
            try:
                btc_k = await dc.klines(config.BTC_REF_SYMBOL, interval="60", limit=60)
                btc_uptrend_ok = not trend_signal(btc_k).get("strong_downtrend", False)
            except Exception as e:
                logger.debug("[ALTPERP] BTC trend fetch failed: %s", e)

            for coin in config.TARGET_COINS:
                try:
                    market = await _fetch_market(dc, coin)
                    if market:
                        evaluate_and_act(coin, market, pm, btc_uptrend_ok, now)
                except Exception as e:
                    logger.warning("[ALTPERP] %s loop error: %s", coin, e)
                    alerter.error(coin, e)

            # daily summary at UTC date rollover
            if now.date() != last_summary_day:
                last_summary_day = now.date()
                day_iso = datetime(now.year, now.month, now.day, tzinfo=timezone.utc).isoformat()
                try:
                    stats = database.closed_stats_since(day_iso, db_path=pm.db_path)
                    alerter.daily_summary(stats, pm.equity)
                except Exception as e:
                    logger.debug("[ALTPERP] daily summary failed: %s", e)

            await asyncio.sleep(config.SIGNAL_LOOP_INTERVAL_SECS)
    finally:
        await dc.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
    asyncio.run(run())

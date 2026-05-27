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

from . import config, database, time_utils, regime as rg, router, trend, mean_reversion
from .signals import (funding_signal, oi_signal, cvd_signal,
                      liq_proximity_signal, trend_signal,
                      funding_dynamics_signal, microstructure_signal)
from .math_utils import is_volume_spike
from .confluence import evaluate as evaluate_confluence
from .position_sizing import compute_size
from .orders import PaperExecutor
from .position_manager import PositionManager
from .telegram import AltperpAlerter

logger = logging.getLogger(__name__)


def evaluate_and_act(coin: str, market: Dict, pm: PositionManager,
                     btc_uptrend_ok: bool, now: datetime, db_path: Optional[str] = None,
                     brain=None):
    """Regime-routed evaluation for one coin. Classify regime → ask the matched
    strategy → route → act. Manages an open position regardless of regime. Logs
    every evaluation. Unit-testable with a mock `market` dict.

    `brain`: optional AIBrain. When provided, fade/flush setups that pass the
    structural gate are submitted to it as a gate-keeper (confirm/veto/size); it
    can never open a setup the gate rejected. When None, the rule engine decides
    alone (the original behavior — used by the offline tests)."""
    price = market["price"]
    klines = market.get("klines", [])
    f = funding_signal(market["funding_rate"], market.get("funding_history", []))
    oi = oi_signal(market.get("oi_points", []), price)
    cvd = cvd_signal(market.get("perp_trades", []), market.get("spot_trades", []))
    liq = liq_proximity_signal(market.get("orderbook") or {}, price)
    tsig = trend_signal(klines)
    fdyn = funding_dynamics_signal(market["funding_rate"], market.get("funding_history", []))
    micro = microstructure_signal(market.get("perp_trades", []), klines,
                                  market.get("spot_klines", []))
    vols = [c["volume"] for c in klines]
    vol_spike = is_volume_spike(vols[-1], vols[-21:-1], config.VOLUME_SPIKE_MULTIPLIER) \
        if len(vols) > 1 else False
    mins = time_utils.get_minutes_to_next_funding_reset(now)
    post_block = time_utils.in_post_funding_block(now)

    reg = rg.classify(klines)
    fired = 0
    routed = None

    if coin in pm.positions:
        # Manage the open position's exits regardless of current regime.
        pm.on_tick(coin, price, market["funding_rate"], oi.get("oi_4hr_change"), now)
    else:
        # Build candidate decisions only for strategies eligible in this regime.
        elig = router.eligible_strategies(reg.regime)
        decisions = {}
        if "trend" in elig:
            ts = trend.evaluate(coin, klines, reg)
            if ts.should_enter:
                decisions["trend"] = ts
        if "mean_reversion" in elig:
            ms = mean_reversion.evaluate(coin, klines, reg)
            if ms.should_enter:
                decisions["mean_reversion"] = ms
        if "fade" in elig or "flush" in elig:
            cs = evaluate_confluence(coin, f, oi, cvd, liq, tsig, vol_spike,
                                     btc_uptrend_ok, mins, post_block)
            if cs.should_enter and cs.setup_type == "fade_short":
                decisions["fade"] = cs
            elif cs.should_enter and cs.setup_type == "flush_long":
                decisions["flush"] = cs

        routed = router.route(reg.regime, decisions)
        if routed.decision:
            setup = routed.decision
            ok, why = pm.can_open(coin, now)
            if ok:
                eff_mult = setup.size_multiplier * routed.size_scale
                proceed = True
                ai_decision = None
                # AI gate-keeper: only for the two spec setups. The structural gate
                # already passed; the brain confirms/vetoes and (re)sizes within the cap.
                if brain is not None and setup.setup_type in ("fade_short", "flush_long"):
                    ctx = _ai_context(f, oi, cvd, liq, tsig, fdyn, micro, reg, mins, btc_uptrend_ok)
                    ai_decision = brain.decide(coin, setup, ctx, now)
                    if ai_decision.confirmed and ai_decision.confidence >= config.AI_CONFIDENCE_FLOOR:
                        eff_mult = ai_decision.size_multiplier * routed.size_scale
                    else:
                        proceed = False
                        logger.info("[ALTPERP] %s AI gate-keeper blocked %s (action=%s conf=%s): %s",
                                    coin, setup.setup_type, ai_decision.action,
                                    ai_decision.confidence, ai_decision.key_signal or ai_decision.reasoning[:80])
                if proceed:
                    stop_frac = getattr(setup, "stop_frac", None)
                    plan = compute_size(pm.equity, price, setup.direction,
                                        setup.setup_type, eff_mult, stop_frac=stop_frac)
                    if plan:
                        pm.open_position(setup, plan, price, now)
                        fired = 1
                if ai_decision is not None:
                    _log_ai_decision(coin, setup, ai_decision, fired, db_path)
            else:
                logger.debug("[ALTPERP] %s routed %s but can't open: %s",
                             coin, routed.active_strategy, why)

    log_setup = routed.active_strategy if (routed and routed.active_strategy) else f"regime:{reg.regime}"
    database.log_signal({
        "coin": coin, "price": price,
        "funding_rate": f["funding_rate"], "funding_rate_48hr_avg": f["funding_48hr_avg"],
        "oi_current": oi["oi_current_usd"], "oi_4hr_change_pct": oi["oi_4hr_change"],
        "oi_8hr_change_pct": oi["oi_8hr_change"],
        "perp_cvd_4hr": cvd["perp_cvd"], "spot_cvd_4hr": cvd["spot_cvd"],
        "cvd_divergence": int(cvd.get("bearish_divergence") or cvd.get("bullish_divergence")),
        "liq_proximity": int(liq.get("short_proximity") or liq.get("long_proximity")),
        "tier1_triggered": int(bool(routed and routed.decision)),
        "tier2_score": 0, "minutes_to_funding_reset": mins,
        "setup_type": log_setup, "trade_fired": fired,
        "funding_velocity": fdyn["funding_velocity"],
        "funding_24h_change": fdyn["funding_24h_change"],
        "basis": micro["basis"], "basis_compression": micro["basis_compression"],
        "taker_ratio_short": micro["taker_ratio_short"],
        "taker_ratio_long": micro["taker_ratio_long"],
        "taker_divergence": int(micro["taker_divergence"]),
    }, db_path=db_path)
    return reg, routed


def _ai_context(f, oi, cvd, liq, tsig, fdyn, micro, reg, mins, btc_ok) -> Dict:
    """Assemble the full signal picture for the AI brain (JSON-serializable).
    Funding shown as %/8h for readability."""
    def pct(x):
        return round(x * 100, 4) if isinstance(x, (int, float)) else None
    return {
        "regime": reg.regime,
        "funding": {
            "rate_pct_8h": pct(f["funding_rate"]),
            "avg_48h_pct_8h": pct(f["funding_48hr_avg"]),
            "short_eligible": f["short_eligible"], "long_eligible": f["long_eligible"],
            "is_spike": f["is_spike"], "collapsed": f["funding_collapsed"],
        },
        "funding_dynamics": {
            "velocity_pct_8h": pct(fdyn["funding_velocity"]),
            "rising": fdyn["funding_rising"],
            "velocity_flip_down": fdyn["velocity_flip_down"],
            "change_24h_pct_8h": pct(fdyn["funding_24h_change"]),
            "trend_24h_rising": fdyn["funding_24h_rising"],
        },
        "open_interest": {
            "change_4h_pct": pct(oi["oi_4hr_change"]), "change_8h_pct": pct(oi["oi_8hr_change"]),
            "short_spike": oi["short_spike"], "long_flush": oi["long_flush"],
        },
        "cvd": cvd,
        "liquidity_clusters": liq,
        "microstructure": {
            "taker_ratio_short": micro["taker_ratio_short"],
            "taker_ratio_long": micro["taker_ratio_long"],
            "taker_divergence": micro["taker_divergence"],
            "basis_pct": pct(micro["basis"]),
            "basis_compressing": micro["basis_compressing"],
        },
        "trend": {"strong_uptrend": tsig.get("strong_uptrend"),
                  "strong_downtrend": tsig.get("strong_downtrend"), "slope": tsig.get("slope")},
        "btc_trend_ok_for_long": btc_ok,
        "minutes_to_funding_reset": mins,
    }


def _log_ai_decision(coin, setup, d, fired, db_path):
    try:
        database.log_ai_decision({
            "coin": coin, "gate_setup_type": setup.setup_type,
            "action": d.action, "confidence": d.confidence,
            "size_multiplier": d.size_multiplier, "key_signal": d.key_signal,
            "invalidation": d.invalidation, "urgency": d.urgency, "reasoning": d.reasoning,
            "model": d.model, "latency_ms": d.latency_ms,
            "input_tokens": d.input_tokens, "output_tokens": d.output_tokens,
            "trade_fired": fired, "error": d.error,
        }, db_path=db_path)
    except Exception as e:
        logger.debug("[ALTPERP] %s ai_decision log failed: %s", coin, e)


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
        "spot_klines": await dc.klines(coin, interval="240", limit=60, category="spot"),
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

    brain = None
    if config.AI_BRAIN_ENABLED:
        from .ai_brain import AIBrain
        brain = AIBrain()
        logger.info("[ALTPERP] AI gate-keeper enabled — model=%s, confidence floor=%d",
                    config.AI_MODEL, config.AI_CONFIDENCE_FLOOR)

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
                        evaluate_and_act(coin, market, pm, btc_uptrend_ok, now, brain=brain)
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

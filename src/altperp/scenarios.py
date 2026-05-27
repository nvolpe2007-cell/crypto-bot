"""
Scenario harness -- run the full fade/flush decision pipeline (signals -> structural
gate -> AI gate-keeper) over crafted market conditions, WITHOUT a live market or
opening any position. Two jobs:

  • Verification: does the gate behave across calm / building-froth / extreme-but-
    -rising / velocity-flip / uptrend-trap / liquidation-flush? (deterministic)
  • Demo: with --live and ANTHROPIC_API_KEY set, see the real brain reason on a
    realistic frothy snapshot before you ever wire it into the live loop.

    python -m src.altperp.scenarios          # gate decisions only (no API needed)
    python -m src.altperp.scenarios --live   # also call the real Claude brain

`advise()` is the reusable core; SCENARIOS are the crafted snapshots; `_selftest`
asserts the gate behaviour so this doubles as living documentation.
"""

import argparse
from datetime import datetime, timezone
from typing import Dict, Optional

from . import config, time_utils, regime as rg
from .signals import (funding_signal, oi_signal, cvd_signal, liq_proximity_signal,
                      trend_signal, funding_dynamics_signal, microstructure_signal)
from .math_utils import is_volume_spike
from .confluence import evaluate as evaluate_confluence
from .runner import _ai_context

_T = datetime(2026, 5, 26, 15, 0, tzinfo=timezone.utc)  # 45m before the 16:00 UTC reset


def advise(coin: str, market: Dict, now: datetime,
           brain=None, btc_uptrend_ok: bool = True) -> Dict:
    """Run signals -> confluence gate -> (if gated) AI brain. No execution, no I/O.
    Returns {regime, setup, ai, context}. `ai` is None unless the gate passes AND
    a brain is supplied."""
    price = market["price"]
    klines = market.get("klines", [])
    f = funding_signal(market["funding_rate"], market.get("funding_history", []))
    oi = oi_signal(market.get("oi_points", []), price)
    cvd = cvd_signal(market.get("perp_trades", []), market.get("spot_trades", []))
    liq = liq_proximity_signal(market.get("orderbook") or {}, price)
    tsig = trend_signal(klines)
    fdyn = funding_dynamics_signal(market["funding_rate"], market.get("funding_history", []))
    micro = microstructure_signal(market.get("perp_trades", []), klines, market.get("spot_klines", []))
    vols = [c["volume"] for c in klines]
    vol_spike = is_volume_spike(vols[-1], vols[-21:-1], config.VOLUME_SPIKE_MULTIPLIER) \
        if len(vols) > 1 else False
    mins = time_utils.get_minutes_to_next_funding_reset(now)
    post_block = time_utils.in_post_funding_block(now)
    setup = evaluate_confluence(coin, f, oi, cvd, liq, tsig, vol_spike,
                                btc_uptrend_ok, mins, post_block)
    reg = rg.classify(klines)
    ctx = _ai_context(f, oi, cvd, liq, tsig, fdyn, micro, reg, mins, btc_uptrend_ok)
    ai = brain.decide(coin, setup, ctx, now) if (setup.should_enter and brain is not None) else None
    return {"regime": reg.regime, "setup": setup, "ai": ai, "context": ctx}


# ── Crafted scenarios ─────────────────────────────────────────────────────────
def _trades(buys: int, sells: int, size: float = 5.0):
    """`buys` newest then `sells` older (Bybit recent-trade is newest-first)."""
    return [{"side": "Buy", "size": size}] * buys + [{"side": "Sell", "size": size}] * sells


def _klines(pattern: str, n: int = 60):
    if pattern == "flat":      # ranging/calm -- small ranges, flat closes
        return [{"ts": i * 14400000, "open": 100.0, "high": 100.6, "low": 99.4,
                 "close": 100.0 + (i % 2) * 0.05, "volume": 1000.0} for i in range(n)]
    if pattern == "volatile":  # flat closes, huge ranges
        return [{"ts": i * 14400000, "open": 100.0, "high": 108.0, "low": 92.0,
                 "close": 100.0 + (i % 2) * 0.1, "volume": 1000.0} for i in range(n)]
    if pattern == "uptrend":   # steadily rising -> strong_uptrend -> fade blocked
        return [{"ts": i * 14400000, "open": 100 + i, "high": 101 + i,
                 "low": 99 + i, "close": 100 + i, "volume": 1000.0} for i in range(n)]
    if pattern == "flush":     # flat then a violent down WICK that snaps back by close
        ks = [{"ts": i * 14400000, "open": 100.0, "high": 100.6, "low": 99.4,
               "close": 100.0, "volume": 1000.0} for i in range(n - 1)]
        ks.append({"ts": (n - 1) * 14400000, "open": 100.0, "high": 100.2,
                   "low": 92.0, "close": 98.0, "volume": 6000.0})  # wicked to 92, recovered to 98
        return ks
    raise ValueError(pattern)


def _oi(a, b, c):
    return [{"ts": 1, "oi": a}, {"ts": 2, "oi": b}, {"ts": 3, "oi": c}]


SCENARIOS = {
    # name: (market, btc_uptrend_ok, what we expect to see)
    "calm": dict(
        market=dict(price=100.0, funding_rate=0.0001, funding_history=[0.0001] * 6,
                    oi_points=_oi(100, 100, 101), perp_trades=_trades(20, 20),
                    spot_trades=_trades(20, 20), orderbook={}, klines=_klines("flat")),
        note="baseline funding, flat OI -> gate stays shut (bot sits idle, by design)"),
    "froth_rising": dict(
        market=dict(price=100.0, funding_rate=0.0006,
                    funding_history=[0.0002, 0.0003, 0.0004, 0.0005],
                    oi_points=_oi(90, 100, 132), perp_trades=_trades(8, 32),
                    spot_trades=_trades(5, 35), orderbook={}, klines=_klines("volatile")),
        note="funding extreme but STILL RISING -- brain should hesitate (crowd still growing)"),
    "froth_flip": dict(
        market=dict(price=100.0, funding_rate=0.00055,
                    funding_history=[0.0003, 0.0005, 0.0007],
                    oi_points=_oi(90, 100, 132), perp_trades=_trades(12, 28),
                    spot_trades=_trades(2, 38), orderbook={}, klines=_klines("volatile")),
        note="extreme funding, velocity JUST FLIPPED DOWN + CVD/taker bearish -- prime fade"),
    "uptrend_trap": dict(
        market=dict(price=160.0, funding_rate=0.0006, funding_history=[0.0002] * 4,
                    oi_points=_oi(90, 100, 132), perp_trades=_trades(20, 20),
                    spot_trades=_trades(20, 20), orderbook={}, klines=_klines("uptrend")),
        note="gated funding+OI BUT strong uptrend -> trend filter BLOCKS the short (protection)"),
    "flush": dict(
        market=dict(price=98.0, funding_rate=0.0001,
                    funding_history=[0.0004, 0.0005, 0.0006],
                    oi_points=_oi(120, 110, 82), perp_trades=_trades(10, 30),
                    spot_trades=_trades(25, 10), orderbook={}, klines=_klines("flush")),
        btc_ok=True,
        note="OI flushed -25%, funding collapsed, volume spike -> flush LONG"),
}


def _run_cli(live: bool):
    brain = None
    if live:
        from .ai_brain import AIBrain
        brain = AIBrain()
        print(f"LIVE -- model={config.AI_MODEL}, confidence floor={config.AI_CONFIDENCE_FLOOR}\n")
    else:
        print("Gate-only (no API). Add --live + ANTHROPIC_API_KEY to see the brain reason.\n")

    for name, sc in SCENARIOS.items():
        r = advise("SOLUSDT", sc["market"], _T, brain=brain, btc_uptrend_ok=sc.get("btc_ok", True))
        s = r["setup"]
        gate = (f"{s.setup_type} ({s.direction}) x{s.size_multiplier:g}" if s.should_enter
                else f"no trade ({s.blocked_reason or 'gate not met'})")
        print(f"[{name:13s}] regime={r['regime']:11s} gate-> {gate}")
        print(f"    {sc['note']}")
        if r["ai"] is not None:
            ai = r["ai"]
            verdict = "VETO" if not ai.confirmed else f"CONFIRM x{ai.size_multiplier:g}"
            print(f"    AI: {verdict}  conf={ai.confidence}/10  urgency={ai.urgency}")
            print(f"        key: {ai.key_signal}")
            if ai.reasoning:
                print(f"        why: {ai.reasoning}")
            if ai.invalidation:
                print(f"        invalidated if: {ai.invalidation}")
        elif s.should_enter and not live:
            print("    AI: (run --live to see the brain's confirm/veto)")
        print()


def _selftest():
    """Assert the deterministic gate behaviour -- no brain, no network."""
    calm = advise("SOLUSDT", SCENARIOS["calm"]["market"], _T)
    assert not calm["setup"].should_enter and calm["ai"] is None, calm["setup"]

    fr = advise("SOLUSDT", SCENARIOS["froth_rising"]["market"], _T)
    assert fr["setup"].should_enter and fr["setup"].setup_type == "fade_short", fr["setup"]
    assert fr["context"]["funding_dynamics"]["rising"] is True

    ff = advise("SOLUSDT", SCENARIOS["froth_flip"]["market"], _T)
    assert ff["setup"].should_enter and ff["setup"].setup_type == "fade_short", ff["setup"]
    assert ff["context"]["funding_dynamics"]["velocity_flip_down"] is True, ff["context"]
    assert ff["context"]["microstructure"]["taker_divergence"] is True, ff["context"]

    ut = advise("SOLUSDT", SCENARIOS["uptrend_trap"]["market"], _T)
    assert not ut["setup"].should_enter and ut["setup"].blocked_reason == "trend_filter_uptrend", ut["setup"]
    assert ut["ai"] is None  # gate blocked -> brain never consulted

    fl = advise("SOLUSDT", SCENARIOS["flush"]["market"], _T)
    assert fl["setup"].should_enter and fl["setup"].setup_type == "flush_long", fl["setup"]
    print("scenarios selftest OK")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Alt-perp decision scenario harness")
    ap.add_argument("--live", action="store_true", help="call the real Claude brain")
    ap.add_argument("--selftest", action="store_true", help="assert gate behaviour and exit")
    args = ap.parse_args()
    if args.selftest:
        _selftest()
    else:
        _run_cli(args.live)

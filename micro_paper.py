#!/usr/bin/env python3
"""
Maker-only microstructure forward arm — the 90-day re-test.

The microstructure scalper FAILED before at t≈-8.82 with TAKER cost on 2s-REST
snapshots and a candle-CVD proxy. This is the legitimately different hypothesis:
**real Kraken tick data + post-only maker fills**. Both inputs that killed it are
replaced, so it is a new test rather than a re-run of a dead one.

What makes it honest, and why each piece matters:

  • ENTRY IS A RESTING BID. `post_maker` never crosses the spread. If the tape
    doesn't trade down through the bid before the timeout, the order is cancelled
    and THERE IS NO TRADE. Non-fills are the real cost of being a maker; they are
    counted in `nonfills` rather than quietly dropped, because a strategy that
    only records the times it got filled is measuring a different strategy.

  • ADVERSE SELECTION COMES FREE. A resting buy fills only when sellers trade
    down into it — you are filled precisely as the tape pushes against you. That
    is a property of the fill model, not an assumption bolted on.

  • LONG ONLY. Maker-only longs on Kraken spot are what a US retail account can
    actually execute, which is what `proof_scorecard._microstructure_forward()`
    asserts when it marks this arm executable. Shorts would make that false.

  • STOPS TAKE LIQUIDITY, AND PAY FOR IT. A maker-only exit can refuse to fill
    for an unbounded time. Pretending otherwise would manufacture a strategy with
    no downside, so a breached stop crosses the spread and is charged the TAKER
    fee. `exit_kind` records which happened.

Records to its own state file so the proof arm judges it independently of every
other arm. Nothing here touches the live directional loop.

  python micro_paper.py --duration 3600      # run an hour, then exit
  python micro_paper.py --dry-run            # wire-check, no orders
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from src.cvd_tracker import TickCVDTracker
from src.kill_switch import is_killed as _is_killed
from src.kraken_ws import KrakenBookFeed, KrakenTradeFeed, TradeTick
from src.maker_fill import (MAKER_FEE_SPOT, MakerOrder, apply_trade, expire,
                            post_maker)
from src.orderflow_ws import obi_from_book

logger = logging.getLogger("micro_paper")

DATA = Path(os.getenv("MICRO_DATA_DIR", "data"))
STATE_FILE = Path(os.getenv("MICRO_STATE_FILE", str(DATA / "micro_paper_state.json")))

SYMBOLS = [s.strip() for s in os.getenv("MICRO_SYMBOLS", "BTC/USD,ETH/USD").split(",") if s.strip()]

# --- the gate ---------------------------------------------------------------
# Deliberately few knobs. The previous incarnation died with a large parameter
# surface; every extra threshold here is another way to overfit a 90-day window.
OBI_LONG_MIN   = float(os.getenv("MICRO_OBI_LONG_MIN", "0.58"))   # bid-heavy book
CVD_SLOPE_MIN  = float(os.getenv("MICRO_CVD_SLOPE_MIN", "0.0"))   # net buying on the tape
MIN_TICKS      = int(os.getenv("MICRO_MIN_TICKS", "40"))          # don't act on a cold tape
BOOK_STALE_MAX = float(os.getenv("MICRO_BOOK_STALE_MAX", "5.0"))  # seconds

# --- sizing and exits -------------------------------------------------------
SIZE_USD       = float(os.getenv("MICRO_SIZE_USD", "50"))
MAX_POSITIONS  = int(os.getenv("MICRO_MAX_POSITIONS", "1"))
# Kraken spot maker is 0.25%/side at tier 0, so a round trip costs 0.50%. That
# is the whole economics of this arm and it is worth stating plainly: at this
# fee, a maker-only strategy CANNOT scalp. The target has to clear half a
# percent before it is worth anything, which makes this a short-hold momentum
# arm, not the tick scalper the name suggests. 0.65% leaves ~0.15% of edge
# after costs — thin, and honest about being thin.
TAKE_PROFIT    = float(os.getenv("MICRO_TAKE_PROFIT", "0.0065"))
STOP_LOSS      = float(os.getenv("MICRO_STOP_LOSS", "0.0045"))
MAX_HOLD_SECS  = float(os.getenv("MICRO_MAX_HOLD_SECS", "900"))
TAKER_FEE_SPOT = float(os.getenv("TAKER_FEE_SPOT", "0.0040"))     # Kraken spot taker t0
COOLDOWN_SECS  = float(os.getenv("MICRO_COOLDOWN_SECS", "120"))

# A round trip costs 2 x maker = 0.50%. A take-profit under that is negative-EV
# before it is anything else — refuse to start rather than log a losing arm.
_MIN_TP = 2 * MAKER_FEE_SPOT


@dataclass
class Position:
    symbol: str
    entry_ts: float
    entry_price: float
    size_usd: float
    entry_fee: float
    obi_at_entry: float
    cvd_slope_at_entry: float
    fill_wait_secs: float
    exit_order: Optional[MakerOrder] = None


@dataclass
class Arm:
    """Per-symbol state: the tape tracker, any resting order, any position."""
    symbol: str
    cvd: TickCVDTracker
    pending: Optional[MakerOrder] = None
    position: Optional[Position] = None
    last_exit_ts: float = 0.0
    last_state: object = None
    ticks: int = 0
    last_price: float = 0.0
    nonfills: int = 0
    entry_meta: dict = field(default_factory=dict)


def _load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except (json.JSONDecodeError, OSError) as e:
            logger.error("state unreadable (%s) — refusing to overwrite it", e)
            raise
    return {"closed": [], "nonfills": 0, "started": time.time()}


def _save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2))
    tmp.replace(STATE_FILE)          # atomic: a crash mid-write can't truncate the record


class MicroMakerArm:
    def __init__(self, symbols: list[str], *, dry_run: bool = False):
        if TAKE_PROFIT < _MIN_TP:
            raise SystemExit(
                f"MICRO_TAKE_PROFIT={TAKE_PROFIT:.4f} is below the {_MIN_TP:.4f} "
                f"round-trip maker cost — negative-EV by construction. Raise it."
            )
        self.dry_run = dry_run
        self.arms = {s: Arm(symbol=s, cvd=TickCVDTracker(symbol=s)) for s in symbols}
        self.state = _load_state()
        self.book = KrakenBookFeed(symbols)
        self.trades = KrakenTradeFeed(symbols, on_trade=self._on_tick)

    # -- tape ---------------------------------------------------------------
    def _on_tick(self, t: TradeTick) -> None:
        """Called synchronously inside the WS handler — keep it cheap."""
        arm = self.arms.get(t.symbol)
        if arm is None:
            return
        ts = time.time()
        arm.ticks += 1
        arm.last_price = t.price
        arm.last_state = arm.cvd.update_tick(t.price, t.qty, t.side, ts)

        # a resting order only resolves against the tape — this is the whole point
        if arm.pending is not None and not arm.pending.resolved:
            apply_trade(arm.pending, t.price, t.side, ts)
        if arm.position is not None and arm.position.exit_order is not None:
            eo = arm.position.exit_order
            if not eo.resolved:
                apply_trade(eo, t.price, t.side, ts)

    # -- gate ---------------------------------------------------------------
    def _long_signal(self, arm: Arm) -> tuple[bool, float, float]:
        if arm.ticks < MIN_TICKS or arm.last_state is None:
            return False, 0.0, 0.0
        if self.book.staleness(arm.symbol) > BOOK_STALE_MAX:
            return False, 0.0, 0.0
        top = self.book.get_top(arm.symbol)
        if not top:
            return False, 0.0, 0.0
        bids, asks = top[0], top[1]
        obi = obi_from_book(bids, asks)
        if obi is None:
            return False, 0.0, 0.0
        st = arm.last_state
        ok = (
            obi >= OBI_LONG_MIN
            and getattr(st, "cvd_slope", 0.0) > CVD_SLOPE_MIN
            and getattr(st, "cvd_direction", 0) == 1
            and getattr(st, "price_responding", False)
        )
        return ok, obi, float(getattr(st, "cvd_slope", 0.0))

    def _best(self, symbol: str) -> tuple[float, float] | None:
        top = self.book.get_top(symbol)
        if not top or not top[0] or not top[1]:
            return None
        try:
            return float(top[0][0][0]), float(top[1][0][0])   # best bid, best ask
        except (IndexError, TypeError, ValueError):
            return None

    # -- one evaluation pass -------------------------------------------------
    def step(self) -> None:
        now = time.time()
        for arm in self.arms.values():
            # 1. resolve a resting ENTRY order
            if arm.pending is not None:
                expire(arm.pending, now)
                if arm.pending.filled:
                    self._open(arm, now)
                elif arm.pending.cancelled:
                    arm.nonfills += 1
                    self.state["nonfills"] = self.state.get("nonfills", 0) + 1
                    logger.info("[%s] no fill — tape never came to the bid (%d total)",
                                arm.symbol, arm.nonfills)
                    arm.pending = None

            # 2. manage an open position
            if arm.position is not None:
                self._manage(arm, now)
                continue

            # 3. consider a new entry
            if arm.pending is not None:
                continue
            if _is_killed():
                continue
            if now - arm.last_exit_ts < COOLDOWN_SECS:
                continue
            if sum(1 for a in self.arms.values() if a.position) >= MAX_POSITIONS:
                continue
            ok, obi, slope = self._long_signal(arm)
            if not ok:
                continue
            best = self._best(arm.symbol)
            if best is None:
                continue
            bid, _ask = best
            if self.dry_run:
                logger.info("[%s] DRY-RUN would post maker bid @ %.2f (obi=%.3f slope=%.4f)",
                            arm.symbol, bid, obi, slope)
                continue
            arm.pending = post_maker("buy", bid, SIZE_USD, now)
            arm.entry_meta = {"obi": obi, "cvd_slope": slope}
            logger.info("[%s] resting bid @ %.2f  obi=%.3f slope=%.4f",
                        arm.symbol, bid, obi, slope)

    def _open(self, arm: Arm, now: float) -> None:
        o = arm.pending
        arm.position = Position(
            symbol=arm.symbol,
            entry_ts=o.fill_ts or now,
            entry_price=o.fill_price,
            size_usd=o.size_usd,
            entry_fee=o.fee_usd,
            obi_at_entry=arm.entry_meta.get("obi", 0.0),
            cvd_slope_at_entry=arm.entry_meta.get("cvd_slope", 0.0),
            fill_wait_secs=(o.fill_ts or now) - o.post_ts,
        )
        arm.pending = None
        logger.info("[%s] FILLED long @ %.2f after %.1fs",
                    arm.symbol, arm.position.entry_price, arm.position.fill_wait_secs)

    def _manage(self, arm: Arm, now: float) -> None:
        p = arm.position
        px = arm.last_price or p.entry_price
        move = (px - p.entry_price) / p.entry_price
        held = now - p.entry_ts

        # a resting exit that filled ends the trade at the maker fee
        if p.exit_order is not None:
            expire(p.exit_order, now)
            if p.exit_order.filled:
                self._close(arm, p.exit_order.fill_price, p.exit_order.fee_usd, "maker_tp", now)
                return
            if p.exit_order.cancelled:
                p.exit_order = None          # re-post below if still warranted

        # stop and max-hold have to cross the spread — a maker exit may never fill
        if move <= -STOP_LOSS:
            self._close(arm, px, p.size_usd * TAKER_FEE_SPOT, "taker_stop", now)
            return
        if held >= MAX_HOLD_SECS:
            self._close(arm, px, p.size_usd * TAKER_FEE_SPOT, "taker_timeout", now)
            return

        # in profit: try to leave as a maker, resting on the ask
        if move >= TAKE_PROFIT and p.exit_order is None:
            best = self._best(arm.symbol)
            if best is not None:
                p.exit_order = post_maker("sell", best[1], p.size_usd, now)
                logger.info("[%s] resting ask @ %.2f (+%.2f%%)", arm.symbol, best[1], move * 100)

    def _close(self, arm: Arm, exit_price: float, exit_fee: float,
               kind: str, now: float) -> None:
        p = arm.position
        gross = p.size_usd * (exit_price - p.entry_price) / p.entry_price
        net = gross - p.entry_fee - exit_fee
        self.state.setdefault("closed", []).append({
            "symbol": p.symbol,
            "side": "long",
            "entry_ts": p.entry_ts,
            "exit_ts": now,
            "entry_price": p.entry_price,
            "exit_price": exit_price,
            "size_usd": p.size_usd,
            "gross": round(gross, 4),
            "fees": round(p.entry_fee + exit_fee, 4),
            "pnl": round(net, 4),
            "exit_kind": kind,
            "held_secs": round(now - p.entry_ts, 1),
            "fill_wait_secs": round(p.fill_wait_secs, 1),
            "obi_at_entry": round(p.obi_at_entry, 4),
            "cvd_slope_at_entry": round(p.cvd_slope_at_entry, 6),
        })
        _save_state(self.state)
        logger.info("[%s] CLOSED %s  net=$%.3f  held=%.0fs", p.symbol, kind, net, now - p.entry_ts)
        arm.position = None
        arm.last_exit_ts = now

    # -- lifecycle -----------------------------------------------------------
    async def run(self, duration: float | None) -> None:
        started = time.time()
        tasks = [asyncio.create_task(self.book.start()),
                 asyncio.create_task(self.trades.start())]
        try:
            while True:
                await asyncio.sleep(1.0)
                try:
                    self.step()
                except Exception:
                    logger.exception("step failed — continuing")
                if duration and time.time() - started >= duration:
                    break
        finally:
            self.book.stop()
            self.trades.stop()
            for t in tasks:
                t.cancel()
            _save_state(self.state)
            closed = self.state.get("closed", [])
            logger.info("stopped — %d closed trades, %d non-fills",
                        len(closed), self.state.get("nonfills", 0))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--duration", type=float, default=None, help="seconds to run, then exit")
    ap.add_argument("--dry-run", action="store_true", help="evaluate the gate, post nothing")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args()
    logging.basicConfig(
        level=logging.DEBUG if a.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    arm = MicroMakerArm(SYMBOLS, dry_run=a.dry_run)
    logger.info("micro maker arm — symbols=%s tp=%.3f%% stop=%.3f%% maker_fee=%.3f%%",
                SYMBOLS, TAKE_PROFIT * 100, STOP_LOSS * 100, MAKER_FEE_SPOT * 100)
    try:
        asyncio.run(arm.run(a.duration))
    except KeyboardInterrupt:
        logger.info("interrupted")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

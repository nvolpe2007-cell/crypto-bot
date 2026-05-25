"""
Funding Rate Arbitrage — Paper Trading Simulator

Reuses the FundingScanner (already running in src/bot.py) for live funding-rate
data, then simulates cash-and-carry positions:

    Positive APY: LONG SPOT + SHORT PERP (collect funding from longs)
    Negative APY: SHORT SPOT + LONG PERP (collect funding from shorts)

Market-neutral by construction, so spot/perp price PnL nets to ~0; the real
return is the funding accrued each 8h cycle.

Silent: never sends per-trade Telegram. Posts a single daily P&L rollup every
24h from bot startup.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Dict, List, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from funding_scanner import FundingScanner

logger = logging.getLogger(__name__)

STATE_FILE = Path('data/funding_arb_state.json')

MIN_ENTRY_APY = 15.0           # only open positions above this APY
EXIT_APY_FRACTION = 0.40       # close when APY drops below 40% of entry rate
MAX_HOLD_DAYS = 7              # safety cap
MAX_CONCURRENT_POSITIONS = 3
POSITION_SIZE_USD = 500.0      # per leg (so $1000 capital used per pair)
FUNDING_CYCLE_HOURS = 8        # standard for Binance/Bybit perps

# ── APY cap: skip absurd funding ─────────────────────────────────────────────
# The scanner's top |APY| names are almost always tiny illiquid alt perps
# (e.g. 800–2000% APY meme coins). That funding is real on paper but rarely
# capturable live — you may not be able to short the perp or buy the spot at
# size, and the rate flips violently. Cap entries so we only hold plausibly
# realistic, executable opportunities. Env-tunable.
MAX_ENTRY_APY = float(os.getenv('FUNDING_ARB_MAX_APY', '150.0'))

# ── Notifications ────────────────────────────────────────────────────────────
# Per-open / per-close Telegram alerts (default on) + a configurable rollup
# cadence (default hourly) so funding activity is actually visible, not buried
# in a once-a-day summary.
NOTIFY_PER_TRADE = os.getenv('FUNDING_ARB_NOTIFY_PER_TRADE', '1') == '1'
ROLLUP_INTERVAL_SECONDS = float(os.getenv('FUNDING_ARB_ROLLUP_HOURS', '1.0')) * 3600

# ── Cost model ───────────────────────────────────────────────────────────────
# Cash-and-carry is FOUR taker fills round-trip: open(spot)+open(perp) and
# close(spot)+close(perp). On Binance/Bybit, perp taker ≈0.04% + spot taker
# ≈0.10% → ~0.28% of one leg's notional round-trip, before slippage. Ignoring
# this is exactly the cost-blind mistake that produced the bot's old ~1% win
# rate (see strategy_cost_expectancy_fix). We deduct it ONCE at entry and report
# PnL net of it. Env-tunable; default leans conservative (taker, not maker).
ROUND_TRIP_COST_FRAC = float(os.getenv('FUNDING_ARB_COST_FRAC', '0.0022'))  # 0.22%
# Don't open unless funding at the entry rate clears round-trip cost within this
# many 8h cycles — i.e. the position is expected to be net-positive well inside
# MAX_HOLD. At 0.22% cost this implies an effective floor of ~24% APY after costs.
MAX_BREAKEVEN_CYCLES = float(os.getenv('FUNDING_ARB_MAX_BREAKEVEN_CYCLES', '10'))


@dataclass
class PaperPosition:
    symbol: str
    exchange: str
    direction: str               # "LONG_SPOT_SHORT_PERP" or "SHORT_SPOT_LONG_PERP"
    entry_apy: float
    entry_rate_8h: float
    size_usd: float
    entry_time_iso: str
    funding_collected: float = 0.0   # GROSS funding accrued (before costs)
    entry_cost: float = 0.0          # round-trip transaction cost, charged at open
    cycles_collected: int = 0
    last_funding_ts_iso: Optional[str] = None
    closed: bool = False
    close_time_iso: Optional[str] = None
    close_reason: Optional[str] = None

    @property
    def entry_time(self) -> datetime:
        return datetime.fromisoformat(self.entry_time_iso)

    @property
    def net_pnl(self) -> float:
        """Funding collected minus the round-trip transaction cost."""
        return self.funding_collected - self.entry_cost


class FundingArbPaperSim:
    """Paper-trades funding arb based on a live FundingScanner feed."""

    def __init__(
        self,
        scanner: "FundingScanner",
        notifier=None,
        min_entry_apy: float = MIN_ENTRY_APY,
        position_size_usd: float = POSITION_SIZE_USD,
        max_positions: int = MAX_CONCURRENT_POSITIONS,
        max_entry_apy: float = MAX_ENTRY_APY,
    ):
        self.scanner = scanner
        self.notifier = notifier
        self.min_entry_apy = min_entry_apy
        self.max_entry_apy = max_entry_apy
        self.position_size_usd = position_size_usd
        self.max_positions = max_positions

        self.open_positions: Dict[str, PaperPosition] = {}
        self.closed_positions: List[PaperPosition] = []
        self.start_time = datetime.now(timezone.utc)
        self.last_rollup_total = 0.0       # cumulative P&L at last rollup
        self.running = False

        self._load_state()

    # ── persistence ────────────────────────────────────────────────────────────

    def _load_state(self):
        if not STATE_FILE.exists():
            return
        try:
            data = json.loads(STATE_FILE.read_text())
            self.open_positions = {
                k: PaperPosition(**v) for k, v in data.get('open', {}).items()
            }
            self.closed_positions = [
                PaperPosition(**v) for v in data.get('closed', [])
            ]
            self.last_rollup_total = float(data.get('last_rollup_total', 0.0))
            start_iso = data.get('start_time_iso')
            if start_iso:
                self.start_time = datetime.fromisoformat(start_iso)
            logger.info(
                f"[FundingArbPaper] Restored {len(self.open_positions)} open, "
                f"{len(self.closed_positions)} closed positions"
            )
        except Exception as e:
            logger.warning(f"[FundingArbPaper] State load failed: {e}")

    def _save_state(self):
        STATE_FILE.parent.mkdir(exist_ok=True)
        payload = {
            'open': {k: asdict(v) for k, v in self.open_positions.items()},
            'closed': [asdict(v) for v in self.closed_positions[-500:]],
            'last_rollup_total': self.last_rollup_total,
            'start_time_iso': self.start_time.isoformat(),
        }
        STATE_FILE.write_text(json.dumps(payload, indent=2))

    # ── core sim loop ──────────────────────────────────────────────────────────

    def _notify(self, msg: str):
        """Best-effort per-event Telegram alert (gated by NOTIFY_PER_TRADE)."""
        if not (NOTIFY_PER_TRADE and self.notifier):
            return
        try:
            self.notifier.send_message(msg)
        except Exception as e:
            logger.warning(f"[FundingArbPaper] notify failed: {e}")

    async def start(self):
        self.running = True
        logger.info(
            f"[FundingArbPaper] Started (paper). "
            f"apy={self.min_entry_apy}-{self.max_entry_apy}% size=${self.position_size_usd} "
            f"max_positions={self.max_positions} per_trade_alerts={NOTIFY_PER_TRADE} "
            f"rollup={ROLLUP_INTERVAL_SECONDS/3600:.0f}h"
        )

        rollup_task = asyncio.create_task(self._rollup_loop())

        try:
            while self.running:
                try:
                    self._tick()
                except Exception as e:
                    logger.error(f"[FundingArbPaper] Tick error: {e}")
                await asyncio.sleep(60)
        finally:
            rollup_task.cancel()

    async def stop(self):
        self.running = False
        self._save_state()

    def _tick(self):
        """Process one cycle: accrue funding, manage exits, look for entries."""
        opps = self.scanner.get_state()  # list[dict]
        opp_by_key = {f"{o['exchange']}:{o['symbol']}": o for o in opps}

        now = datetime.now(timezone.utc)

        # 1. Accrue funding + check exits on existing positions
        for key, pos in list(self.open_positions.items()):
            self._accrue_funding(pos, opp_by_key.get(key), now)
            reason = self._should_exit(pos, opp_by_key.get(key), now)
            if reason:
                pos.closed = True
                pos.close_time_iso = now.isoformat()
                pos.close_reason = reason
                self.closed_positions.append(pos)
                del self.open_positions[key]
                logger.info(
                    f"[FundingArbPaper] CLOSE {pos.symbol} ({pos.exchange}) "
                    f"reason={reason} funding=${pos.funding_collected:.4f} "
                    f"cost=${pos.entry_cost:.4f} net=${pos.net_pnl:+.4f} "
                    f"cycles={pos.cycles_collected}"
                )
                emoji = "✅" if pos.net_pnl >= 0 else "❌"
                self._notify(
                    f"{emoji} <b>Funding Arb CLOSE</b>\n"
                    f"{pos.exchange} {pos.symbol}\n"
                    f"net <b>${pos.net_pnl:+.4f}</b> "
                    f"(funding ${pos.funding_collected:.4f} − cost ${pos.entry_cost:.2f})\n"
                    f"{pos.cycles_collected} funding cycles · {reason}"
                )

        # 2. Look for new entries
        if len(self.open_positions) < self.max_positions:
            for opp in opps:
                if len(self.open_positions) >= self.max_positions:
                    break
                key = f"{opp['exchange']}:{opp['symbol']}"
                if key in self.open_positions:
                    continue
                if abs(opp['apy']) < self.min_entry_apy:
                    continue
                if abs(opp['apy']) > self.max_entry_apy:
                    logger.info(
                        f"[FundingArbPaper] SKIP {opp['symbol']} ({opp['exchange']}) "
                        f"apy={opp['apy']:.0f}% > cap {self.max_entry_apy:.0f}% "
                        f"(illiquid/unstable — not realistically capturable)"
                    )
                    continue
                # Cost-aware gate: reject unless funding at the entry rate clears
                # the round-trip cost within MAX_BREAKEVEN_CYCLES. The position
                # size cancels out, so this is purely rate-vs-cost.
                rate_8h_frac = abs(float(opp['rate_8h'])) / 100.0
                if rate_8h_frac <= 0:
                    continue
                breakeven_cycles = ROUND_TRIP_COST_FRAC / rate_8h_frac
                if breakeven_cycles > MAX_BREAKEVEN_CYCLES:
                    logger.info(
                        f"[FundingArbPaper] SKIP {opp['symbol']} ({opp['exchange']}) "
                        f"apy={opp['apy']:.1f}%: breakeven {breakeven_cycles:.1f} cycles "
                        f"> max {MAX_BREAKEVEN_CYCLES:.0f} (cost {ROUND_TRIP_COST_FRAC*100:.2f}% "
                        f"vs {rate_8h_frac*100:.4f}%/cycle)"
                    )
                    continue
                self._open_position(opp, now)

        self._save_state()

    def _open_position(self, opp: dict, now: datetime):
        key = f"{opp['exchange']}:{opp['symbol']}"
        direction = (
            "LONG_SPOT_SHORT_PERP" if opp['apy'] > 0
            else "SHORT_SPOT_LONG_PERP"
        )
        entry_cost = ROUND_TRIP_COST_FRAC * self.position_size_usd
        pos = PaperPosition(
            symbol=opp['symbol'],
            exchange=opp['exchange'],
            direction=direction,
            entry_apy=float(opp['apy']),
            entry_rate_8h=float(opp['rate_8h']) / 100.0,  # scanner stores as %
            size_usd=self.position_size_usd,
            entry_time_iso=now.isoformat(),
            last_funding_ts_iso=now.isoformat(),
            entry_cost=entry_cost,
        )
        self.open_positions[key] = pos
        logger.info(
            f"[FundingArbPaper] OPEN {pos.symbol} ({pos.exchange}) "
            f"apy={pos.entry_apy:.1f}% dir={direction} size=${pos.size_usd} "
            f"entry_cost=${entry_cost:.4f}"
        )
        dir_label = ("long spot / short perp" if direction == "LONG_SPOT_SHORT_PERP"
                     else "short spot / long perp")
        self._notify(
            f"📈 <b>Funding Arb OPEN</b>\n"
            f"{pos.exchange} {pos.symbol}\n"
            f"{pos.entry_apy:+.0f}% APY · {dir_label}\n"
            f"size ${pos.size_usd:.0f} · entry cost ${entry_cost:.2f}\n"
            f"<i>collects funding every {FUNDING_CYCLE_HOURS}h while open</i>"
        )

    def _accrue_funding(
        self,
        pos: PaperPosition,
        current_opp: Optional[dict],
        now: datetime,
    ):
        """Accrue funding for any 8h cycles that have elapsed since last accrual."""
        last_ts = (
            datetime.fromisoformat(pos.last_funding_ts_iso)
            if pos.last_funding_ts_iso else pos.entry_time
        )
        hours_since = (now - last_ts).total_seconds() / 3600.0
        cycles_due = int(hours_since // FUNDING_CYCLE_HOURS)
        if cycles_due <= 0:
            return

        # Use current rate if scanner still tracks the symbol, otherwise
        # decay toward zero (assume rate normalised below scanner's min threshold).
        if current_opp is not None:
            current_rate_per_cycle = float(current_opp['rate_8h']) / 100.0
        else:
            current_rate_per_cycle = pos.entry_rate_8h * 0.25  # conservative

        # We collect funding (we're on the receiving side by construction).
        # Sign of entry_rate tells us which side; magnitude × size = $/cycle.
        per_cycle_pnl = abs(current_rate_per_cycle) * pos.size_usd
        pos.funding_collected += per_cycle_pnl * cycles_due
        pos.cycles_collected += cycles_due
        pos.last_funding_ts_iso = (
            last_ts + timedelta(hours=cycles_due * FUNDING_CYCLE_HOURS)
        ).isoformat()

    def _should_exit(
        self,
        pos: PaperPosition,
        current_opp: Optional[dict],
        now: datetime,
    ) -> Optional[str]:
        # 1. Max-hold safety
        age_days = (now - pos.entry_time).total_seconds() / 86400.0
        if age_days >= MAX_HOLD_DAYS:
            return f"max_hold_{MAX_HOLD_DAYS}d"

        # 2. Funding flipped sign (paying instead of collecting)
        if current_opp is not None:
            current_apy = float(current_opp['apy'])
            if (pos.entry_apy > 0 and current_apy < 0) or \
               (pos.entry_apy < 0 and current_apy > 0):
                return "funding_flipped"

            # 3. Funding decayed below exit threshold
            if abs(current_apy) < abs(pos.entry_apy) * EXIT_APY_FRACTION:
                return f"apy_decayed_to_{current_apy:.1f}"

        # 4. Symbol disappeared from scanner for an extended period:
        # only force-close if we've been "off the radar" for a full day.
        if current_opp is None:
            last_ts = (
                datetime.fromisoformat(pos.last_funding_ts_iso)
                if pos.last_funding_ts_iso else pos.entry_time
            )
            if (now - last_ts).total_seconds() > 86400:
                return "off_scanner_24h"

        return None

    # ── 24h rollup ─────────────────────────────────────────────────────────────

    async def _rollup_loop(self):
        try:
            while self.running:
                await asyncio.sleep(ROLLUP_INTERVAL_SECONDS)
                try:
                    self._send_daily_rollup()
                except Exception as e:
                    logger.error(f"[FundingArbPaper] Rollup error: {e}")
        except asyncio.CancelledError:
            pass

    def _total_pnl(self) -> float:
        """Cumulative NET P&L (funding collected minus transaction costs)."""
        open_pnl = sum(p.net_pnl for p in self.open_positions.values())
        closed_pnl = sum(p.net_pnl for p in self.closed_positions)
        return open_pnl + closed_pnl

    def _total_gross_funding(self) -> float:
        return (sum(p.funding_collected for p in self.open_positions.values())
                + sum(p.funding_collected for p in self.closed_positions))

    def _total_costs(self) -> float:
        return (sum(p.entry_cost for p in self.open_positions.values())
                + sum(p.entry_cost for p in self.closed_positions))

    def _send_daily_rollup(self):
        total = self._total_pnl()
        delta = total - self.last_rollup_total
        now = datetime.now(timezone.utc)
        uptime_h = (now - self.start_time).total_seconds() / 3600.0
        interval_h = ROLLUP_INTERVAL_SECONDS / 3600.0

        open_lines = [
            f"  • {p.exchange} {p.symbol}: net ${p.net_pnl:+.4f} "
            f"(funding ${p.funding_collected:.4f} − cost ${p.entry_cost:.4f}, "
            f"{p.cycles_collected} cycles, {p.entry_apy:.0f}% APY)"
            for p in self.open_positions.values()
        ] or ["  (none)"]

        closed_today = [
            p for p in self.closed_positions
            if p.close_time_iso and
            (now - datetime.fromisoformat(p.close_time_iso)).total_seconds() < 86400
        ]
        closed_lines = [
            f"  • {p.exchange} {p.symbol}: net ${p.net_pnl:+.4f} "
            f"({p.close_reason})"
            for p in closed_today
        ] or ["  (none)"]

        msg = (
            f"<b>📊 Funding Arb — P&amp;L rollup (net of costs)</b>\n"
            f"Last {interval_h:.0f}h net P&amp;L: <b>${delta:+.4f}</b>\n"
            f"Cumulative net: ${total:.4f}\n"
            f"  (gross funding ${self._total_gross_funding():.4f} − "
            f"costs ${self._total_costs():.4f})\n"
            f"Uptime: {uptime_h:.0f}h\n\n"
            f"<b>Open ({len(self.open_positions)}):</b>\n"
            + "\n".join(open_lines) + "\n\n"
            f"<b>Closed in last 24h ({len(closed_today)}):</b>\n"
            + "\n".join(closed_lines)
        )

        logger.info(f"[FundingArbPaper] Daily rollup: ${delta:+.4f} (cum ${total:.4f})")

        if self.notifier:
            try:
                self.notifier.send_message(msg)
            except Exception as e:
                logger.warning(f"[FundingArbPaper] Telegram send failed: {e}")

        self.last_rollup_total = total
        self._save_state()

    # ── public helpers ─────────────────────────────────────────────────────────

    def get_summary(self) -> dict:
        return {
            'open_positions': len(self.open_positions),
            'closed_positions': len(self.closed_positions),
            'total_pnl': round(self._total_pnl(), 4),          # NET of costs
            'total_gross_funding': round(self._total_gross_funding(), 4),
            'total_costs': round(self._total_costs(), 4),
            'positions': [asdict(p) for p in self.open_positions.values()],
        }

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

# Per-venue funding settlement interval. Binance/Bybit settle every 8h, but
# Kraken Futures settles HOURLY — modeling it as 8h made positions all-or-
# nothing at the 8h boundary, so a perp whose funding flipped a few hours after
# entry showed cycles=0 and ate the full entry cost having collected nothing.
# Hourly accrual banks the funding actually earned before a flip. NOTE: funding
# APY is annualized, so total funding over a given hold is interval-INDEPENDENT;
# a shorter interval does not raise expectancy, it only finishes the all-or-
# nothing cycle-0 wipeouts by accruing in finer increments before a flip.
KRAKEN_FUNDING_INTERVAL_HOURS = float(
    os.getenv('FUNDING_ARB_KRAKEN_INTERVAL_HOURS', '1')
)

# Fraction of entry funding still credited while a position is OFF the scanner.
# 0 = book nothing we can't observe (honest default; off-scanner means funding
# fell below the scanner's threshold). Was hardcoded 0.25 (optimistic phantom).
OFFSCANNER_RATE_FRAC = float(os.getenv('FUNDING_ARB_OFFSCANNER_RATE_FRAC', '0.0'))


def _funding_interval_hours(exchange: Optional[str]) -> float:
    """Funding settlement interval (hours) for a venue. Kraken Futures = 1h."""
    if exchange and 'kraken' in exchange.lower():
        return KRAKEN_FUNDING_INTERVAL_HOURS
    return float(FUNDING_CYCLE_HOURS)

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
# close(spot)+close(perp). Ignoring this is exactly the cost-blind mistake that
# produced the bot's old ~1% win rate (see strategy_cost_expectancy_fix). We
# deduct it ONCE at entry and report PnL net of it.
#
# Per-arm reality (set via cost_frac=… or FUNDING_ARB_*_COST_FRAC env vars):
#   • Aggressive arm (Binance/Bybit, RESEARCH BASELINE — rates not capturable
#     for this account): 0.22% default. Reflects Binance/Bybit retail taker
#     fees, which is the wrong basis for live execution. Kept as-is so the
#     comparison ledger doesn't shift; treat the +$X figure as fantasy.
#   • Majors arm (same source, SAME CAVEAT): 0.08% default. Honest-maker-cost
#     on Binance/Bybit, but again rates aren't capturable here.
#   • Kraken arm (the only ACTUALLY capturable arm): 0.64% default
#     (FUNDING_ARB_KRAKEN_COST_FRAC). Honest Kraken retail: maker spot 0.25%
#     ×2 + maker perp 0.02% ×2 + ~0.10% slippage. This is the one to trust.
ROUND_TRIP_COST_FRAC = float(os.getenv('FUNDING_ARB_COST_FRAC', '0.0022'))  # 0.22%
# Don't open unless funding at the entry rate clears round-trip cost within this
# many 8h cycles — i.e. the position is expected to be net-positive well inside
# MAX_HOLD. At 0.22% cost this implies an effective floor of ~24% APY after costs.
MAX_BREAKEVEN_CYCLES = float(os.getenv('FUNDING_ARB_MAX_BREAKEVEN_CYCLES', '10'))

# ── Conviction-weighted sizing ───────────────────────────────────────────────
# Instead of a flat size per position, scale capital by opportunity quality:
# the further an opportunity's |APY| sits above the cost-gate floor (~24% at
# defaults) toward the cap, the larger the position. Best risk-adjusted yields
# get the most capital, marginal ones the least. Bounded both per-position and
# in total so it can never over-concentrate. Env-tunable.
MIN_POSITION_USD   = float(os.getenv('FUNDING_ARB_MIN_SIZE', '250'))
MAX_POSITION_USD   = float(os.getenv('FUNDING_ARB_MAX_SIZE', '1000'))
MAX_TOTAL_NOTIONAL = float(os.getenv('FUNDING_ARB_MAX_TOTAL', '3000'))


# Liquid majors/mid-caps for the conservative "honest" arm — names with deep
# spot+perp markets where a positive-funding cash-and-carry is genuinely retail-
# executable. Widened from the original 10 majors to ~30 (research: the arm was
# idle because positive-funding among only 10 majors rarely cleared the cost gate;
# more liquid names = more chances WITHOUT relaxing any clean constraint — it's
# still positive-funding-only, maker cost, and the per-coin delta-neutral structure
# that has no price risk). The newer mid-caps have slightly shallower spot than the
# top majors, so "believable" is marginally softer for those — an intended trade.
# Override with FUNDING_ARB_MAJORS="BTC,ETH,..." (comma-separated base symbols).
_DEFAULT_MAJORS = {
    # original liquid majors
    'BTC', 'ETH', 'SOL', 'BNB', 'XRP', 'DOGE', 'ADA', 'AVAX', 'LINK', 'LTC',
    # widened: established large/mid-caps with deep spot + perp on major venues
    'DOT', 'TRX', 'BCH', 'NEAR', 'ATOM', 'UNI', 'APT', 'ARB', 'OP', 'FIL',
    'ICP', 'INJ', 'SUI', 'AAVE', 'ETC', 'HBAR', 'XLM', 'RENDER', 'TIA', 'SEI',
}
_env_majors = os.getenv('FUNDING_ARB_MAJORS', '').strip()
MAJOR_SYMBOLS = ({s.strip().upper() for s in _env_majors.split(',') if s.strip()}
                 if _env_majors else _DEFAULT_MAJORS)

# Spot-borrow carry for short-spot legs (SHORT_SPOT_LONG_PERP, i.e. negative-
# funding trades). Shorting spot requires borrowing the asset, which costs a
# daily fee the old model ignored entirely — it charged only a one-off entry
# cost and zero carry, so negative-funding microcap shorts looked free and
# inflated the aggressive arm's P&L. Majors borrow cheap; illiquid alts/microcaps
# borrow dear (often uneconomic). Applied per funding cycle while the short is
# open. Set both to 0 to restore the old optimistic (borrow-free) baseline.
BORROW_APY_MAJOR = float(os.getenv('FUNDING_ARB_BORROW_APY_MAJOR', '10'))
BORROW_APY_ALT = float(os.getenv('FUNDING_ARB_BORROW_APY_ALT', '50'))


def _base_symbol(symbol: str) -> str:
    """Strip exchange prefix + quote suffix → base asset.

    'BTCUSDT' → 'BTC' (Binance/Bybit)
    'ETHUSD'  → 'ETH'
    'PF_SOLUSD' → 'SOL' (Kraken Futures multi-collateral perp prefix)
    'PF_XBTUSD' → 'BTC' (Kraken lists Bitcoin as XBT, not BTC)
    """
    s = symbol.upper()
    if s.startswith('PF_'):       # Kraken Futures multi-collateral perp
        s = s[3:]
    for quote in ('USDT', 'USDC', 'USD'):
        if s.endswith(quote):
            s = s[: -len(quote)]
            break
    # Normalise exchange-specific aliases to a canonical base so symbol
    # allowlists match across venues. Kraken uses XBT for Bitcoin (and XDG for
    # Dogecoin); without this, PF_XBTUSD never matches a 'BTC' allowlist entry.
    return {'XBT': 'BTC', 'XDG': 'DOGE'}.get(s, s)


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
    borrow_cost: float = 0.0         # cumulative spot-borrow carry (short-spot legs)
    cycles_collected: int = 0
    # Funding settlement interval for this position's venue (hours). Persisted
    # so restored positions keep their cadence. Defaults to the 8h standard;
    # Kraken Futures positions are stamped 1h at open. See _funding_interval_hours.
    funding_interval_hours: float = float(FUNDING_CYCLE_HOURS)
    last_funding_ts_iso: Optional[str] = None
    # last_seen_iso tracks the most recent tick on which the scanner returned
    # data for this symbol.  It is ONLY updated when current_opp is not None,
    # so _should_exit's off_scanner_24h check can use it as a true "last seen"
    # timestamp — unlike last_funding_ts_iso, which is updated even when the
    # symbol is off-scanner (decayed accrual), causing the 24h gate to never fire.
    last_seen_iso: Optional[str] = None
    closed: bool = False
    close_time_iso: Optional[str] = None
    close_reason: Optional[str] = None

    @property
    def entry_time(self) -> datetime:
        return datetime.fromisoformat(self.entry_time_iso)

    @property
    def net_pnl(self) -> float:
        """Funding collected minus transaction cost and spot-borrow carry."""
        return self.funding_collected - self.entry_cost - self.borrow_cost


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
        cost_frac: float = ROUND_TRIP_COST_FRAC,
        max_breakeven_cycles: float = MAX_BREAKEVEN_CYCLES,
        positive_funding_only: bool = False,
        symbol_allowlist: Optional[set] = None,
        source_allowlist: Optional[set] = None,
        state_file: Optional[Path] = None,
        label: str = "Funding Arb",
        min_position_usd: float = MIN_POSITION_USD,
        max_position_usd: float = MAX_POSITION_USD,
        max_total_notional: float = MAX_TOTAL_NOTIONAL,
        history=None,
        min_persistence_cycles: float = 0.0,
        max_flips: int = 10**9,
        flip_cooldown_hours: float = 0.0,
    ):
        self.scanner = scanner
        self.notifier = notifier
        self.min_entry_apy = min_entry_apy
        self.max_entry_apy = max_entry_apy
        self.position_size_usd = position_size_usd
        self.max_positions = max_positions
        # Funding-persistence gate (FundingHistory). When min_persistence_cycles
        # > 0, an entry also requires the symbol's funding to have held positive
        # for that many 8h cycles AND not flipped more than max_flips times in
        # the retention window. This is the real fix for the cycle-0 flip bleed:
        # the breakeven gate assumes persistence, this one VERIFIES it from
        # recorded history. min_persistence_cycles=0 disables it (default).
        self.history = history
        self.min_persistence_cycles = min_persistence_cycles
        self.max_flips = max_flips
        # Realized-outcome feedback loop: when a position closes at a NET LOSS
        # (typically funding_flipped at cycle 0), the symbol is stamped with a
        # cooldown and cannot be re-entered until flip_cooldown_hours elapse.
        # This is the missing guard behind the bleed — e.g. PF_DEXEUSD flipped,
        # then got re-entered two days later and flipped AGAIN for another loss.
        # The breakeven/persistence gates reason about the funding SERIES; this
        # one reasons about OUR OWN realized P&L on the symbol. 0 disables it.
        self.flip_cooldown_hours = flip_cooldown_hours
        # Per-arm conviction-sizing band (defaults to the module globals). Set
        # min==max==total for an all-in single-position arm: every entry uses
        # the full allocation, no conviction scaling. The Kraken arm uses this
        # for the "aggressive, all money on one trade" config.
        self.min_position_usd = min_position_usd
        self.max_position_usd = max_position_usd
        self.max_total_notional = max_total_notional
        # Per-instance config so the same class can run an aggressive arm
        # (all symbols, both funding sides) AND a conservative "honest" arm
        # (liquid majors, positive funding only, maker-fee cost) at once.
        self.cost_frac = cost_frac
        # Per-arm breakeven gate. Entry requires funding at the entry rate to
        # clear round-trip cost within this many 8h cycles — i.e. it encodes a
        # funding-PERSISTENCE assumption: we only open if the position is
        # expected net-positive within a hold we believe the rate survives.
        # The aggressive/majors arms keep the lax module default (10). The
        # Kraken arm (honest 0.64% cost) is set much tighter because its
        # microcap funding empirically flips at cycle 0 — see memory
        # funding_arb_kraken_bleed.
        self.max_breakeven_cycles = max_breakeven_cycles
        self.positive_funding_only = positive_funding_only
        self.symbol_allowlist = symbol_allowlist   # set of BASE symbols, e.g. {'BTC','ETH'}
        # Source allowlist restricts to opportunities from specific exchanges,
        # e.g. {'Kraken Futures'} for an arm that only opens on actually-tradeable
        # rates for a US-geo-blocked account.
        self.source_allowlist = source_allowlist
        self.state_file = state_file or STATE_FILE
        self.label = label

        self.open_positions: Dict[str, PaperPosition] = {}
        self.closed_positions: List[PaperPosition] = []
        # symbol_key -> iso timestamp of the most recent net-loss close.
        self._flip_cooldowns: Dict[str, str] = {}
        self.start_time = datetime.now(timezone.utc)
        self.last_rollup_total = 0.0       # cumulative P&L at last rollup
        self.running = False

        self._load_state()

    # ── persistence ────────────────────────────────────────────────────────────

    def _load_state(self):
        if not self.state_file.exists():
            return
        try:
            data = json.loads(self.state_file.read_text())
            self.open_positions = {
                k: PaperPosition(**v) for k, v in data.get('open', {}).items()
            }
            self.closed_positions = [
                PaperPosition(**v) for v in data.get('closed', [])
            ]
            self.last_rollup_total = float(data.get('last_rollup_total', 0.0))
            self._flip_cooldowns = dict(data.get('flip_cooldowns', {}))
            start_iso = data.get('start_time_iso')
            if start_iso:
                self.start_time = datetime.fromisoformat(start_iso)
            logger.info(
                f"[{self.label}] Restored {len(self.open_positions)} open, "
                f"{len(self.closed_positions)} closed positions"
            )
        except Exception as e:
            logger.warning(f"[{self.label}] State load failed: {e}")

    def _save_state(self):
        self.state_file.parent.mkdir(exist_ok=True)
        payload = {
            'open': {k: asdict(v) for k, v in self.open_positions.items()},
            'closed': [asdict(v) for v in self.closed_positions[-500:]],
            'last_rollup_total': self.last_rollup_total,
            'flip_cooldowns': self._flip_cooldowns,
            'start_time_iso': self.start_time.isoformat(),
        }
        self.state_file.write_text(json.dumps(payload, indent=2))

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
        mode = "positive-funding only" if self.positive_funding_only else "both sides"
        universe = ("majors " + "/".join(sorted(self.symbol_allowlist))
                    if self.symbol_allowlist else "all symbols")
        sources = ("sources=" + ",".join(sorted(self.source_allowlist))
                   if self.source_allowlist else "sources=all")
        logger.info(
            f"[{self.label}] Started (paper). {mode}, {universe}, {sources}. "
            f"floor~{self._apy_floor():.0f}% cap={self.max_entry_apy:.0f}% "
            f"cost={self.cost_frac*100:.2f}% max_positions={self.max_positions} "
            f"rollup={ROLLUP_INTERVAL_SECONDS/3600:.0f}h"
        )

        rollup_task = asyncio.create_task(self._rollup_loop())

        try:
            while self.running:
                try:
                    self._tick()
                except Exception as e:
                    logger.error(f"[{self.label}] Tick error: {e}")
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

        # Record this snapshot into the shared funding-history tracker so the
        # persistence gate has data to reason about (downsampled + throttled
        # internally). Recording the full opp list captures every symbol.
        if self.history is not None:
            self.history.record_many(opps, now)
            self.history.maybe_save(now)

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
                # Feedback loop: stamp a re-entry cooldown on any net-loss close
                # so we stop immediately re-buying a symbol that just cost us money.
                if self.flip_cooldown_hours > 0 and pos.net_pnl < 0:
                    self._flip_cooldowns[key] = now.isoformat()
                    logger.info(
                        f"[{self.label}] COOLDOWN {pos.symbol} ({pos.exchange}) "
                        f"net=${pos.net_pnl:+.4f} → no re-entry for "
                        f"{self.flip_cooldown_hours:.0f}h"
                    )
                logger.info(
                    f"[{self.label}] CLOSE {pos.symbol} ({pos.exchange}) "
                    f"reason={reason} funding=${pos.funding_collected:.4f} "
                    f"cost=${pos.entry_cost:.4f} net=${pos.net_pnl:+.4f} "
                    f"cycles={pos.cycles_collected}"
                )
                emoji = "✅" if pos.net_pnl >= 0 else "❌"
                self._notify(
                    f"{emoji} <b>{self.label} CLOSE</b>\n"
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
                # Realized-loss cooldown: skip symbols that recently closed at a
                # net loss (the re-flip guard). Silent — checked every tick, so
                # logging here would spam.
                remaining = self._cooldown_remaining_h(key, now)
                if remaining is not None:
                    continue
                # Source-restricted arm: only consider opportunities from
                # allowlisted exchanges (e.g. {'Kraken Futures'} for an arm whose
                # rates are actually capturable by this account).
                if self.source_allowlist is not None and \
                        opp['exchange'] not in self.source_allowlist:
                    continue
                # Conservative arm: positive funding only (long spot / short perp
                # → no spot borrow needed, so genuinely retail-executable).
                if self.positive_funding_only and opp['apy'] <= 0:
                    continue
                # Conservative arm: restrict to a liquid-majors allowlist.
                if self.symbol_allowlist is not None and \
                        _base_symbol(opp['symbol']) not in self.symbol_allowlist:
                    continue
                if abs(opp['apy']) < self.min_entry_apy:
                    continue
                if abs(opp['apy']) > self.max_entry_apy:
                    logger.info(
                        f"[{self.label}] SKIP {opp['symbol']} ({opp['exchange']}) "
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
                breakeven_cycles = self.cost_frac / rate_8h_frac
                if breakeven_cycles > self.max_breakeven_cycles:
                    logger.info(
                        f"[{self.label}] SKIP {opp['symbol']} ({opp['exchange']}) "
                        f"apy={opp['apy']:.1f}%: breakeven {breakeven_cycles:.1f} cycles "
                        f"> max {self.max_breakeven_cycles:.0f} (cost {self.cost_frac*100:.2f}% "
                        f"vs {rate_8h_frac*100:.4f}%/cycle)"
                    )
                    continue
                # Persistence gate: the breakeven gate above only ASSUMES the
                # funding lasts; this VERIFIES it from recorded history. Reject
                # unless funding has actually held positive for the required
                # cycles and the symbol isn't a serial flipper. This is the
                # real defence against the cycle-0 flip bleed.
                if self.history is not None and self.min_persistence_cycles > 0:
                    if not self.history.is_stable(
                        key, self.min_persistence_cycles, self.max_flips, now
                    ):
                        st = self.history.stats(key, now)
                        logger.info(
                            f"[{self.label}] SKIP {opp['symbol']} ({opp['exchange']}) "
                            f"apy={opp['apy']:.1f}%: persistence — consec={st['consec_cycles']:.1f}cyc "
                            f"(need {self.min_persistence_cycles:.0f}) flips={st['flips_30d']} "
                            f"(max {self.max_flips}) samples={st['samples']}"
                        )
                        continue
                self._open_position(opp, now)

        self._save_state()

    def _apy_floor(self) -> float:
        """The effective APY floor — the rate at which breakeven == the cost
        gate's max cycles. Below this the cost gate rejects entries, so it's the
        natural bottom of the conviction-sizing band. ≈24% at the 0.22% cost."""
        rate_floor = self.cost_frac / self.max_breakeven_cycles
        return rate_floor * 3 * 365 * 100

    def _size_for_apy(self, apy: float) -> float:
        """Conviction-weighted position size: scale linearly from MIN→MAX as
        |APY| moves from the cost floor up to the cap. Clamped to [MIN, MAX]."""
        floor = max(self.min_entry_apy, self._apy_floor())
        cap = self.max_entry_apy
        if cap <= floor:
            return self.min_position_usd
        q = (abs(apy) - floor) / (cap - floor)
        q = max(0.0, min(1.0, q))
        return self.min_position_usd + (self.max_position_usd - self.min_position_usd) * q

    def _total_notional(self) -> float:
        return sum(p.size_usd for p in self.open_positions.values())

    def _open_position(self, opp: dict, now: datetime):
        key = f"{opp['exchange']}:{opp['symbol']}"
        direction = (
            "LONG_SPOT_SHORT_PERP" if opp['apy'] > 0
            else "SHORT_SPOT_LONG_PERP"
        )
        # Conviction-weighted size, trimmed to stay within the total budget.
        size = self._size_for_apy(opp['apy'])
        remaining = self.max_total_notional - self._total_notional()
        if remaining < self.min_position_usd:
            logger.info(
                f"[{self.label}] SKIP {opp['symbol']} ({opp['exchange']}) "
                f"total notional ${self._total_notional():.0f} near cap "
                f"${self.max_total_notional:.0f}"
            )
            return
        size = min(size, remaining)
        entry_cost = self.cost_frac * size
        pos = PaperPosition(
            symbol=opp['symbol'],
            exchange=opp['exchange'],
            direction=direction,
            entry_apy=float(opp['apy']),
            entry_rate_8h=float(opp['rate_8h']) / 100.0,  # scanner stores as %
            size_usd=size,
            entry_time_iso=now.isoformat(),
            last_funding_ts_iso=now.isoformat(),
            last_seen_iso=now.isoformat(),
            entry_cost=entry_cost,
            funding_interval_hours=_funding_interval_hours(opp['exchange']),
        )
        self.open_positions[key] = pos
        conviction_pct = pos.size_usd / self.max_position_usd * 100
        logger.info(
            f"[{self.label}] OPEN {pos.symbol} ({pos.exchange}) "
            f"apy={pos.entry_apy:.1f}% dir={direction} size=${pos.size_usd:.0f} "
            f"(conviction {conviction_pct:.0f}% of max) entry_cost=${entry_cost:.4f}"
        )
        dir_label = ("long spot / short perp" if direction == "LONG_SPOT_SHORT_PERP"
                     else "short spot / long perp")
        self._notify(
            f"📈 <b>{self.label} OPEN</b>\n"
            f"{pos.exchange} {pos.symbol}\n"
            f"{pos.entry_apy:+.0f}% APY · {dir_label}\n"
            f"size <b>${pos.size_usd:.0f}</b> "
            f"(conviction {conviction_pct:.0f}% of ${self.max_position_usd:.0f} max) · "
            f"cost ${entry_cost:.2f}\n"
            f"<i>collects funding every {pos.funding_interval_hours:.0f}h while open</i>"
        )

    def _accrue_funding(
        self,
        pos: PaperPosition,
        current_opp: Optional[dict],
        now: datetime,
    ):
        """Accrue funding for any 8h cycles that have elapsed since last accrual."""
        # Advance last_seen unconditionally when scanner data is present.  This
        # must happen BEFORE the cycles_due early-return so that ticks inside the
        # first 8h window (cycles_due == 0) still refresh the timestamp.
        # last_seen_iso is the sole input to the off_scanner_24h exit check; it
        # must NOT be updated on decayed-rate (off-scanner) ticks so the 24h clock
        # correctly reflects scanner absence rather than accrual cadence.
        if current_opp is not None:
            pos.last_seen_iso = now.isoformat()

        last_ts = (
            datetime.fromisoformat(pos.last_funding_ts_iso)
            if pos.last_funding_ts_iso else pos.entry_time
        )
        interval_h = pos.funding_interval_hours or float(FUNDING_CYCLE_HOURS)
        hours_since = (now - last_ts).total_seconds() / 3600.0
        cycles_due = int(hours_since // interval_h)
        if cycles_due <= 0:
            return

        # Use current rate if scanner still tracks the symbol, otherwise stop
        # crediting funding. A symbol leaves the scanner because its funding fell
        # below the scanner's threshold — i.e. we can no longer observe it paying.
        # The old 0.25×entry-rate credit was optimistic PHANTOM income that
        # inflated the arms' P&L. Default 0 (book nothing we can't see); set
        # FUNDING_ARB_OFFSCANNER_RATE_FRAC>0 to restore a research fraction.
        # NOTE: borrow carry below still accrues — you owe it regardless of scanner.
        if current_opp is not None:
            current_rate_per_cycle = float(current_opp['rate_8h']) / 100.0
        else:
            current_rate_per_cycle = pos.entry_rate_8h * OFFSCANNER_RATE_FRAC

        # We collect funding (we're on the receiving side by construction).
        # Sign of entry_rate tells us which side; magnitude × size = $/8h-cycle.
        # Rates are stored per 8h; scale to this venue's interval so total funding
        # over a given hold is the same regardless of cadence (annualized rate).
        interval_frac = interval_h / float(FUNDING_CYCLE_HOURS)
        per_cycle_pnl = abs(current_rate_per_cycle) * pos.size_usd * interval_frac
        pos.funding_collected += per_cycle_pnl * cycles_due
        pos.cycles_collected += cycles_due

        # Spot-borrow carry: only short-spot legs (SHORT_SPOT_LONG_PERP) pay it,
        # since you must borrow the asset to short it. Long-spot legs own the
        # asset outright, so they carry nothing. Charged per elapsed cycle for as
        # long as the short is held (accrues even when off-scanner — you still
        # owe borrow on the open short).
        if pos.direction == "SHORT_SPOT_LONG_PERP":
            borrow_apy = (BORROW_APY_MAJOR if _base_symbol(pos.symbol) in MAJOR_SYMBOLS
                          else BORROW_APY_ALT)
            borrow_per_cycle = (borrow_apy / 100.0) * pos.size_usd * \
                (interval_h / (24.0 * 365.0))
            pos.borrow_cost += borrow_per_cycle * cycles_due

        pos.last_funding_ts_iso = (
            last_ts + timedelta(hours=cycles_due * interval_h)
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
        # Use last_seen_iso (updated only when scanner returns data) rather than
        # last_funding_ts_iso (updated on every decayed-rate accrual tick too),
        # so the 24h window reflects actual scanner absence, not accrual cadence.
        if current_opp is None:
            last_seen_ts = (
                datetime.fromisoformat(pos.last_seen_iso)
                if pos.last_seen_iso else pos.entry_time
            )
            if (now - last_seen_ts).total_seconds() > 86400:
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
                    logger.error(f"[{self.label}] Rollup error: {e}")
        except asyncio.CancelledError:
            pass

    def _cooldown_remaining_h(self, key: str, now: datetime) -> Optional[float]:
        """Hours left on a symbol's post-loss re-entry cooldown, or None if it's
        clear (or the feature is disabled). Expired entries are pruned on read."""
        if self.flip_cooldown_hours <= 0:
            return None
        ts_iso = self._flip_cooldowns.get(key)
        if not ts_iso:
            return None
        elapsed_h = (now - datetime.fromisoformat(ts_iso)).total_seconds() / 3600.0
        if elapsed_h >= self.flip_cooldown_hours:
            del self._flip_cooldowns[key]      # expired — forget it
            return None
        return self.flip_cooldown_hours - elapsed_h

    def _total_pnl(self) -> float:
        """Cumulative NET P&L (funding collected minus transaction costs)."""
        open_pnl = sum(p.net_pnl for p in self.open_positions.values())
        closed_pnl = sum(p.net_pnl for p in self.closed_positions)
        return open_pnl + closed_pnl

    @staticmethod
    def _borrow_owed(pos: PaperPosition) -> float:
        """Spot-borrow carry a short-spot leg SHOULD have paid over its whole
        life, recomputed fresh from the tiered borrow APY — independent of what
        was actually charged. Positions opened before the borrow model existed
        paid $0 carry, which inflated their net; this exposes the true cost.
        Returns 0 for long-spot legs (you own the asset, nothing to borrow)."""
        if pos.direction != "SHORT_SPOT_LONG_PERP":
            return 0.0
        borrow_apy = (BORROW_APY_MAJOR if _base_symbol(pos.symbol) in MAJOR_SYMBOLS
                      else BORROW_APY_ALT)
        interval_h = pos.funding_interval_hours or float(FUNDING_CYCLE_HOURS)
        return ((borrow_apy / 100.0) * pos.size_usd
                * (pos.cycles_collected * interval_h / (24.0 * 365.0)))

    def borrow_corrected_pnl(self) -> float:
        """Lifetime NET P&L with EVERY short-spot leg charged the borrow it owes
        (not just what was booked). For positive-funding-only arms this equals
        _total_pnl; for the aggressive arm it strips the unpaid-carry illusion in
        legacy short-spot trades that closed before the borrow model deployed."""
        total = 0.0
        for p in list(self.open_positions.values()) + self.closed_positions:
            total += p.funding_collected - p.entry_cost - self._borrow_owed(p)
        return total

    def net_pnl_since(self, cutoff: datetime) -> float:
        """NET P&L from positions CLOSED at/after `cutoff`, plus all currently
        open positions. Lifetime cumulative conflates dead config eras with the
        live one (e.g. the Kraken arm's -$27 is almost entirely pre-whitelist
        legacy); a rolling window shows what the CURRENT config is actually doing."""
        closed = sum(
            p.net_pnl for p in self.closed_positions
            if p.close_time_iso
            and datetime.fromisoformat(p.close_time_iso) >= cutoff
        )
        open_pnl = sum(p.net_pnl for p in self.open_positions.values())
        return closed + open_pnl

    def _total_gross_funding(self) -> float:
        return (sum(p.funding_collected for p in self.open_positions.values())
                + sum(p.funding_collected for p in self.closed_positions))

    def _total_costs(self) -> float:
        return (sum(p.entry_cost + p.borrow_cost for p in self.open_positions.values())
                + sum(p.entry_cost + p.borrow_cost for p in self.closed_positions))

    def _send_daily_rollup(self):
        total = self._total_pnl()
        delta = total - self.last_rollup_total
        now = datetime.now(timezone.utc)
        uptime_h = (now - self.start_time).total_seconds() / 3600.0
        interval_h = ROLLUP_INTERVAL_SECONDS / 3600.0

        open_lines = [
            f"  • {p.exchange} {p.symbol}: net ${p.net_pnl:+.4f} "
            f"(funding ${p.funding_collected:.4f} − cost ${p.entry_cost:.4f}"
            + (f" − borrow ${p.borrow_cost:.4f}" if p.borrow_cost else "")
            + f", {p.cycles_collected} cycles, {p.entry_apy:.0f}% APY)"
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
            f"<b>📊 {self.label} — P&amp;L rollup (net of costs)</b>\n"
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

        logger.info(f"[{self.label}] rollup: ${delta:+.4f} (cum ${total:.4f})")

        if self.notifier:
            try:
                self.notifier.send_message(msg)
            except Exception as e:
                logger.warning(f"[{self.label}] Telegram send failed: {e}")

        self.last_rollup_total = total
        self._save_state()

    # ── public helpers ─────────────────────────────────────────────────────────

    def get_summary(self) -> dict:
        return {
            'open_positions': len(self.open_positions),
            'closed_positions': len(self.closed_positions),
            'total_pnl': round(self._total_pnl(), 4),          # NET of costs (lifetime)
            'borrow_corrected_pnl': round(self.borrow_corrected_pnl(), 4),  # honest short-spot carry
            'pnl_7d': round(self.net_pnl_since(
                datetime.now(timezone.utc) - timedelta(days=7)), 4),  # rolling window
            'total_gross_funding': round(self._total_gross_funding(), 4),
            'total_costs': round(self._total_costs(), 4),
            'cooldowns_active': sum(
                1 for k in list(self._flip_cooldowns)
                if self._cooldown_remaining_h(k, datetime.now(timezone.utc)) is not None
            ),
            'positions': [asdict(p) for p in self.open_positions.values()],
        }

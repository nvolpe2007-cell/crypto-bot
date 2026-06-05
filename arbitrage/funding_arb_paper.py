"""
Funding Rate Arbitrage — Paper Trading Engine

Market-neutral carry: long spot + short perp, collect funding every 8h.
Paper P&L is deliberately conservative (maker fees, liquid majors only,
no phantom accrual) so it's a believable estimate of real performance.

Improvements over naive implementations:
  A. Exit on absolute APY floor (~8.8%), not 40%-of-entry — holds winners
  B. Position size scales with funding persistence, not raw APY magnitude
  C. Off-scanner symbols stop accruing immediately (no phantom income)
"""

import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass, field
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

# ── Entry gates ────────────────────────────────────────────────────────────────
MIN_ENTRY_APY        = 15.0          # minimum APY to open
MAX_ENTRY_APY        = float(os.getenv('FUNDING_ARB_MAX_APY', '150.0'))  # spike filter
ROUND_TRIP_COST_FRAC = 0.0008        # 0.08% maker fees (both legs combined)
MAX_BREAKEVEN_CYCLES = 10            # funding must recover round-trip within N 8h cycles
# Absolute APY floor = (ROUND_TRIP_COST_FRAC / MAX_BREAKEVEN_CYCLES) × 3 × 365 × 100 ≈ 8.8%
EXIT_APY_FLOOR       = round(ROUND_TRIP_COST_FRAC / MAX_BREAKEVEN_CYCLES * 3 * 365 * 100, 1)

MIN_PERSIST_CYCLES   = 2             # symbol must appear on scanner ≥N cycles before entry
COOLDOWN_HOURS       = 48            # rest after a losing trade on a symbol
MAX_HOLD_DAYS        = 7             # force-close after this many days

# ── Portfolio limits ───────────────────────────────────────────────────────────
MAX_OPEN_POSITIONS = 3
BASE_POSITION_USD  = 375.0
MAX_POSITION_USD   = 1500.0

# ── Liquid majors allowlist ────────────────────────────────────────────────────
MAJORS_ALLOWLIST = {
    'BTCUSDT', 'ETHUSDT', 'SOLUSDT', 'BNBUSDT', 'XRPUSDT',
    'DOGEUSDT', 'ADAUSDT', 'AVAXUSDT', 'DOTUSDT', 'MATICUSDT',
    'LINKUSDT', 'LTCUSDT', 'UNIUSDT', 'ATOMUSDT', 'NEARUSDT',
    'FTMUSDT', 'SANDUSDT', 'MANAUSDT', 'AAVEUSDT', 'COMPUSDT',
    'MKRUSDT', 'CRVUSDT', 'SUSHIUSDT', 'YFIUSDT', 'SNXUSDT',
    'ALGOUSDT', 'VETUSDT', 'FILUSDT', 'ICPUSDT', 'TRXUSDT',
}


def _size_for_persistence(persist_cycles: int) -> float:
    """
    Improvement B: size by funding durability, not raw APY.
    2 cycles (minimum) → $375.  10+ cycles → $1500.
    Spiky high-APY names that just appeared get the smallest bet.
    """
    factor = min(1.0, (persist_cycles - MIN_PERSIST_CYCLES) / 8.0)
    return round(BASE_POSITION_USD + factor * (MAX_POSITION_USD - BASE_POSITION_USD), 2)


@dataclass
class ArbPosition:
    symbol:             str
    exchange:           str
    entry_time:         datetime
    entry_apy:          float        # APY at entry (annualised %)
    entry_rate_8h:      float        # raw 8h rate at entry (fractional, e.g. 0.0003)
    size_usd:           float
    persist_cycles:     int          # scanner appearances at entry — determines sizing
    round_trip_cost:    float        # entry + exit fees reserved at open
    funding_collected:  float = 0.0
    cycles_accrued:     int   = 0
    last_seen_on_scanner: Optional[datetime] = None  # set when symbol leaves scanner


@dataclass
class ArbAccount:
    initial_capital: float
    cash:            float
    closed_pnl:      float = 0.0
    total_fees:      float = 0.0
    wins:            int   = 0
    losses:          int   = 0


class FundingArbPaper:
    """
    Paper trading engine for the funding-rate carry strategy.

    Usage:
        engine = FundingArbPaper(initial_capital=5000)
        # Pass FundingScanner.get_state as the state function
        await engine.start(scanner.get_state)
    """

    def __init__(self, initial_capital: float = 5000.0, notifier=None):
        self.account   = ArbAccount(initial_capital=initial_capital, cash=initial_capital)
        self.notifier  = notifier
        self.positions: Dict[str, ArbPosition] = {}
        self.running   = False

        self._persist_count: Dict[str, int]      = {}   # symbol → consecutive scan cycles seen
        self._loss_cooldown: Dict[str, datetime] = {}   # symbol → cooldown-until timestamp

    # ── Public ─────────────────────────────────────────────────────────────────

    async def start(self, scanner_state_fn, poll_interval: int = 60):
        """
        Main loop. `scanner_state_fn` is a zero-arg callable returning a list of
        opportunity dicts (keys: symbol, exchange, apy, rate_8h, action, timestamp).
        """
        self.running = True
        logger.info(f"[FundingArb] Paper engine started — floor={EXIT_APY_FLOOR}% APY  cap={MAX_ENTRY_APY}%")
        while self.running:
            try:
                opps = scanner_state_fn()
                self._tick(opps)
            except Exception as e:
                logger.error(f"[FundingArb] tick error: {e}", exc_info=True)
            await asyncio.sleep(poll_interval)

    def stop(self):
        self.running = False

    def get_summary(self) -> dict:
        unrealised = sum(p.funding_collected - p.round_trip_cost for p in self.positions.values())
        total_trades = self.account.wins + self.account.losses
        return {
            'cash':           round(self.account.cash, 2),
            'unrealised_pnl': round(unrealised, 2),
            'equity':         round(self.account.cash + unrealised, 2),
            'closed_pnl':     round(self.account.closed_pnl, 2),
            'total_fees':     round(self.account.total_fees, 2),
            'open_positions': len(self.positions),
            'wins':           self.account.wins,
            'losses':         self.account.losses,
            'win_rate':       round(self.account.wins / total_trades * 100, 1) if total_trades else 0.0,
        }

    def get_positions(self) -> List[dict]:
        now = datetime.now(timezone.utc)
        return [
            {
                'symbol':            sym,
                'exchange':          p.exchange,
                'entry_apy':         round(p.entry_apy, 1),
                'size_usd':          round(p.size_usd, 2),
                'funding_collected': round(p.funding_collected, 4),
                'hold_hours':        round((now - p.entry_time).total_seconds() / 3600, 1),
                'persist_cycles':    p.persist_cycles,
                'on_scanner':        p.last_seen_on_scanner is None,
            }
            for sym, p in self.positions.items()
        ]

    # ── Internal ───────────────────────────────────────────────────────────────

    def _tick(self, opps: List[dict]):
        now = datetime.now(timezone.utc)
        current_symbols = {o['symbol'] for o in opps if o.get('apy', 0) > 0}

        # Update persistence counters: increment if seen, reset if absent
        for sym in current_symbols:
            self._persist_count[sym] = self._persist_count.get(sym, 0) + 1
        for sym in list(self._persist_count):
            if sym not in current_symbols:
                self._persist_count[sym] = 0

        # Mark positions that just dropped off the scanner (improvement C)
        for sym, pos in self.positions.items():
            if sym not in current_symbols and pos.last_seen_on_scanner is None:
                pos.last_seen_on_scanner = now
                logger.info(f"[FundingArb] {sym} left scanner — stopping accrual")

        # Accrue funding for open positions
        for pos in list(self.positions.values()):
            self._accrue_funding(pos, opps, now)

        # Check exits
        for sym in list(self.positions.keys()):
            reason = self._exit_reason(self.positions[sym], opps, now)
            if reason:
                self._close(sym, reason, now)

        # Open new positions up to the limit
        if len(self.positions) < MAX_OPEN_POSITIONS:
            opp_map = {o['symbol']: o for o in opps if o.get('apy', 0) > 0}
            for sym, opp in sorted(opp_map.items(), key=lambda x: -x[1].get('apy', 0)):
                if len(self.positions) >= MAX_OPEN_POSITIONS:
                    break
                if sym not in self.positions and self._should_enter(sym, opp, now):
                    self._open(sym, opp, now)

    def _accrue_funding(self, pos: ArbPosition, opps: List[dict], now: datetime):
        """
        Improvement C: off-scanner positions accrue nothing.
        Previously this credited 25% of entry rate for up to 24h — optimistic
        phantom P&L because symbols usually leave the scanner due to rate collapse.
        """
        hours = (now - pos.entry_time).total_seconds() / 3600
        expected_cycles = int(hours / 8)
        if expected_cycles <= pos.cycles_accrued:
            return

        new_cycles = expected_cycles - pos.cycles_accrued
        pos.cycles_accrued = expected_cycles

        # Off scanner → no accrual
        if pos.last_seen_on_scanner is not None:
            return

        # Use live rate from scanner
        live = next((o for o in opps if o['symbol'] == pos.symbol), None)
        if not live or live.get('apy', 0) <= 0:
            return

        rate_8h = live['rate_8h'] / 100  # scanner stores as %, convert to fraction
        pos.funding_collected += rate_8h * pos.size_usd * new_cycles

    def _exit_reason(self, pos: ArbPosition, opps: List[dict], now: datetime) -> Optional[str]:
        """
        Improvement A: exit when live APY drops below EXIT_APY_FLOOR (~8.8%),
        not when it drops below 40% of entry APY.

        Old EXIT_APY_FRACTION=0.40 closed at 40% of entry (e.g. 40% APY on a
        100% entry), which is still 4.5× above breakeven — needlessly paying a
        round-trip and often re-entering shortly after.

        New: hold as long as carry is genuinely profitable above the cost floor.
        """
        if (now - pos.entry_time).days >= MAX_HOLD_DAYS:
            return 'MAX_HOLD'

        if pos.last_seen_on_scanner and (now - pos.last_seen_on_scanner).total_seconds() > 86400:
            return 'OFF_SCANNER_24H'

        live = next((o for o in opps if o['symbol'] == pos.symbol), None)
        if live:
            current_apy = live.get('apy', 0)
            if current_apy < EXIT_APY_FLOOR:
                return f'APY_FLOOR ({current_apy:.1f}% < {EXIT_APY_FLOOR:.1f}%)'
        elif pos.last_seen_on_scanner is None:
            # Still expect to see it — no exit yet
            pass

        return None

    def _should_enter(self, symbol: str, opp: dict, now: datetime) -> bool:
        apy     = opp.get('apy', 0)
        rate_8h = opp.get('rate_8h', 0) / 100

        if not (MIN_ENTRY_APY <= apy <= MAX_ENTRY_APY):
            return False
        if symbol not in MAJORS_ALLOWLIST:
            return False
        if self._persist_count.get(symbol, 0) < MIN_PERSIST_CYCLES:
            return False
        # Funding over the breakeven window must exceed round-trip cost
        if rate_8h * MAX_BREAKEVEN_CYCLES < ROUND_TRIP_COST_FRAC:
            return False
        cooldown = self._loss_cooldown.get(symbol)
        if cooldown and now < cooldown:
            return False
        size = _size_for_persistence(self._persist_count.get(symbol, MIN_PERSIST_CYCLES))
        if size > self.account.cash:
            return False
        return True

    def _open(self, symbol: str, opp: dict, now: datetime):
        rate_8h  = opp.get('rate_8h', 0) / 100
        persist  = self._persist_count.get(symbol, MIN_PERSIST_CYCLES)
        size     = _size_for_persistence(persist)
        cost     = size * ROUND_TRIP_COST_FRAC

        self.account.cash       -= size
        self.account.total_fees += cost / 2  # entry leg

        pos = ArbPosition(
            symbol          = symbol,
            exchange        = opp.get('exchange', ''),
            entry_time      = now,
            entry_apy       = opp.get('apy', 0),
            entry_rate_8h   = rate_8h,
            size_usd        = size,
            persist_cycles  = persist,
            round_trip_cost = cost,
        )
        self.positions[symbol] = pos
        logger.info(
            f"[FundingArb] OPEN  {symbol:<12} {opp['apy']:>6.1f}% APY  "
            f"${size:.0f}  persist={persist} cycles"
        )

    def _close(self, symbol: str, reason: str, now: datetime):
        pos = self.positions.pop(symbol)
        exit_fee = pos.size_usd * ROUND_TRIP_COST_FRAC / 2
        self.account.total_fees += exit_fee

        pnl = pos.funding_collected - pos.round_trip_cost
        self.account.cash       += pos.size_usd + pnl
        self.account.closed_pnl += pnl

        if pnl >= 0:
            self.account.wins += 1
        else:
            self.account.losses += 1
            self._loss_cooldown[symbol] = now + timedelta(hours=COOLDOWN_HOURS)

        hold_h = (now - pos.entry_time).total_seconds() / 3600
        logger.info(
            f"[FundingArb] CLOSE {symbol:<12} {reason}  "
            f"pnl=${pnl:+.2f}  funding=${pos.funding_collected:.4f}  hold={hold_h:.1f}h"
        )

        if self.notifier:
            fn = self.notifier.send_win if pnl >= 0 else self.notifier.send_loss
            try:
                fn(symbol, pnl, pnl / pos.size_usd * 100,
                   pos.entry_apy, self.account.cash, reason=reason)
            except Exception:
                pass

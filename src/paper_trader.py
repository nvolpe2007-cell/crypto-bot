"""
Paper trading execution engine — extracted from paper_trading.py.

PaperPosition / PaperAccount / PaperTrader: the cost-aware paper fill/PnL model
(spot + perp, fees, slippage, partial exits, liquidation). Pure execution — no
strategy or loop logic — so it lives on its own and is imported back by
paper_trading.py (which re-exports it, so existing `from src.paper_trading import
PaperTrader` imports keep working).
"""
from __future__ import annotations

import logging
import math
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional

from .backtester import Trade
from .exchange import ExchangeConnection, CircuitBreakerOpen
from .notifications import TelegramNotifier

logger = logging.getLogger(__name__)

# Kraken Futures maintenance margin rate.  A position is liquidated when the
# unrealized loss consumes (1 - MAINT_MARGIN) of the initial margin.
# Long liq_price  = entry × (1 − (1−MAINT) / leverage)
# Short liq_price = entry × (1 + (1−MAINT) / leverage)
_PERP_MAINT_MARGIN = float(os.getenv("PERP_MAINT_MARGIN", "0.02"))


@dataclass
class PaperPosition:
    entry_time:      datetime
    entry_price:     float
    size:            float
    side:            str
    entry_fee:       float = 0.0
    unrealized_pnl:  float = 0.0
    entry_signal:    Optional[ScientificSignal] = None   # full signal context
    # Excursion tracking (updated each tick)
    peak_favorable_price: float = 0.0    # best price for the position
    peak_adverse_price:   float = 0.0    # worst price for the position
    # Entry pathway: 'main' / 'mr' / 'mr-extreme' / 'fast-track'
    entry_path:      str = 'main'
    # Pre-trade context snapshot
    size_usd_target: float = 0.0
    spread_at_entry: float = 0.0
    sentiment_fng:   Optional[int]   = None
    sentiment_btc_dom: Optional[float] = None
    # Probability gate output
    prob_win:        float = 0.0
    edges_used:      List[str] = field(default_factory=list)
    # Conviction tier + trailing stop state (set on entry, updated each tick)
    tier:                str   = 'scalp'
    intended_hold_min:   int   = 0
    trail_style:         str   = 'atr_stop'
    trail_stop_price:    float = 0.0
    target_usd_at_entry: float = 0.0
    # Perp-only state (zero in spot mode)
    is_perp:             bool  = False
    leverage:            float = 1.0
    margin_locked:       float = 0.0    # USD locked as margin (= notional / leverage)
    funding_accrued:     float = 0.0    # cumulative funding paid (long) or collected (short)
    last_funding_ts:     Optional[datetime] = None
    liquidation_price:   float = 0.0    # price at which the exchange force-closes (0 = no liq)


@dataclass
class PaperAccount:
    initial_capital: float
    cash:            float
    positions:       Dict[str, PaperPosition] = field(default_factory=dict)
    closed_trades:   List[Trade]              = field(default_factory=list)
    total_pnl:       float = 0.0


class PaperTrader:
    def __init__(self, initial_capital: float = 100.0,
                 position_size: float = 50.0,     # kept for compat; scientific uses equity %
                 fee_pct: float = 0.40,
                 slippage_pct: float = 0.1,
                 stop_loss_pct: float = 2.0,
                 take_profit_pct: float = 3.0,
                 perp_mode: bool = False,
                 leverage: float = 1.0,
                 allow_spot_shorts: bool = True):
        self.initial_capital  = initial_capital
        self.account          = PaperAccount(initial_capital=initial_capital, cash=initial_capital)
        self.position_size    = position_size
        self.fee_pct          = fee_pct / 100
        self.slippage_pct     = slippage_pct / 100   # floor / fallback
        self.stop_loss_pct    = stop_loss_pct / 100
        self.take_profit_pct  = take_profit_pct / 100
        self.running          = False
        self._started_at: Optional[str] = None
        # Live spread cache populated by paper_trading main loop; used for realistic slippage
        self.live_spreads: Dict[str, float] = {}   # symbol → current spread in price units
        # ── Perp mode state ───────────────────────────────────────────────
        self.perp_mode        = perp_mode
        self.leverage         = max(1.0, float(leverage)) if perp_mode else 1.0
        # Fee defaults match Kraken Pro lowest tier ($0–10K 30d vol): 0.40% spot
        # taker, 0.05% futures taker. Spot tier is what a $500 account actually
        # pays; futures tier is what live perps would cost (US retail can't
        # access Kraken Futures, but the funding-arb paper sim still uses it).
        if self.perp_mode:
            self.fee_pct = min(self.fee_pct, float(os.getenv('PERP_TAKER_FEE_PCT', '0.05')) / 100)
        # US Kraken spot has no shorting/margin for retail. When this flag is
        # False, execute_short refuses in spot mode so paper P&L reflects what
        # the user could actually replicate on a Kraken Pro spot account.
        self.allow_spot_shorts = bool(allow_spot_shorts)
        # Symbol → current 8h funding rate (fraction, e.g. 0.0001). Caller updates.
        self._funding_rates: Dict[str, float] = {}
        if perp_mode:
            logger.info(f"[PaperTrader] PERP mode ON  leverage={self.leverage:.1f}x")

    # ── Perp funding helpers ──────────────────────────────────────────────

    def set_funding_rate(self, symbol: str, rate_8h_fraction: float) -> None:
        """Update the current 8h funding rate (as a fraction, e.g. 0.0001 = 0.01%)."""
        self._funding_rates[symbol] = float(rate_8h_fraction)

    def accrue_funding(self, now: datetime) -> None:
        """
        Accrue funding for all open perp positions across any 8h cycles
        elapsed since the last accrual. Long pays positive funding, short collects.
        Called each tick from the main loop; no-op outside perp mode.
        """
        if not self.perp_mode:
            return
        for symbol, pos in self.account.positions.items():
            if not pos.is_perp:
                continue
            rate = self._funding_rates.get(symbol)
            if rate is None:
                continue
            last_ts = pos.last_funding_ts or pos.entry_time
            hours = (now - last_ts).total_seconds() / 3600.0
            cycles = int(hours // 8)
            if cycles <= 0:
                continue
            notional = pos.entry_price * pos.size
            # Long pays positive funding → -rate*notional per cycle
            # Short collects positive funding → +rate*notional per cycle
            sign = -1.0 if pos.side == 'buy' else 1.0
            delta = sign * rate * notional * cycles
            pos.funding_accrued += delta
            pos.last_funding_ts = last_ts + timedelta(hours=cycles * 8)
            # Funding erodes (or grows) effective margin, which shifts the
            # liquidation boundary. For longs paying funding (delta < 0), margin
            # shrinks so liq_price rises toward entry. For shorts collecting
            # funding (delta > 0), margin grows so liq_price also rises (moves
            # further above entry, making liquidation harder to trigger).
            if pos.liquidation_price > 0 and pos.size > 0:
                if pos.side == 'buy':
                    pos.liquidation_price -= (1.0 - _PERP_MAINT_MARGIN) * delta / pos.size
                else:
                    pos.liquidation_price += (1.0 - _PERP_MAINT_MARGIN) * delta / pos.size

    def _liquidate(self, symbol: str, liq_price: float, timestamp: datetime) -> Optional['Trade']:
        """Force-close a perp position at exactly liq_price (no additional slippage).

        The exchange marks the position at the maintenance-margin boundary; the
        trader loses almost all margin.  Entry fee was already deducted from cash
        at open, so we add it back here to avoid double-counting (same pattern as
        execute_sell / execute_cover).
        """
        if symbol not in self.account.positions:
            return None
        pos = self.account.positions[symbol]
        if not pos.is_perp:
            return None
        self.accrue_funding(timestamp)
        exit_fee   = liq_price * pos.size * self.fee_pct
        total_fees = exit_fee + pos.entry_fee
        if pos.side == 'buy':
            pnl = (liq_price - pos.entry_price) * pos.size - total_fees + pos.funding_accrued
        else:
            pnl = (pos.entry_price - liq_price) * pos.size - total_fees + pos.funding_accrued
        cost_basis = pos.margin_locked + pos.entry_fee
        self.account.cash += pos.margin_locked + pos.entry_fee + pnl
        pnl_pct = pnl / cost_basis * 100 if cost_basis else 0.0
        self.account.total_pnl += pnl
        trade = Trade(entry_time=pos.entry_time, exit_time=timestamp,
                      entry_price=pos.entry_price, exit_price=liq_price,
                      size=pos.size, side='liquidation', pnl=pnl,
                      pnl_pct=pnl_pct, fees=total_fees)
        self.account.closed_trades.append(trade)
        del self.account.positions[symbol]
        logger.warning(
            f"[LIQUIDATED] {symbol} @ ${liq_price:,.2f}  PnL ${pnl:+.2f} ({pnl_pct:+.2f}%)"
            f"  margin_lost=${pos.margin_locked:.2f}"
            f"  funding=${pos.funding_accrued:+.4f}"
        )
        return trade

    def _slippage_pct_for(self, symbol: str, price: float) -> float:
        """
        Realistic slippage = max(floor, 0.5 × spread_pct).
        On a market order you cross half the spread on entry, half on exit; thin pairs / wide spreads
        give more slippage. Falls back to flat self.slippage_pct when no spread data.
        """
        spread = self.live_spreads.get(symbol, 0.0)
        if spread > 0 and price > 0:
            spread_pct = spread / price
            # Cap slippage to avoid pathological book reads (max 0.5%)
            return max(self.slippage_pct, min(0.005, spread_pct * 0.5))
        return self.slippage_pct

    def execute_buy(self, symbol: str, price: float, timestamp: datetime,
                    size_usd: float, signal: Optional[ScientificSignal] = None) -> Optional[PaperPosition]:
        size       = size_usd / price
        slip       = self._slippage_pct_for(symbol, price)
        exec_price = price * (1 + slip)
        fee        = exec_price * size * self.fee_pct
        notional   = exec_price * size
        margin_req = notional / self.leverage if self.perp_mode else notional
        total_cost = margin_req + fee

        if total_cost > self.account.cash:
            # Scale down to fit available cash
            available  = self.account.cash * 0.98
            # cash >= notional/lev + notional*fee_pct  →  notional <= cash / (1/lev + fee_pct)
            denom      = (1.0 / self.leverage) + self.fee_pct if self.perp_mode else (1.0 + self.fee_pct)
            notional   = available / denom
            size       = notional / exec_price
            fee        = notional * self.fee_pct
            margin_req = notional / self.leverage if self.perp_mode else notional
            total_cost = margin_req + fee

        if size <= 0 or total_cost > self.account.cash:
            return None

        liq_price = (
            exec_price * (1.0 - (1.0 - _PERP_MAINT_MARGIN) / self.leverage)
            if self.perp_mode else 0.0
        )
        self.account.cash -= total_cost
        pos = PaperPosition(entry_time=timestamp, entry_price=exec_price,
                            size=size, side='buy', entry_fee=fee, entry_signal=signal,
                            peak_favorable_price=exec_price,
                            peak_adverse_price=exec_price,
                            size_usd_target=size_usd,
                            is_perp=self.perp_mode,
                            leverage=self.leverage,
                            margin_locked=margin_req,
                            last_funding_ts=timestamp,
                            liquidation_price=liq_price)
        self.account.positions[symbol] = pos
        tag = "[LONG-PERP]" if self.perp_mode else "[BUY]"
        liq_note = f"  liq=${liq_price:,.2f}" if self.perp_mode else ""
        logger.info(f"{tag} {symbol} @ ${exec_price:,.2f}  notional=${notional:.2f} margin=${margin_req:.2f}{liq_note}  conf={signal.confidence:.0f}%" if signal else f"{tag} {symbol} @ ${exec_price:,.2f}{liq_note}")
        return pos

    def execute_sell(self, symbol: str, price: float, timestamp: datetime,
                     reason: str = "signal") -> Optional[Trade]:
        if symbol not in self.account.positions:
            return None
        pos        = self.account.positions[symbol]
        # Final funding accrual on the position before closing it
        if pos.is_perp:
            self.accrue_funding(timestamp)
        slip       = self._slippage_pct_for(symbol, price)
        exec_price = price * (1 - slip)
        exit_fee   = exec_price * pos.size * self.fee_pct
        total_fees = exit_fee + pos.entry_fee
        pnl        = (exec_price - pos.entry_price) * pos.size - total_fees + pos.funding_accrued
        if pos.is_perp:
            cost_basis = pos.margin_locked + pos.entry_fee
            # Return margin + entry_fee (already deducted at open) plus net pnl.
            # pnl already deducts entry_fee via total_fees, so we add it back here
            # to avoid double-counting it against cash.
            self.account.cash += pos.margin_locked + pos.entry_fee + pnl
        else:
            cost_basis = pos.entry_price * pos.size + pos.entry_fee
            self.account.cash += exec_price * pos.size - exit_fee
        pnl_pct    = pnl / cost_basis * 100 if cost_basis else 0.0
        self.account.total_pnl += pnl
        trade = Trade(entry_time=pos.entry_time, exit_time=timestamp,
                      entry_price=pos.entry_price, exit_price=exec_price,
                      size=pos.size, side='sell', pnl=pnl, pnl_pct=pnl_pct, fees=total_fees)
        self.account.closed_trades.append(trade)
        del self.account.positions[symbol]
        tag = "[CLOSE-LONG]" if pos.is_perp else "[SELL]"
        funding_note = f" funding=${pos.funding_accrued:+.4f}" if pos.is_perp else ""
        logger.info(f"{tag} {symbol} @ ${exec_price:,.2f}  PnL ${pnl:+.2f} ({pnl_pct:+.2f}%){funding_note}  {reason}")
        return trade

    def execute_short(self, symbol: str, price: float, timestamp: datetime,
                      size_usd: float, signal: Optional[ScientificSignal] = None) -> Optional[PaperPosition]:
        if not self.perp_mode and not self.allow_spot_shorts:
            logger.info(f"[SKIP SHORT] {symbol} — Kraken Pro spot has no retail shorting")
            return None
        size       = size_usd / price
        slip       = self._slippage_pct_for(symbol, price)
        exec_price = price * (1 - slip)
        fee        = exec_price * size * self.fee_pct
        notional   = exec_price * size
        margin_req = notional / self.leverage if self.perp_mode else notional
        total_cost = margin_req + fee

        if total_cost > self.account.cash:
            available  = self.account.cash * 0.98
            denom      = (1.0 / self.leverage) + self.fee_pct if self.perp_mode else (1.0 + self.fee_pct)
            notional   = available / denom
            size       = notional / exec_price
            fee        = notional * self.fee_pct
            margin_req = notional / self.leverage if self.perp_mode else notional
            total_cost = margin_req + fee

        if size <= 0 or total_cost > self.account.cash:
            return None

        liq_price = (
            exec_price * (1.0 + (1.0 - _PERP_MAINT_MARGIN) / self.leverage)
            if self.perp_mode else 0.0
        )
        self.account.cash -= total_cost
        pos = PaperPosition(entry_time=timestamp, entry_price=exec_price,
                            size=size, side='short', entry_fee=fee, entry_signal=signal,
                            peak_favorable_price=exec_price,
                            peak_adverse_price=exec_price,
                            size_usd_target=size_usd,
                            is_perp=self.perp_mode,
                            leverage=self.leverage,
                            margin_locked=margin_req,
                            last_funding_ts=timestamp,
                            liquidation_price=liq_price)
        self.account.positions[symbol] = pos
        tag = "[SHORT-PERP]" if self.perp_mode else "[SHORT]"
        liq_note = f"  liq=${liq_price:,.2f}" if self.perp_mode else ""
        logger.info(f"{tag} {symbol} @ ${exec_price:,.2f}  notional=${notional:.2f} margin=${margin_req:.2f}{liq_note}  conf={signal.confidence:.0f}%" if signal else f"{tag} {symbol} @ ${exec_price:,.2f}{liq_note}")
        return pos

    def execute_cover(self, symbol: str, price: float, timestamp: datetime,
                      reason: str = "signal") -> Optional[Trade]:
        if symbol not in self.account.positions:
            return None
        pos = self.account.positions[symbol]
        if pos.side != 'short':
            return self.execute_sell(symbol, price, timestamp, reason)
        if pos.is_perp:
            self.accrue_funding(timestamp)
        slip        = self._slippage_pct_for(symbol, price)
        exec_price  = price * (1 + slip)
        exit_fee    = exec_price * pos.size * self.fee_pct
        total_fees  = exit_fee + pos.entry_fee
        pnl         = (pos.entry_price - exec_price) * pos.size - total_fees + pos.funding_accrued
        if pos.is_perp:
            cost_basis = pos.margin_locked + pos.entry_fee
            self.account.cash += pos.margin_locked + pos.entry_fee + pnl
        else:
            cost_basis = pos.entry_price * pos.size + pos.entry_fee
            returned   = pos.entry_price * pos.size + pos.entry_fee
            self.account.cash += returned + pnl
        pnl_pct = pnl / cost_basis * 100 if cost_basis else 0.0
        self.account.total_pnl += pnl
        trade = Trade(entry_time=pos.entry_time, exit_time=timestamp,
                      entry_price=pos.entry_price, exit_price=exec_price,
                      size=pos.size, side='cover', pnl=pnl, pnl_pct=pnl_pct, fees=total_fees)
        self.account.closed_trades.append(trade)
        del self.account.positions[symbol]
        tag = "[CLOSE-SHORT]" if pos.is_perp else "[COVER]"
        funding_note = f" funding=${pos.funding_accrued:+.4f}" if pos.is_perp else ""
        logger.info(f"{tag} {symbol} @ ${exec_price:,.2f}  PnL ${pnl:+.2f} ({pnl_pct:+.2f}%){funding_note}  {reason}")
        return trade

    def execute_partial_sell(self, symbol: str, price: float, timestamp: datetime,
                             fraction: float = 0.5) -> Optional[float]:
        """Close `fraction` of a long position.

        Returns pnl_partial (net of exit fee) or None when no matching position.
        Cash is credited with proceeds minus the exit fee, matching execute_sell
        semantics so that partial + final close produce identical accounting to a
        single full close at the same prices.
        """
        if symbol not in self.account.positions:
            return None
        pos = self.account.positions[symbol]
        if pos.side != 'buy':
            return None
        partial_size = pos.size * fraction
        slip = self._slippage_pct_for(symbol, price)
        exec_price = price * (1 - slip)
        exit_fee = exec_price * partial_size * self.fee_pct
        pnl_partial = (exec_price - pos.entry_price) * partial_size - exit_fee
        pos.size -= partial_size
        self.account.cash += exec_price * partial_size - exit_fee
        self.account.total_pnl += pnl_partial
        logger.info(f"[PARTIAL SELL] {symbol} @ ${exec_price:,.2f}  "
                    f"size={partial_size:.6f}  pnl=${pnl_partial:+.4f}")
        return pnl_partial

    def execute_partial_cover(self, symbol: str, price: float, timestamp: datetime,
                              fraction: float = 0.5) -> Optional[float]:
        """Close `fraction` of a short position.

        Returns pnl_partial (net of exit fee) or None when no matching position.
        Cash formula mirrors execute_cover: releases entry_price × partial_size of
        the locked collateral and adds the net P&L (which includes the exit fee),
        so that partial + final cover produce identical accounting to a single full
        cover at the same prices.
        """
        if symbol not in self.account.positions:
            return None
        pos = self.account.positions[symbol]
        if pos.side != 'short':
            return None
        partial_size = pos.size * fraction
        slip = self._slippage_pct_for(symbol, price)
        exec_price = price * (1 + slip)
        exit_fee = exec_price * partial_size * self.fee_pct
        pnl_partial = (pos.entry_price - exec_price) * partial_size - exit_fee
        pos.size -= partial_size
        # Release proportional collateral and return the net P&L for the covered
        # portion.  Equivalent to (2×entry - exec) × partial - exit_fee, which
        # mirrors execute_cover's "returned + pnl" formula on a pro-rated basis.
        self.account.cash += pos.entry_price * partial_size + pnl_partial
        self.account.total_pnl += pnl_partial
        logger.info(f"[PARTIAL COVER] {symbol} @ ${exec_price:,.2f}  "
                    f"size={partial_size:.6f}  pnl=${pnl_partial:+.4f}")
        return pnl_partial

    def update_unrealized_pnl(self, prices: Dict[str, float]) -> List[str]:
        """Update unrealized PnL and excursion stats for all open positions.

        Returns a list of symbols that were liquidated this tick (perp only).
        Callers may use this to send alerts; existing callers that ignore the
        return value are unaffected.
        """
        to_liquidate: List[str] = []
        for sym, pos in self.account.positions.items():
            if sym not in prices:
                continue
            p = prices[sym]
            raw = (p - pos.entry_price) * pos.size if pos.side == 'buy' else (pos.entry_price - p) * pos.size
            # Perp positions accrue funding continuously; include it so that
            # get_account_summary() and the daily circuit breaker see the true
            # equity (funding paid by longs reduces equity, collected by shorts
            # increases it) before the position is closed.
            pos.unrealized_pnl = raw + pos.funding_accrued if pos.is_perp else raw
            # Track excursions (favorable = direction we want, adverse = against us)
            if pos.side == 'buy':
                if p > pos.peak_favorable_price: pos.peak_favorable_price = p
                if p < pos.peak_adverse_price:   pos.peak_adverse_price   = p
            else:   # short
                if p < pos.peak_favorable_price: pos.peak_favorable_price = p
                if p > pos.peak_adverse_price:   pos.peak_adverse_price   = p
            # Liquidation check — only for perp positions that have a computed boundary
            if pos.is_perp and pos.liquidation_price > 0:
                if pos.side == 'buy' and p <= pos.liquidation_price:
                    to_liquidate.append(sym)
                elif pos.side == 'short' and p >= pos.liquidation_price:
                    to_liquidate.append(sym)

        liquidated: List[str] = []
        for sym in to_liquidate:
            if sym in self.account.positions:
                liq_price = self.account.positions[sym].liquidation_price
                self._liquidate(sym, liq_price, datetime.now(timezone.utc))
                liquidated.append(sym)
        return liquidated

    def get_account_summary(self) -> Dict:
        # Perp positions: only margin_locked is the actual capital at risk, not full notional.
        # Spot positions: full notional (entry_price * size) is the capital deployed.
        pos_val  = sum(
            (p.margin_locked if p.is_perp else p.entry_price * p.size) + p.unrealized_pnl
            for p in self.account.positions.values()
        )
        equity   = self.account.cash + pos_val
        closed   = self.account.closed_trades
        return {
            'cash':           self.account.cash,
            'total_equity':   equity,
            'total_pnl':      self.account.total_pnl,
            'pnl_pct':        (self.account.total_pnl / self.initial_capital) * 100,
            'open_positions': len(self.account.positions),
            'closed_trades':  len(closed),
            'winning_trades': len([t for t in closed if t.pnl > 0]),
            'losing_trades':  len([t for t in closed if t.pnl <= 0]),
        }

    def print_summary(self):
        s = self.get_account_summary()
        print("\n" + "=" * 50)
        print("PAPER TRADING ACCOUNT")
        print("=" * 50)
        print(f"Cash:           ${s['cash']:.2f}")
        print(f"Total Equity:   ${s['total_equity']:.2f}")
        print(f"Total PnL:      ${s['total_pnl']:.2f} ({s['pnl_pct']:.2f}%)")
        print(f"Trades:         {s['closed_trades']}  ({s['winning_trades']}W / {s['losing_trades']}L)")
        print("=" * 50)


# ── Entry-funnel instrumentation ─────────────────────────────────────────────────────────────────────────────────
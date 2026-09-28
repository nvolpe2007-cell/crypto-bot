"""
Polymarket paper-trading simulator — infra only, no real money.

Mirrors this repo's existing arm-state convention (see arbitrage/funding_arb_paper.py,
lev_perp_paper.py) so it slots into dashboard_data.py's auto-discovery of
data/*_state.json unmodified: {"equity", "start_equity", "open": [...], "closed": [...]}.

Signal is a PLACEHOLDER (see `signal.py`) — this module only wires the
market data -> paper position -> settlement plumbing. Do not point this at
a real wallet; there is no order-signing or execution path here at all.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

from polymarket_bot.client import BinaryMarket, get_midpoint_price

logger = logging.getLogger(__name__)

STATE_FILE = Path("data/polymarket_paper_state.json")

START_EQUITY = 1000.0
POSITION_SIZE_USD = 50.0
# Polymarket taker fee is 0 at time of writing on most markets, but the CLOB
# charges a spread; model a conservative round-trip cost so paper PnL isn't
# fantasy-optimistic. Revisit against real fee schedule before ever going live.
ASSUMED_ROUND_TRIP_COST_FRAC = 0.02


@dataclass
class PaperPosition:
    condition_id: str
    question: str
    side: str  # "YES" or "NO"
    token_id: str
    entry_price: float
    size_usd: float
    opened_at: str
    # Actual taker fee paid at entry. When set, settlement charges it win OR
    # lose (that is when Polymarket takes it) instead of the flat legacy cost.
    fee_usd: Optional[float] = None
    window_end: Optional[str] = None


@dataclass
class PolymarketPaperState:
    equity: float = START_EQUITY
    start_equity: float = START_EQUITY
    open: List[dict] = field(default_factory=list)
    closed: List[dict] = field(default_factory=list)

    @classmethod
    def load(cls) -> "PolymarketPaperState":
        if not STATE_FILE.exists():
            return cls()
        try:
            data = json.loads(STATE_FILE.read_text())
            return cls(
                equity=data.get("equity", START_EQUITY),
                start_equity=data.get("start_equity", START_EQUITY),
                open=data.get("open", []),
                closed=data.get("closed", []),
            )
        except Exception:
            logger.exception("failed to load %s, starting fresh", STATE_FILE)
            return cls()

    def save(self) -> None:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps(asdict(self), indent=2, default=str))


def open_paper_position(
    state: PolymarketPaperState,
    market: BinaryMarket,
    side: str,
    size_usd: float = POSITION_SIZE_USD,
    entry_price: Optional[float] = None,
    fee_usd: Optional[float] = None,
    window_end: Optional[str] = None,
) -> Optional[PaperPosition]:
    """Open a paper position. No real order is placed.

    `entry_price` should be the book's best ask (what a taker actually pays);
    it falls back to the midpoint, which flatters paper P&L by half a spread.
    """
    token_id = market.yes_token_id if side == "YES" else market.no_token_id
    if not token_id:
        logger.warning("market %s missing token_id for side=%s", market.slug, side)
        return None

    price = entry_price if entry_price is not None else get_midpoint_price(token_id)
    if price is None:
        return None

    pos = PaperPosition(
        condition_id=market.condition_id,
        question=market.question,
        side=side,
        token_id=token_id,
        entry_price=price,
        size_usd=size_usd,
        opened_at=datetime.now(timezone.utc).isoformat(),
        fee_usd=fee_usd,
        window_end=window_end,
    )
    state.open.append(asdict(pos))
    state.save()
    return pos


def settle_position(state: PolymarketPaperState, condition_id: str, resolved_yes: bool) -> None:
    """Settle a paper position once the market resolves (outcome = 1 or 0)."""
    remaining = []
    for raw in state.open:
        if raw["condition_id"] != condition_id:
            remaining.append(raw)
            continue

        won = (raw["side"] == "YES") == resolved_yes
        payout = raw["size_usd"] / raw["entry_price"] if won else 0.0
        if raw.get("fee_usd") is not None:
            pnl = payout - raw["size_usd"] - raw["fee_usd"]
        else:
            cost = raw["size_usd"] * ASSUMED_ROUND_TRIP_COST_FRAC
            pnl = payout - raw["size_usd"] - cost if won else -raw["size_usd"]

        state.equity += pnl
        state.closed.append(
            {
                **raw,
                "resolved_yes": resolved_yes,
                "pnl": round(pnl, 4),
                "closed_at": datetime.now(timezone.utc).isoformat(),
            }
        )

    state.open = remaining
    state.save()

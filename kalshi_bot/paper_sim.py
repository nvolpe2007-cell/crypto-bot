"""
Kalshi paper book — no real money, no order path.

Same arm-state shape as the other paper arms ({"equity", "start_equity",
"open", "closed"}) so src/dashboard_data.py auto-discovers
data/kalshi_paper_state.json.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import List

logger = logging.getLogger(__name__)

STATE_FILE = Path("data/kalshi_paper_state.json")
START_EQUITY = 1000.0


@dataclass
class KalshiPaperState:
    equity: float = START_EQUITY
    start_equity: float = START_EQUITY
    open: List[dict] = field(default_factory=list)
    closed: List[dict] = field(default_factory=list)

    @classmethod
    def load(cls, path: Path = STATE_FILE) -> "KalshiPaperState":
        if not path.exists():
            return cls()
        try:
            d = json.loads(path.read_text())
            return cls(d.get("equity", START_EQUITY), d.get("start_equity", START_EQUITY),
                       d.get("open", []), d.get("closed", []))
        except Exception:
            logger.exception("failed to load %s, starting fresh", path)
            return cls()

    def save(self, path: Path = STATE_FILE) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(self), indent=2, default=str))
        tmp.replace(path)


def open_position(state: KalshiPaperState, ticker: str, side: str, ask: float, contracts: float,
                  fee_usd: float, close_time: str, **extra) -> dict:
    pos = {"ticker": ticker, "side": side, "entry_price": ask, "contracts": contracts,
           "size_usd": round(ask * contracts, 4), "fee_usd": fee_usd, "close_time": close_time,
           "opened_at": datetime.now(timezone.utc).isoformat(), **extra}
    state.open.append(pos)
    return pos


def settle(state: KalshiPaperState, ticker: str, result_yes: bool) -> List[dict]:
    """Settle every open position on `ticker`; the fee is paid win or lose."""
    done, remaining = [], []
    for p in state.open:
        if p["ticker"] != ticker:
            remaining.append(p)
            continue
        won = (p["side"] == "YES") == result_yes
        pnl = (p["contracts"] if won else 0.0) - p["size_usd"] - p["fee_usd"]
        state.equity += pnl
        row = {**p, "result_yes": result_yes, "won": won, "pnl": round(pnl, 4),
               "closed_at": datetime.now(timezone.utc).isoformat()}
        state.closed.append(row)
        done.append(row)
    state.open = remaining
    return done

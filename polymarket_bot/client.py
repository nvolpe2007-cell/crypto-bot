"""
Read-only Polymarket CLOB client.

Public market/price data only — no wallet, no API key, no order placement.
Placing a real order requires a funded Polygon wallet and signed orders via
py-clob-client; that is deliberately NOT implemented here (see WORKLOG /
polymarket_bot/README.md — infra-first, paper trading only for now).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Dict, List, Optional

import requests

logger = logging.getLogger(__name__)

GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"

_TIMEOUT = 10


@dataclass
class BinaryMarket:
    condition_id: str
    slug: str
    question: str
    yes_token_id: Optional[str]
    no_token_id: Optional[str]
    end_date_iso: Optional[str]
    active: bool
    closed: bool


def search_markets(query: str, limit: int = 20) -> List[BinaryMarket]:
    """Search active Gamma markets by keyword (e.g. 'bitcoin up or down')."""
    try:
        resp = requests.get(
            f"{GAMMA_API}/markets",
            params={"active": "true", "closed": "false", "limit": limit},
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        raw = resp.json()
    except Exception:
        logger.exception("polymarket market search failed")
        return []

    query_lower = query.lower()
    out: List[BinaryMarket] = []
    for m in raw:
        question = m.get("question", "")
        if query_lower not in question.lower():
            continue
        token_ids = m.get("clobTokenIds")
        yes_id = no_id = None
        if isinstance(token_ids, list) and len(token_ids) == 2:
            yes_id, no_id = token_ids[0], token_ids[1]
        out.append(
            BinaryMarket(
                condition_id=m.get("conditionId", ""),
                slug=m.get("slug", ""),
                question=question,
                yes_token_id=yes_id,
                no_token_id=no_id,
                end_date_iso=m.get("endDate"),
                active=bool(m.get("active")),
                closed=bool(m.get("closed")),
            )
        )
    return out


def get_midpoint_price(token_id: str) -> Optional[float]:
    """Current mid price (0-1, implied probability) for one outcome token."""
    try:
        resp = requests.get(
            f"{CLOB_API}/midpoint", params={"token_id": token_id}, timeout=_TIMEOUT
        )
        resp.raise_for_status()
        data = resp.json()
        return float(data["mid"])
    except Exception:
        logger.exception("polymarket midpoint fetch failed for token_id=%s", token_id)
        return None


def get_order_book(token_id: str) -> Optional[Dict]:
    """Raw order book (bids/asks) for one outcome token — read-only."""
    try:
        resp = requests.get(
            f"{CLOB_API}/book", params={"token_id": token_id}, timeout=_TIMEOUT
        )
        resp.raise_for_status()
        return resp.json()
    except Exception:
        logger.exception("polymarket book fetch failed for token_id=%s", token_id)
        return None

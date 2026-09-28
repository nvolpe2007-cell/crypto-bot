"""
Placeholder signal for the BTC up/down Polymarket bot.

Infra-first per owner's choice: this returns HOLD always. Do not wire this
to real capital. A real signal needs the same discipline the rest of this
repo applies before trusting anything (see CLAUDE.md "Core principle" and
the turnover/cost-screen literature notes) — short-duration binary markets
(e.g. 15-minute BTC up/down) have an even higher effective turnover than
the directional scalper that already failed that screen here, so the prior
should be that this needs strong justification, not the default assumption
that a signal is easy to find.
"""

from __future__ import annotations

from enum import Enum


class Decision(str, Enum):
    HOLD = "HOLD"
    YES = "YES"
    NO = "NO"


def decide(_btc_price_now: float, _btc_price_recent: list[float]) -> Decision:
    return Decision.HOLD

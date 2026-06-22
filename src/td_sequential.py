"""
TD Sequential (DeMark) — list-based, dependency-light, MEASURE-FIRST.

Computes TD Setup + a simplified TD Countdown over an ASCENDING list of OHLC bar
dicts ({"o","h","l","c"}). Pure functions, no pandas / requests / network — it
runs on the same 4h bars swing_paper.py already fetched, so it adds ZERO new data
sources or dependencies (matching swing_strategy.py's list-based style).

WHY THIS IS A TAG, NOT A GATE (read before wiring it into a decision):
TD Sequential is an unproven *directional/mean-reversion* predictor. The bot's
rule is that no directional overlay gates a trade until the bot's OWN ledger
proves it adds edge (see proof_scorecard.py, session_filter.py, the cost
principle in CLAUDE.md). So swing_paper.py only STAMPS the TD state on each entry,
and proof_scorecard breaks swing P&L down "by TD signal". If TD-aligned entries
measurably out-earn, THEN promote it to a soft check — not before. Note the swing
engine is a long-only TREND follower while a TD buy-setup is downtrend
EXHAUSTION, so the empirically useful question here is whether a recent TD
sell-setup (uptrend exhaustion) warns off a long — the attribution will tell us.

Definitions:
  Buy  Setup — consecutive closes each LESS than the close 4 bars prior (1..9)
  Sell Setup — consecutive closes each MORE than the close 4 bars prior (1..9)
  Countdown (simplified) — after a 9-setup, count bars whose close <= low 2 bars
    prior (buy) / close >= high 2 bars prior (sell); 13 completes, then resets.
"""
from __future__ import annotations

from typing import Dict, List

# How many recent bars to scan when deciding the "fresh signal" string. The swing
# runner tags an entry with the TD signal that fired on or shortly before it.
_RECENT = 6


def _setups(closes: List[float]) -> "tuple[list[int], list[int]]":
    """Per-bar TD buy/sell setup counts (a count resets to 0 when the streak
    breaks). Counts are allowed to exceed 9; callers read '== 9' / '>= 9'."""
    n = len(closes)
    buy = [0] * n
    sell = [0] * n
    for i in range(4, n):
        if closes[i] < closes[i - 4]:
            buy[i] = buy[i - 1] + 1 if buy[i - 1] > 0 else 1
            sell[i] = 0
        elif closes[i] > closes[i - 4]:
            sell[i] = sell[i - 1] + 1 if sell[i - 1] > 0 else 1
            buy[i] = 0
        # equal close → both reset (left at 0)
    return buy, sell


def _countdowns(closes: List[float], highs: List[float], lows: List[float],
                buy_setup: List[int], sell_setup: List[int]
                ) -> "tuple[list[int], list[int]]":
    """Per-bar TD countdown progress (0..13). Triggered by a 9-setup; reset to 0
    once 13 completes so a stale 13 doesn't linger on later bars."""
    n = len(closes)
    buy_cd = [0] * n
    sell_cd = [0] * n
    in_buy = in_sell = False
    bc = sc = 0
    for i in range(n):
        if buy_setup[i] == 9:
            in_buy, bc = True, 0
        if sell_setup[i] == 9:
            in_sell, sc = True, 0
        if in_buy and i >= 2 and closes[i] <= lows[i - 2]:
            bc += 1
        if in_sell and i >= 2 and closes[i] >= highs[i - 2]:
            sc += 1
        buy_cd[i] = bc if in_buy else 0
        sell_cd[i] = sc if in_sell else 0
        if in_buy and bc >= 13:
            in_buy, bc = False, 0
        if in_sell and sc >= 13:
            in_sell, sc = False, 0
    return buy_cd, sell_cd


def td_state(bars: List[dict], recent: int = _RECENT) -> Dict:
    """Latest TD state for an ascending OHLC list. Returns the most-recent bar's
    setup/countdown counts plus a `signal` string derived from the last `recent`
    bars (so a 9/13 that printed a few bars before the entry still registers).
    Short/empty input → a 'warmup' state with zero counts (fail-open)."""
    n = len(bars)
    if n < 5:
        return {"buy_setup": 0, "sell_setup": 0, "buy_countdown": 0,
                "sell_countdown": 0, "signal": "warmup"}
    closes = [float(b["c"]) for b in bars]
    highs = [float(b["h"]) for b in bars]
    lows = [float(b["l"]) for b in bars]

    buy_setup, sell_setup = _setups(closes)
    buy_cd, sell_cd = _countdowns(closes, highs, lows, buy_setup, sell_setup)

    lo = max(0, n - recent)
    # >= 9: a setup is "complete" at 9; on a long streak the raw count climbs past
    # 9, so an exact ==9 would miss a setup that completed a few bars before entry.
    recent_buy_setup = any(buy_setup[j] >= 9 for j in range(lo, n))
    recent_sell_setup = any(sell_setup[j] >= 9 for j in range(lo, n))
    recent_buy_cd = any(buy_cd[j] >= 13 for j in range(lo, n))
    recent_sell_cd = any(sell_cd[j] >= 13 for j in range(lo, n))

    # Countdown completion outranks a bare setup; buy checked before sell.
    if recent_buy_cd:
        signal = "buy_countdown_13"
    elif recent_sell_cd:
        signal = "sell_countdown_13"
    elif recent_buy_setup:
        signal = "buy_setup_9"
    elif recent_sell_setup:
        signal = "sell_setup_9"
    else:
        signal = "none"

    return {"buy_setup": int(buy_setup[-1]), "sell_setup": int(sell_setup[-1]),
            "buy_countdown": int(buy_cd[-1]), "sell_countdown": int(sell_cd[-1]),
            "signal": signal}

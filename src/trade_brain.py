"""
Trade brain — a Claude-powered DISCRETIONARY trader for its own paper book.

Unlike the mechanical arms (a fixed SMA rule), this consults Claude once per day
with the full market picture for BTC/ETH/SOL and lets it decide LONG / SHORT / FLAT
per coin, with a size and a written rationale. It runs its OWN $1k paper account and
is judged head-to-head against the mechanical arms by proof_scorecard — the honest
test of whether a "thinking" brain actually beats rules, or just adds expensive noise.

Design (mirrors src/altperp/ai_brain.py, which is the proven pattern here):
  • Anthropic client built lazily — importing this module never needs the SDK/key,
    so the test suite injects a fake client via TradeBrain(client=...).
  • FAIL-SAFE TO HOLD: any API error / no key / parse failure → empty decisions, and
    the runner simply keeps current positions (no churn, no crash). A broken brain
    costs nothing; it does not trade randomly.
  • Discipline is in the system prompt: cost (~0.15-0.3% round-trip) and perp funding
    dominate at this size, so OVERTRADING is the main way to lose. Hold or stay flat
    unless there is a real reason to be positioned.
"""

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

import os

MODEL = os.getenv("BRAIN_MODEL", "claude-sonnet-4-6")
MAX_TOKENS = int(os.getenv("BRAIN_MAX_TOKENS", "1500"))
TIMEOUT_SECS = float(os.getenv("BRAIN_TIMEOUT_SECS", "40"))

# Static strategy knowledge → cache_control so the daily call hits the prompt cache.
SYSTEM_PROMPT = """\
You are the decision core of a crypto PAPER trading account. Each day you decide, \
for BTC, ETH and SOL independently, whether to hold a LONG, a SHORT, or stay FLAT \
(in cash) over the coming days — and at what size. You trade 1x notional perps (no \
leverage); you CAN profit in downtrends by shorting.

YOU ARE BEING JUDGED HEAD-TO-HEAD against simple mechanical trend rules (long above \
a moving average, short below) by a strict statistical proof bar. To beat them you \
must add real judgment, not noise. The hard truths of this system, learned the \
expensive way:

  • COST DOMINATES. Each position change costs ~0.15% round-trip PLUS ~10%/yr funding \
    while held. Overtrading is THE way to lose money at this size. Most days the right \
    move is to do NOTHING (keep yesterday's position). Churn is the enemy.
  • TREND PROTECTS, SHORTING PROFITS. Across 1-2yr of real data: trend-following's \
    proven edge is DOWNSIDE PROTECTION (go flat/short before a drop), not extra upside. \
    In a bear, going FLAT preserves capital; only SHORTING actually makes money. In a \
    choppy range, both long and short get whipsawed — prefer FLAT.
  • DON'T FIGHT A CLEAN TREND. Above a rising SMA50/200 with positive momentum → bias \
    LONG. Below a falling SMA50/200 with negative momentum → bias SHORT or FLAT. \
    Conflicting signals (price above one MA, below another; flat momentum) → FLAT.
  • SOL is the most volatile and the least reliable of the three; demand a cleaner \
    setup there. BTC leads; if BTC is clearly trending, ETH/SOL usually follow.

HOW TO SIZE (size_mult scales a fixed base allocation, clamped 0.0-1.5 by the runner):
  • 1.0 = normal conviction in a clean trend.
  • up to 1.5 = strong, multi-signal agreement (price/MA/momentum all aligned).
  • 0.3-0.7 = real but mixed; want exposure but cautious.
  • action "flat" = no position (size ignored). Choosing flat is often correct.

For EVERY coin, call submit_decisions exactly once with one entry per coin. Be \
concrete: name the signal that decided it and what would flip your view. Prefer \
KEEPING the current position when the picture hasn't materially changed — say so."""

DECISION_TOOL = {
    "name": "submit_decisions",
    "description": "Submit one trade decision per coin (BTC, ETH, SOL).",
    "input_schema": {
        "type": "object",
        "properties": {
            "decisions": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "coin": {"type": "string", "enum": ["BTC", "ETH", "SOL"]},
                        "action": {"type": "string", "enum": ["long", "short", "flat"]},
                        "conviction": {"type": "integer", "minimum": 1, "maximum": 10},
                        "size_mult": {"type": "number", "minimum": 0.0, "maximum": 1.5},
                        "key_signal": {"type": "string",
                                       "description": "The single signal that drove this call."},
                        "invalidation": {"type": "string",
                                         "description": "What would flip this view."},
                        "reasoning": {"type": "string",
                                      "description": "Concise read — why long/short/flat."},
                    },
                    "required": ["coin", "action", "conviction", "size_mult",
                                 "key_signal", "invalidation", "reasoning"],
                },
            }
        },
        "required": ["decisions"],
    },
}


@dataclass
class CoinDecision:
    coin: str
    action: str = "flat"             # long / short / flat
    conviction: int = 0
    size_mult: float = 1.0
    key_signal: str = ""
    invalidation: str = ""
    reasoning: str = ""


@dataclass
class BrainResult:
    decisions: Dict[str, CoinDecision] = field(default_factory=dict)
    model: str = ""
    latency_ms: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    error: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.error is None and bool(self.decisions)


def _build_user_message(snapshot: Dict, now) -> str:
    payload = {
        "utc_time": now.isoformat() if hasattr(now, "isoformat") else str(now),
        "note": "Decide long/short/flat per coin. Holding yesterday's position is "
                "free of cost; changing it is not. Do nothing unless there's a reason.",
        "coins": snapshot,
    }
    return ("Here is today's market picture. Call submit_decisions with one entry "
            "per coin.\n\n```json\n" + json.dumps(payload, indent=2, default=str) + "\n```")


def _parse(resp) -> Dict[str, CoinDecision]:
    for block in getattr(resp, "content", []) or []:
        if (getattr(block, "type", None) == "tool_use"
                and getattr(block, "name", None) == "submit_decisions"):
            out: Dict[str, CoinDecision] = {}
            for d in (block.input or {}).get("decisions", []) or []:
                coin = str(d.get("coin", "")).upper()
                if not coin:
                    continue
                action = str(d.get("action", "flat")).lower()
                if action not in ("long", "short", "flat"):
                    action = "flat"
                out[coin] = CoinDecision(
                    coin=coin, action=action,
                    conviction=max(0, min(10, int(d.get("conviction", 0) or 0))),
                    size_mult=max(0.0, min(1.5, float(d.get("size_mult", 1.0) or 1.0))),
                    key_signal=str(d.get("key_signal", "")),
                    invalidation=str(d.get("invalidation", "")),
                    reasoning=str(d.get("reasoning", "")),
                )
            return out
    raise ValueError("no submit_decisions tool_use block in response")


class TradeBrain:
    """Wraps the Anthropic client. `client` may be injected for tests."""

    def __init__(self, api_key: Optional[str] = None, model: Optional[str] = None,
                 client: Any = None):
        self.model = model or MODEL
        self._client = client
        self._api_key = api_key if api_key is not None else os.getenv("ANTHROPIC_API_KEY", "")

    def available(self) -> bool:
        return self._client is not None or bool(self._api_key)

    def _get_client(self):
        if self._client is None:
            import anthropic  # lazy: importing this module never requires the SDK
            if not self._api_key:
                raise RuntimeError("ANTHROPIC_API_KEY not set")
            self._client = anthropic.Anthropic(api_key=self._api_key, timeout=TIMEOUT_SECS)
        return self._client

    def decide(self, snapshot: Dict, now) -> BrainResult:
        """Consult the brain for all coins. Never raises — fail-safe to empty
        decisions (the runner then holds current positions)."""
        t0 = time.time()
        try:
            client = self._get_client()
            resp = client.messages.create(
                model=self.model,
                max_tokens=MAX_TOKENS,
                system=[{"type": "text", "text": SYSTEM_PROMPT,
                         "cache_control": {"type": "ephemeral"}}],
                tools=[DECISION_TOOL],
                tool_choice={"type": "tool", "name": "submit_decisions"},
                messages=[{"role": "user", "content": _build_user_message(snapshot, now)}],
            )
            decisions = _parse(resp)
            res = BrainResult(decisions=decisions, model=self.model,
                              latency_ms=int((time.time() - t0) * 1000))
            usage = getattr(resp, "usage", None)
            if usage is not None:
                res.input_tokens = int(getattr(usage, "input_tokens", 0) or 0)
                res.output_tokens = int(getattr(usage, "output_tokens", 0) or 0)
            return res
        except Exception as e:
            logger.warning("[TRADE_BRAIN] decide failed → hold current: %s", e)
            return BrainResult(decisions={}, model=self.model, error=str(e),
                               latency_ms=int((time.time() - t0) * 1000))

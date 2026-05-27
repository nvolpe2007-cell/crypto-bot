"""
AI brain — the gate-keeper decision layer.

A structural setup (extreme funding + OI spike/flush) is detected by the rule
engine FIRST. Only then is the brain consulted: it reasons over the full signal
picture and returns CONFIRM or VETO plus a size multiplier, conviction, and the
rationale. It is a gate-KEEPER, not a gate-OPENER:

  • it can VETO a gated setup or TRIM size — never invent a sub-threshold trade;
  • size is hard-clamped to [config.AI_MIN_SIZE_MULT, config.MAX_SIZE_BOOST];
  • fail-closed: any API error / timeout / parse failure → VETO (a broken brain
    never trades).

The Anthropic client is constructed lazily so importing this module (and running
the rest of the test suite) never requires the SDK or a key. Tests inject a fake
client via `AIBrain(client=...)`; no network calls in CI.
"""

import json
import logging
import time
from dataclasses import dataclass
from typing import Dict, Optional, Any

from . import config

logger = logging.getLogger(__name__)

# The strategy knowledge. Static → marked cache_control so repeat calls (multiple
# coins inside a froth burst) hit the prompt cache and cost ~nothing.
SYSTEM_PROMPT = """\
You are the decision core of a paper-trading bot that fades over-leveraged retail \
crowds on mid-cap alt perpetuals (SOLUSDT, AVAXUSDT, ARBUSDT) on Bybit. You take \
exactly two trades:

  FADE SHORT (primary): retail has piled into leveraged longs — funding elevated, \
  open interest spiking, the move driven by perp buying not spot demand. You short \
  the perp; the crowd gets squeezed out and price drops.

  FLUSH LONG (secondary): a forced-liquidation cascade just wiped longs — price \
  dropped fast, OI collapsed, funding flipped, volume spiked then dried up. You buy \
  the exhaustion; forced selling overshoots and snaps back.

CRITICAL ROLE CONSTRAINT — YOU ARE A GATE-KEEPER, NOT A GATE-OPENER:
A hard structural gate (funding extreme + OI spike for shorts; OI flush + funding \
collapse + volume spike for longs) has ALREADY passed before you are called. Your \
job is to judge the QUALITY of that gated setup and either CONFIRM it (with a size) \
or VETO it. You must NOT rationalize a trade on partial/sub-threshold evidence — \
that is the exact failure mode this system was rebuilt to avoid. When the evidence \
is thin, contradictory, or the structure looks like it could extend against you, \
VETO. Costs (~0.3% round-trip) dominate at this size; a marginal trade is a losing \
trade. When in doubt, veto.

HOW TO WEIGH THE SIGNALS (evidence, not checkboxes):
- Funding dynamics: a high but STILL-RISING funding rate means the crowd is still \
  growing — the squeeze can extend; prefer to wait. A high funding rate whose \
  velocity just flipped DOWN means the fuel is running out — the prime fade moment.
- OI: spiking OI + high funding confirms fresh leveraged longs at the top. OI \
  flushing confirms a cascade for the flush-long.
- CVD divergence: perp net-buying while spot is flat/selling = leverage-only move \
  with no real demand → fragile → strengthens a short.
- Taker cross-window divergence: aggressive buying NOW into a broader selling tape \
  = distribution (smart money selling into FOMO) → strengthens a short.
- Basis compression: a positive perp premium shrinking while price holds = stealth \
  long-unwind before it shows in price → strengthens a short.
- Liquidity clusters: a large resting cluster below price is a magnet that feeds \
  the cascade you are fading.
- Trend / regime / BTC: NEVER fade into a confirmed uptrend (funding can stay \
  extreme for weeks while price rips — the #1 way this strategy loses). Don't buy \
  flushes while BTC is dumping.
- Funding-reset timing: the 45–90 min before a Bybit reset (00/08/16 UTC) is the \
  best short-entry window when longs are paying to hold.

SIZING (within the structural cap):
- 1.0x: gated, little extra confirmation.
- up to ~1.5x: gate + strong, agreeing confirmations (CVD + taker + basis + a real \
  cluster + good timing). This fade is negatively skewed — being biggest right \
  before a squeeze is the wrong moment, so never exceed the cap.
- 0.5x: gated but you see a real reason for caution and want skin-in but small.

Return your decision by calling submit_trade_decision. Be concrete and specific in \
the reasoning — name the signal that decided it and what would invalidate the trade.
"""

DECISION_TOOL = {
    "name": "submit_trade_decision",
    "description": "Submit the trade decision for the gated setup.",
    "input_schema": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["confirm", "veto"],
                "description": "confirm = take the gated trade; veto = skip it.",
            },
            "confidence": {
                "type": "integer",
                "minimum": 1,
                "maximum": 10,
                "description": "Conviction 1-10. The runner ignores confirms below its floor.",
            },
            "size_multiplier": {
                "type": "number",
                "minimum": 0.5,
                "maximum": 2.0,
                "description": "Size vs base risk. Clamped to the structural cap by the runner.",
            },
            "key_signal": {
                "type": "string",
                "description": "The single signal that most drove this decision.",
            },
            "invalidation": {
                "type": "string",
                "description": "What would immediately prove this trade wrong.",
            },
            "urgency": {
                "type": "string",
                "enum": ["enter_now", "wait_for_dip", "wait_for_confirmation", "watch"],
            },
            "reasoning": {
                "type": "string",
                "description": "Concise structural read — why confirm or veto.",
            },
        },
        "required": ["action", "confidence", "size_multiplier", "key_signal",
                     "invalidation", "urgency", "reasoning"],
    },
}


@dataclass
class AIDecision:
    action: str = "veto"
    confidence: int = 0
    size_multiplier: float = 1.0
    key_signal: str = ""
    invalidation: str = ""
    urgency: str = "watch"
    reasoning: str = ""
    model: str = ""
    latency_ms: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    error: Optional[str] = None

    @property
    def confirmed(self) -> bool:
        return self.action == "confirm" and self.error is None


def _build_user_message(coin: str, setup, signals: Dict, now) -> str:
    payload = {
        "coin": coin,
        "utc_time": now.isoformat() if hasattr(now, "isoformat") else str(now),
        "gated_setup": {
            "type": setup.setup_type,
            "direction": setup.direction,
            "structural_size_multiplier": setup.size_multiplier,
        },
        "signals": signals,
    }
    return (
        "A structural setup just passed the hard gate. Judge its quality and call "
        "submit_trade_decision (confirm or veto). Remember: do not confirm marginal "
        "structure — costs dominate.\n\n```json\n"
        + json.dumps(payload, indent=2, default=str)
        + "\n```"
    )


def _parse(resp) -> AIDecision:
    for block in getattr(resp, "content", []) or []:
        if getattr(block, "type", None) == "tool_use" and getattr(block, "name", None) == "submit_trade_decision":
            inp = block.input or {}
            return AIDecision(
                action=str(inp.get("action", "veto")).lower(),
                confidence=int(inp.get("confidence", 0) or 0),
                size_multiplier=float(inp.get("size_multiplier", 1.0) or 1.0),
                key_signal=str(inp.get("key_signal", "")),
                invalidation=str(inp.get("invalidation", "")),
                urgency=str(inp.get("urgency", "watch")),
                reasoning=str(inp.get("reasoning", "")),
            )
    raise ValueError("no submit_trade_decision tool_use block in response")


class AIBrain:
    """Wraps the Anthropic client. `client` may be injected for tests."""

    def __init__(self, api_key: Optional[str] = None, model: Optional[str] = None,
                 client: Any = None):
        self.model = model or config.AI_MODEL
        self._client = client
        self._api_key = api_key or config.ANTHROPIC_API_KEY

    def _get_client(self):
        if self._client is None:
            import anthropic  # lazy: importing this module never requires the SDK
            if not self._api_key:
                raise RuntimeError("ANTHROPIC_API_KEY not set")
            self._client = anthropic.Anthropic(api_key=self._api_key,
                                                timeout=config.AI_TIMEOUT_SECS)
        return self._client

    def decide(self, coin: str, setup, signals: Dict, now) -> AIDecision:
        """Consult the brain for one gated setup. Always returns an AIDecision;
        never raises (fail-closed → veto on any error)."""
        t0 = time.time()
        try:
            client = self._get_client()
            resp = client.messages.create(
                model=self.model,
                max_tokens=config.AI_MAX_TOKENS,
                system=[{"type": "text", "text": SYSTEM_PROMPT,
                         "cache_control": {"type": "ephemeral"}}],
                tools=[DECISION_TOOL],
                tool_choice={"type": "tool", "name": "submit_trade_decision"},
                messages=[{"role": "user",
                           "content": _build_user_message(coin, setup, signals, now)}],
            )
            d = _parse(resp)
            d.model = self.model
            d.latency_ms = int((time.time() - t0) * 1000)
            usage = getattr(resp, "usage", None)
            if usage is not None:
                d.input_tokens = int(getattr(usage, "input_tokens", 0) or 0)
                d.output_tokens = int(getattr(usage, "output_tokens", 0) or 0)
            # Gate-keeper guarantee, enforced in code regardless of model output.
            d.size_multiplier = max(config.AI_MIN_SIZE_MULT,
                                    min(config.MAX_SIZE_BOOST, d.size_multiplier))
            return d
        except Exception as e:
            logger.warning("[AI_BRAIN] %s decide failed → fail-closed VETO: %s", coin, e)
            return AIDecision(action="veto", confidence=0,
                              reasoning=f"AI error (fail-closed): {e}",
                              key_signal="error", model=self.model,
                              latency_ms=int((time.time() - t0) * 1000), error=str(e))

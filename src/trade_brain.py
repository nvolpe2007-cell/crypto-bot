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

READING THE EXTRA CONTEXT. Each coin now also carries the desk signals the rest of \
the system computes, and a portfolio-wide `market` block. Use them to REFINE the \
trend read and sizing — they do NOT override the cost/overtrading discipline above. \
If `market.stale` is true, IGNORE this context and decide on price/trend alone.
  • regime + adx (the bot's own classifier): ADX>25 = a real trend, align with it; \
    ADX<20 = chop, directional bets get whipsawed → prefer FLAT. In RANGING regime, \
    do NOT chase breakouts; in TRENDING_DOWN, long is fighting the tape (short or flat).
  • rsi: in a RANGE, stretched RSI (>70/<30) mean-reverts; in a STRONG trend RSI can \
    stay pinned — do not fade a strong trend on RSI alone.
  • iv_percentile / iv_term: high IV percentile = options pricing in big moves → size \
    DOWN and demand a cleaner setup; BACKWARDATION (near>far) signals stress/fear → caution.
  • funding (perp APY): a POSITIONING tell, not a price forecast. Large positive funding \
    = crowded longs paying to hold (headwind for fresh longs, carry tailwind for shorts); \
    large negative = crowded shorts. Weigh lightly, never as a sole trigger.
  • market.fear_greed: Extreme Fear (<25) marks capitulation zones — contrarian LONG \
    bias for survivors, BUT in a confirmed downtrend fear persists; do not catch a falling \
    knife. Extreme Greed (>75) = froth, caution on new longs. A tiebreaker, not a trigger.
  • market.btc_dominance rising = capital rotating OUT of alts into BTC (risk-off for \
    alts) → demand cleaner ETH/SOL setups or prefer BTC. altcoin_pressure flags this too.
The context should mostly REINFORCE or VETO a trend call, and tune size — it is not a \
new set of triggers to trade more. When signals conflict, that is itself a reason for FLAT.

HOW TO SIZE — BE AGGRESSIVE WHEN YOU ARE RIGHT, FLAT WHEN YOU ARE NOT. Sizing is \
BIMODAL, not a dial of small bets. size_mult scales a fixed base allocation and is \
clamped 0.0-2.5 by the runner. Tie it to conviction:
  • conviction 9-10, clean multi-signal trend (price/MA/momentum all aligned, BTC \
    confirming): 2.0-2.5x — bet big, this is the whole point.
  • conviction 7-8, solid trend, minor caveats: 1.2-1.8x.
  • conviction 5-6, real but mixed: 0.5-0.9x — small, or prefer FLAT.
  • conviction <=4 or signals disagree: action "flat" (size ignored). DO NOT take \
    small low-conviction positions — that is "throwing money away": you pay full \
    round-trip cost + funding for an edge that isn't there. Flat is a position.
The aggression rule: concentrate size on your highest-conviction coin; don't spread \
thin equal bets across all three out of habit. Being big on a clean trend and flat on \
the rest beats being medium on everything.

READING THE CHART IMAGES. You may also be shown a daily candlestick chart per coin \
(last ~140 days) with SMA50 (blue), SMA100 (orange) and SMA200 (purple) overlays. \
Use it to read STRUCTURE the numbers alone don't show: the shape of the trend, \
consolidation/range vs expansion, support/resistance, higher-highs/higher-lows (up) \
or lower-highs/lower-lows (down), and where price sits relative to the MA ribbon. \
HONESTY ABOUT CHARTS: classic chart patterns are WEAK, contested predictors — the \
chart is for CONFIRMING or VETOING the trend read and judging conviction, NOT a new \
set of triggers to trade more. The JSON values are authoritative for exact levels; \
the image is for context. When the chart and the numbers disagree, trust the numbers \
and lean toward FLAT. Never trade on a pattern alone.

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
                        "size_mult": {"type": "number", "minimum": 0.0, "maximum": 2.5},
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


# ── Portfolio overseer ───────────────────────────────────────────────────────
# A SECOND, separate role for the same brain: read EVERY arm's live book and
# produce a portfolio-level RISK REVIEW. This is OBSERVABILITY ONLY — its output
# is never executed and changes no positions. Its job is judgment about the whole
# book: concentration (many arms betting the same direction on the same coin),
# arms fighting the tape, drawdown, and cost/overtrading discipline.
OVERSEER_SYSTEM_PROMPT = """\
You are the RISK OVERSEER for a multi-arm crypto PAPER trading system. Several \
independent arms each run their own book: some are mechanical trend rules, some are \
delta-neutral funding-carry arms, one is a discretionary "brain". You are shown every \
arm's current equity, open positions, and recent P&L.

YOUR OUTPUT IS ADVISORY ONLY. Nothing you say is executed; you change no positions. \
You are a second pair of eyes that flags risk a single-arm view misses. Be concise, \
specific, and honest — do NOT invent an edge or cheerlead.

What to look for:
  • CONCENTRATION / hidden correlation: if several arms are all LONG (or all SHORT) \
    the same coin, the "diversified" book is really one big directional bet. Say so.
  • FIGHTING THE TAPE: an arm short into a sustained bounce (or long into a drop), \
    especially with a widening drawdown, is bleeding. Name it.
  • DRAWDOWN: arms near or past their loss caps; books down materially on MTM.
  • COST / OVERTRADING: arms churning for tiny edges (cost ~0.15-0.5% round-trip \
    dominates at this size). Idle is usually fine; churn is the enemy.
  • The honest baseline: this system has NO proven directional edge; trend's only \
    proven value is downside protection. Flat is a legitimate position. If the whole \
    book is quiet and that's appropriate, say that plainly rather than manufacturing \
    concern.

Call submit_review exactly once. Keep flags to what genuinely matters (0 is fine)."""

REVIEW_TOOL = {
    "name": "submit_review",
    "description": "Submit one portfolio-level risk review.",
    "input_schema": {
        "type": "object",
        "properties": {
            "overall_risk": {"type": "string", "enum": ["low", "medium", "high"]},
            "portfolio_note": {"type": "string",
                               "description": "1-3 sentence read of the whole book."},
            "flags": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "arm": {"type": "string"},
                        "severity": {"type": "string",
                                     "enum": ["info", "warn", "critical"]},
                        "note": {"type": "string"},
                    },
                    "required": ["arm", "severity", "note"],
                },
            },
            "suggestions": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Non-binding observations. NOT executed.",
            },
        },
        "required": ["overall_risk", "portfolio_note", "flags"],
    },
}


@dataclass
class ReviewResult:
    overall_risk: str = "low"
    portfolio_note: str = ""
    flags: List[Dict[str, str]] = field(default_factory=list)
    suggestions: List[str] = field(default_factory=list)
    model: str = ""
    latency_ms: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    error: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.error is None and bool(self.portfolio_note)


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


def _build_user_message(snapshot: Dict, now, macro: Optional[Dict] = None) -> str:
    payload = {
        "utc_time": now.isoformat() if hasattr(now, "isoformat") else str(now),
        "note": "Decide long/short/flat per coin. Holding yesterday's position is "
                "free of cost; changing it is not. Do nothing unless there's a reason.",
        "market": macro or {},
        "coins": snapshot,
    }
    return ("Here is today's market picture. Call submit_decisions with one entry "
            "per coin.\n\n```json\n" + json.dumps(payload, indent=2, default=str) + "\n```")


def _build_user_content(snapshot: Dict, now, macro: Optional[Dict] = None,
                        charts: Optional[Dict[str, str]] = None):
    """User-message content. Text-only (a str) when no charts are given — identical
    to before. When `charts` (coin -> base64 PNG) is provided, returns a multimodal
    content list (text + labeled image blocks) so a vision model reads the charts."""
    text = _build_user_message(snapshot, now, macro)
    if not charts:
        return text
    content: List[Dict[str, Any]] = [{"type": "text", "text": text}]
    for coin, b64 in charts.items():
        if not b64:
            continue
        content.append({"type": "text",
                        "text": f"Daily candlestick chart for {coin} "
                                f"(last ~140d; SMA50=blue, SMA100=orange, SMA200=purple):"})
        content.append({"type": "image",
                        "source": {"type": "base64", "media_type": "image/png", "data": b64}})
    return content


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
                    size_mult=max(0.0, min(2.5, float(d.get("size_mult", 1.0) or 1.0))),
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

    def decide(self, snapshot: Dict, now, macro: Optional[Dict] = None,
               charts: Optional[Dict[str, str]] = None) -> BrainResult:
        """Consult the brain for all coins. Never raises — fail-safe to empty
        decisions (the runner then holds current positions). `macro` carries
        portfolio-wide context (sentiment, dominance, staleness); `charts` (coin ->
        base64 PNG) attaches candlestick images for the vision-capable model to read."""
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
                messages=[{"role": "user",
                           "content": _build_user_content(snapshot, now, macro, charts)}],
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

    def review(self, portfolio: Dict, now) -> "ReviewResult":
        """Portfolio-level RISK REVIEW across all arms. Advisory only — never
        executed. Fail-safe: any error → empty review (the runner just skips)."""
        t0 = time.time()
        try:
            client = self._get_client()
            payload = {
                "utc_time": now.isoformat() if hasattr(now, "isoformat") else str(now),
                "note": "Review the whole book. Advisory only — you change no positions.",
                "arms": portfolio,
            }
            user = ("Here is every arm's current book. Call submit_review once.\n\n"
                    "```json\n" + json.dumps(payload, indent=2, default=str) + "\n```")
            resp = client.messages.create(
                model=self.model,
                max_tokens=MAX_TOKENS,
                system=[{"type": "text", "text": OVERSEER_SYSTEM_PROMPT,
                         "cache_control": {"type": "ephemeral"}}],
                tools=[REVIEW_TOOL],
                tool_choice={"type": "tool", "name": "submit_review"},
                messages=[{"role": "user", "content": user}],
            )
            data = {}
            for block in getattr(resp, "content", []) or []:
                if (getattr(block, "type", None) == "tool_use"
                        and getattr(block, "name", None) == "submit_review"):
                    data = block.input or {}
                    break
            if not data:
                raise ValueError("no submit_review tool_use block in response")
            res = ReviewResult(
                overall_risk=str(data.get("overall_risk", "low")).lower(),
                portfolio_note=str(data.get("portfolio_note", "")),
                flags=[{"arm": str(f.get("arm", "")),
                        "severity": str(f.get("severity", "info")).lower(),
                        "note": str(f.get("note", ""))}
                       for f in (data.get("flags") or []) if isinstance(f, dict)],
                suggestions=[str(s) for s in (data.get("suggestions") or [])],
                model=self.model, latency_ms=int((time.time() - t0) * 1000))
            usage = getattr(resp, "usage", None)
            if usage is not None:
                res.input_tokens = int(getattr(usage, "input_tokens", 0) or 0)
                res.output_tokens = int(getattr(usage, "output_tokens", 0) or 0)
            return res
        except Exception as e:
            logger.warning("[TRADE_BRAIN] review failed → skip: %s", e)
            return ReviewResult(model=self.model, error=str(e),
                                latency_ms=int((time.time() - t0) * 1000))

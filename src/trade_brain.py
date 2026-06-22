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

MODEL = os.getenv("BRAIN_MODEL", "claude-opus-4-8")     # upgraded engine (was sonnet-4-6)
MAX_TOKENS = int(os.getenv("BRAIN_MAX_TOKENS", "2000"))
TIMEOUT_SECS = float(os.getenv("BRAIN_TIMEOUT_SECS", "90"))   # extended thinking needs headroom
# Adaptive thinking: let Opus 4.8 reason before deciding (the modern API — there is
# no fixed token budget; `output_config.effort` controls depth). Forcing a specific
# tool is incompatible with thinking, so tool_choice falls back to "auto" and the
# prompt instructs the brain to finish by calling submit_decisions. BRAIN_THINKING=0
# restores the forced-tool path (e.g. for an older non-thinking model).
THINKING = os.getenv("BRAIN_THINKING", "1") == "1"
EFFORT = os.getenv("BRAIN_EFFORT", "high")              # low | medium | high | xhigh | max

# Static strategy knowledge → cache_control so the daily call hits the prompt cache.
SYSTEM_PROMPT = """\
You are the decision core of a crypto PAPER trading account. Each day you decide, \
for EACH coin you are shown independently (you will be given the exact list — it may \
be just the majors or a wider basket of liquid alts), whether to hold a LONG, a SHORT, \
or stay FLAT (in cash) over the coming days — and at what size. You trade 1x notional \
perps (no leverage); you CAN profit in downtrends by shorting.

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
  • BTC LEADS; if BTC is clearly trending, ETH/SOL and the alts usually follow. The \
    smaller/lower-cap the alt, the more volatile and less reliable it is — demand a \
    cleaner setup there. CRUCIALLY, all these coins co-move with BTC, so a book stacked \
    LONG (or SHORT) across many of them is NOT diversified — it is one big BTC-beta bet \
    at combined size. Spread across more names only helps when the setups genuinely \
    differ; otherwise concentrate on the cleanest one. The risk_budget block tracks this.

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

DESK BLOCKS (composable context in `market.desk_blocks`, any subset may be present):
  • cross_asset: the macro risk backdrop (S&P, dollar/dxy, gold, 10Y yield, 20d moves \
    + a regime: risk_on/risk_off/mixed). Crypto tracks RISK-ON; a strong/rising dollar \
    and rising yields are headwinds. In risk_off, demand a cleaner long and lean FLAT/short.
  • flow (per coin): SLOW directional volume — buy_pressure_20d/5d in [-1,1] (net up-day \
    minus down-day volume) and vs_price. This is NOT a tick scalp signal; use it to \
    confirm or fade. 'bearish_divergence' (price up on net selling) = a weak rally, be \
    wary of fresh longs; 'bullish_divergence' (price down on net buying) = quiet \
    accumulation, a downtrend may be tiring. Weigh lightly; never trade on it alone.
  • risk_budget: YOUR OWN book — net/gross exposure and directional_concentration. \
    BTC/ETH/SOL co-move, so an all_long or all_short book is really ONE bet at gross \
    size. If you are already concentrated, do NOT pile more onto the same correlated \
    direction; prefer trimming or diversifying the read. Respect this before sizing up.
  • proof_status: the WHOLE system's honest scorecard — which strategy arms have actually \
    cleared the strict proof bar (executable & n>=30 & expectancy>0 & family-wise t) and which \
    have not. If `proven` is "none", NO directional edge is established here — carry humility, \
    prefer FLAT/small, and do not act as if any setup is a sure thing. This is epistemic \
    grounding, not a trade trigger.
  • session_edge: the bot's own realised win-rate/expectancy by UTC session (Asia/EU/US), with \
    a FAVORABLE/NEUTRAL/UNFAVORABLE verdict. A weak timing tiebreaker — never a trigger.
  • swing_attribution: per-symbol net P&L / win-rate from the live 4h-majors swing forward test \
    — which names have actually worked. Use to lean toward proven names, not to chase.
These blocks REFINE and RISK-CHECK the decision; they are not new reasons to trade.

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

READING THE CHART IMAGES. You may be shown TWO candlestick charts per coin: a WEEKLY \
chart (~2 years, SMA13/26/52-week — the DOMINANT, higher-timeframe trend) and a DAILY \
chart (~140 days, SMA50/100/200-day — recent action and entry timing). Read them \
TOGETHER the way a discretionary trader does: the WEEKLY sets the regime/bias (only \
fight it with strong evidence), the DAILY times the entry within that bias. ALIGNMENT \
is the high-conviction setup — weekly uptrend + daily pullback-then-resumption → LONG; \
weekly downtrend + daily bounce-into-resistance → SHORT or FLAT. CONFLICT between the \
two timeframes (weekly up, daily breaking down, or vice-versa) is itself a reason for \
FLAT or smaller size. Read STRUCTURE the numbers don't show: trend shape, \
consolidation vs expansion, support/resistance, higher-highs/lows vs lower-highs/lows, \
position relative to the MA ribbon. HONESTY ABOUT CHARTS: classic chart patterns are \
WEAK, contested predictors — the charts CONFIRM or VETO the trend read and tune \
conviction, they are NOT new triggers to trade more. The JSON values are authoritative \
for exact levels; images are for context. When charts and numbers disagree, trust the \
numbers and lean FLAT. Never trade on a pattern alone.

YOUR TRACK RECORD & MEMORY. The payload may include a `memory` block: your own recent
decisions, your CLOSED trades (entry rationale + how each turned out), your equity and
drawdown, and a conviction-calibration table (did your high-conviction calls actually
win?). USE IT like a professional reviewing their journal:
  • LEARN from outcomes, not vibes. If a setup you keep taking keeps losing (e.g. shorting
    a bounce that then squeezes), STOP taking it — say which past trade taught you this.
  • WEIGHT BY SAMPLE SIZE, NOT RECENCY. A handful of recent losses is noise; do not
    abandon a sound process over 2-3 trades, and do not get cocky after 2-3 wins. Only
    update your behaviour when the record is big enough to mean something.
  • CALIBRATE CONVICTION. If your "conviction 8" calls have only won ~50%, your scale is
    inflated — pull conviction (and size) down until the record earns it back. The memory now
    also breaks your record down by COIN and by ACTION (long/short) and lists your WORST trades
    with the thesis that lost — if a coin or a side keeps losing, name that record and stop.
  • RESPECT DRAWDOWN. If the book is underwater, get smaller and more selective, not
    bigger trying to win it back. Revenge-sizing is how accounts die.
  • If memory is sparse (few/no closed trades), say so and lean on the principles above —
    do not invent lessons from an empty record.

For EVERY coin, call submit_decisions exactly once with one entry per coin. Be \
concrete: name the signal that decided it and what would flip your view. Prefer \
KEEPING the current position when the picture hasn't materially changed — say so. \
You MUST finish your turn by calling the submit_decisions tool — reasoning in text \
without calling it is an incomplete answer."""

def _system_blocks() -> List[Dict[str, Any]]:
    """System content for decide(): the static strategy prompt, plus the durable
    curated knowledge base as a SECOND cached block (BRAIN_KNOWLEDGE=1, default on).
    Both carry cache_control so the daily call hits the prompt cache. Fail-safe — if
    the knowledge module can't be imported, the brain still runs on the base prompt."""
    blocks: List[Dict[str, Any]] = [
        {"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}
    ]
    try:
        from .brain_knowledge import brain_knowledge
        kb = brain_knowledge()
    except Exception:
        kb = ""
    if kb and kb.strip():
        blocks.append({"type": "text", "text": kb,
                       "cache_control": {"type": "ephemeral"}})
    return blocks


def build_decision_tool(coins: Optional[List[str]] = None) -> Dict[str, Any]:
    """The submit_decisions tool. The coin enum is set to the active universe so the
    model can only emit coins we actually trade; coins=None/empty omits the enum (any
    string accepted — back-compat for tests / unknown universes). The runner only acts
    on coins it has a fresh bar for, so a stray coin is harmless either way."""
    coin_prop: Dict[str, Any] = {"type": "string"}
    coins = sorted({c for c in (coins or []) if c})
    if coins:
        coin_prop["enum"] = coins
    return {
        "name": "submit_decisions",
        "description": "Submit one trade decision per coin you are shown.",
        "input_schema": {
            "type": "object",
            "properties": {
                "decisions": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "coin": coin_prop,
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


# Module-level default (BTC/ETH/SOL) kept for back-compat; decide() builds a tool
# scoped to the actual snapshot universe per call.
DECISION_TOOL = build_decision_tool(["BTC", "ETH", "SOL"])


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


def _build_user_message(snapshot: Dict, now, macro: Optional[Dict] = None,
                        memory: Optional[Dict] = None) -> str:
    payload = {
        "utc_time": now.isoformat() if hasattr(now, "isoformat") else str(now),
        "note": "Decide long/short/flat per coin. Holding yesterday's position is "
                "free of cost; changing it is not. Do nothing unless there's a reason.",
        "memory": memory or {},
        "market": macro or {},
        "coins": snapshot,
    }
    return ("Here is today's market picture (and your `memory` / track record). Call "
            "submit_decisions with one entry per coin.\n\n```json\n"
            + json.dumps(payload, indent=2, default=str) + "\n```")


def _coin_charts(coin: str, val) -> List[tuple]:
    """Normalise one coin's chart payload to a list of (label, base64). Accepts a
    bare base64 str (single daily chart — back-compat) or a list of (label, b64)
    tuples (multi-timeframe). Drops empties."""
    if not val:
        return []
    if isinstance(val, str):
        return [(f"Daily candlestick chart for {coin} "
                 f"(last ~140d; SMA50=blue, SMA100=orange, SMA200=purple):", val)]
    out = []
    for item in val:
        if isinstance(item, (list, tuple)) and len(item) == 2 and item[1]:
            out.append((str(item[0]), item[1]))
    return out


def _build_user_content(snapshot: Dict, now, macro: Optional[Dict] = None,
                        charts: Optional[Dict] = None, memory: Optional[Dict] = None):
    """User-message content. Text-only (a str) when no charts are given — identical
    to before. When `charts` (coin -> base64 str OR list of (label, base64)) is given,
    returns a multimodal content list (text + labeled image blocks) so a vision model
    reads the charts. Multiple timeframes per coin are supported via the list form.
    `memory` (track record / past decisions) is folded into the text payload."""
    text = _build_user_message(snapshot, now, macro, memory)
    if not charts:
        return text
    content: List[Dict[str, Any]] = [{"type": "text", "text": text}]
    for coin, val in charts.items():
        for label, b64 in _coin_charts(coin, val):
            content.append({"type": "text", "text": label})
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
               charts: Optional[Dict[str, str]] = None,
               memory: Optional[Dict] = None) -> BrainResult:
        """Consult the brain for all coins. Never raises — fail-safe to empty
        decisions (the runner then holds current positions). `macro` carries
        portfolio-wide context; `charts` (coin -> base64 PNG, or list of (label,b64))
        attaches candlestick images for the vision model; `memory` carries the brain's
        own track record (past decisions, closed-trade outcomes, equity/drawdown,
        conviction calibration) so it can learn from its results."""
        t0 = time.time()
        try:
            client = self._get_client()
            kwargs = dict(
                model=self.model,
                max_tokens=MAX_TOKENS,
                system=_system_blocks(),
                tools=[build_decision_tool(list(snapshot.keys()) if snapshot else None)],
                messages=[{"role": "user",
                           "content": _build_user_content(snapshot, now, macro, charts, memory)}],
            )
            if THINKING:
                # Adaptive thinking forbids forcing a specific tool → use auto and let
                # the prompt drive the tool call; give max_tokens room for think+answer.
                kwargs["thinking"] = {"type": "adaptive"}
                kwargs["output_config"] = {"effort": EFFORT}
                kwargs["tool_choice"] = {"type": "auto"}
                kwargs["max_tokens"] = max(MAX_TOKENS, 8000)
            else:
                kwargs["tool_choice"] = {"type": "tool", "name": "submit_decisions"}
            resp = client.messages.create(**kwargs)
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

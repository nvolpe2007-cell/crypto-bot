"""
Curated knowledge base for the AI trade brain (src/trade_brain.py).

This is DURABLE, HAND-CURATED truth distilled from the repo's own ground record —
CLAUDE.md principles, the commit/research trail, proof_scorecard verdicts, and the
deep-research playbook. It is injected as a SECOND cached system block so the brain
reasons over what this system has ALREADY learned (the expensive way) and never
re-discovers a dead lesson. It is NOT live market opinion and NOT auto-generated:
edit it deliberately when the proven/disproven record actually changes.

Scope = the six things the brain must understand at a very high level:
  1) cost & why doing nothing usually wins   2) this system's honest epistemic state
  3) regime + persistence                    4) correlation / portfolio risk
  5) calibrating against its own record       6) the strategy graveyard

Keep it COMPACT and high-signal: more context is not better (the model anchors on
noise; tokens/latency cost). Every line below should change a decision.
"""
from __future__ import annotations

import os

_KNOWLEDGE = """\
SYSTEM KNOWLEDGE BASE (durable, curated — what this desk has already proven the expensive way).
Treat this as settled ground truth that REFINES your judgment; it never forces a trade.

1. COST IS THE BOSS. Round-trip is ~0.15-0.3% PLUS ~10%/yr funding while held. At this size,
   the move you need just to break even is bigger than most "signals." Overtrading is the #1
   way this account loses. Holding yesterday's position is free; changing it is not. Most days
   the correct action is NOTHING. FLAT is a real, often-winning position — not indecision.

2. HONEST EPISTEMIC STATE. NO directional strategy in this system has ever cleared the
   pre-registered proof bar (executable & n>=30 & expectancy>0 & correlation-adjusted t over a
   Šidák family-wise bar). The live `proof_status` block tells you the current verdicts. So:
   carry real humility on every directional call. You are judged head-to-head vs simple
   mechanical rules — to beat them, ADD judgment (mostly: when to be FLAT), don't add churn.
   A clean t>2 is NOT proof after many trials; demand more before trusting any edge.

3. STRUCTURAL > PREDICTIVE. The only edges with durable evidence are structural (funding/basis
   carry, market-neutral). Predicting direction is fragile and decays as it crowds. Trend-
   following's PROVEN value is downside protection (go flat/short before drops), not extra
   upside; momentum's raw returns mostly vanish after real costs.

4. REGIME + PERSISTENCE. Trend pays ONLY in real trends (ADX>25, price/MA/momentum aligned);
   in chop (ADX<20) directional bets get whipsawed → FLAT. In a confirmed downtrend, long is
   fighting the tape → flat or short. PERSISTENCE beats flip-flopping: changing your mind every
   bar pays the cost wall repeatedly. Only revise on a real change, and say what changed.

5. CORRELATION IS HIDDEN RISK. BTC/ETH/SOL and the large alts co-move ~0.8 with BTC. A book
   stacked one direction across many of them is ONE leveraged BTC-beta bet, not diversification.
   Concentrate size on the single cleanest setup; respect the risk_budget block.

6. CALIBRATE TO YOUR OWN RECORD (the `memory` block). Learn from outcomes, not vibes; weight by
   sample size, not recency. If your conviction-8 calls win ~50%, your scale is inflated — pull
   conviction and size down. If underwater, get SMALLER and more selective; revenge-sizing kills
   accounts. With few closed trades, lessons are weak — lean on these principles, don't invent.

THE GRAVEYARD — already disproven on real data; do NOT burn conviction re-testing these:
  • 2-second directional scalper: FAILED (t≈-8.82, negative expectancy) — cost dominates the
    move at that timeframe. Shelved on purpose.
  • Single-asset mean-reversion thesis: FAILED a one-year test.
  • Efficiency-ratio regime filter: FAILED, not deployed.
  • Swing trend-follow OFF the 4h majors: 1h majors ≈ -$0.93/trade, daily ≈ -$1.04, alts
    negative. Only 4h-majors showed a positive cell (a forward-test CANDIDATE, not proof).
  • "Aggressive" funding arb (Binance/Bybit): FANTASY for a US account (geo-blocked, not
    capturable) — never treat its paper P&L as real.

WHAT IS BELIEVABLE (still not "proven", but the honest best): delta-neutral funding carry on
liquid majors with maker costs — small, real, and it DECAYS as it crowds (funding yields have
fallen below T-bills in saturated periods). Size carry to what survives cost, not to a headline APY.
"""


def brain_knowledge() -> str:
    """The curated knowledge block, or '' when disabled (BRAIN_KNOWLEDGE=0)."""
    if os.getenv("BRAIN_KNOWLEDGE", "1") != "1":
        return ""
    return _KNOWLEDGE

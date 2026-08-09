# How the bot actually thinks — full mechanics (2026-07-21)

This is the detailed reference version of "what happens when the bot sees an
entry." One section per arm (exact thresholds/formulas), then the shared
main-loop pipeline (directional engine — currently shelved by default, but
its exits and every other arm run regardless).

## 1. lev_perp (v1) — leveraged perp, daily bars, BTC/ETH/SOL

**Signal**: pure trend sign. `side = +1 if close >= SMA(50) else -1`. No
oscillator drives direction — RSI is used only as a filter (see below).

**Entry filters** (all must pass):
- RSI(14) < 45 — refuses to chase an already-extended move, both directions.
- Trend age ≥ 8 consecutive days on the same side of the SMA.
- Volume ≥ 1.2× the trailing 20-day average.
- ADX NOT in [20,30] — that band is empirically "fake trend," skipped.
- Master kill switch and the external news-halt file both block new entries
  (not exits).

**Sizing**: $1,000 book / 3 symbols = $333 margin each. Effective leverage =
`min(3x, 2.0%-daily-vol-target / realized 20d vol)` — shrinks automatically
in choppy markets. If two symbols are already long, a third long gets
margin/3 (correlation cap).

**Exit** (checked every bar, worst-outcome-first: liquidation > stop >
take-profit): TP at ±5% price move, stop at ±5%, liquidation at
`(1-5% maintenance)/leverage` (~32% at 3x). Separately, if price crosses back
through the SMA, the position is force-closed regardless of TP/SL state
("trend flip") — then the filter re-runs to possibly re-enter the other way
same-bar. Costs: 0.15% round-trip + 10%/yr funding drag on notional, charged
on every close.

## 2. lev_perp_v2 — same entries, different exit

Literally imports v1's entry logic unchanged. The only difference: instead of
a fixed 5%/5% TP/SL, it trails an ATR(14)×2 "chandelier" stop that only ever
tightens in the trade's favor, using the *prior* bar's trail (so a bar can't
arm its own stop). This is the whole reason it forward-tests differently —
same signal, letting winners run instead of capping them.

## 3. regime_arm — hourly trend-follow with a cost gate

Classification runs on EMA(50)/EMA(200)/ATR% at 1h bars:
1. **Cost gate first** — if hourly ATR% < 1.5× round-trip cost (0.75%), the
   regime is forced FLAT regardless of trend, labeled `"move<cost"`. This is
   the arm literally refusing to trade because the bar can't pay its own fee.
2. Otherwise: TRENDING_UP if `EMA50>EMA200 AND close>EMA50 AND EMA50 rising`;
   TRENDING_DOWN mirror; else flat.
3. A hysteresis band (0.3% around EMA50) means it won't snap straight from
   long to short — it exits toward flat first unless the new side clears the
   band, suppressing whipsaw.

Flat $500/3 sizing per symbol, no leverage. Flat 0.5% round-trip cost, no
funding model.

## 4. pairs (market-neutral) — BTC/ETH/SOL, all 3 pairs

Spread = `ln(price_a) - ln(price_b)`, hourly. Z-score = (today's spread −
7-day mean) / 7-day std.

- Enter when `|z| ≥ 2.0`: short the rich leg, long the cheap leg, $150 each
  (dollar-neutral).
- Exit on convergence `|z| ≤ 0.5`, or stop-out if it keeps diverging
  `|z| ≥ 3.5`, or a 7-day time stop.
- Cost: 0.15%×2 legs + funding drag on the short leg only (borrowing to
  short).
- Portfolio kill: if mark-to-market equity drops $150 below start, everything
  flattens and new entries halt until manually re-armed.

## 5. swing (4h/daily majors) — cadence wrapper around a locked signal

The actual entry logic lives in `src/swing_strategy.py` and is untouched here
— this file just governs *when* and *how many* of its candidates get taken,
across 16 symbols × 2 timeframes (4h, daily) = 32 independent slots.

Each bar: manage any open exit first (stop/target/trend-break). If flat and
the locked strategy says enter, the candidate still has to survive: an
event-blackout veto (macro releases), a session-of-day tag (soft by default,
hard veto only if explicitly flipped on), and gets its fill adjusted to the
live ticker price if available.

All qualifying candidates across the whole universe are ranked by
"conviction" (20-bar rate-of-change + distance above the moving average) and
committed strongest-first, subject to live caps: max 7 open positions
(~$440 of $500 book), and max 3 new trades in the day window (EU+US) / 3 in
the night window (Asia) — so on a busy bar, only the strongest few setups get
taken and the rest are dropped, not queued.

Fixed $62.50 per trade (1/8 of book), uniform across all 32 slots.

## 6. tsmom (long-only) and tsmom_ls (long/short)

**tsmom (long-only)**: daily SMA(200), with an asymmetric 2% hysteresis band
— once long, stays long until price falls 2% below the SMA; once flat, needs
a clean 2% break above to re-enter. This band is the entire whipsaw
defense. No stop-loss at all — the only way out is the trend itself
breaking. ~6-10 flips/year historically.

**tsmom_ls (long/short)**: faster SMA(50), no hysteresis band by default —
pure sign of close vs SMA. Never sits in cash; every flip closes the old
side and opens the opposite same-bar. 1x notional, no leverage (leverage is
explicitly called "settled ruin" in the code comments). Charges funding drag
on both long and short sides deliberately pessimistically.

## 7. trend_ensemble — BTC only, 2-of-3 majority vote

Three yes/no votes computed daily: close>SMA100, close>SMA200, close higher
than 90 days ago. Needs 2 of 3 to be long; otherwise flat. That's a *vote*,
not a strict AND — this is deliberately looser than a conjunction so it
doesn't require all three timeframes to agree. No stop-loss/take-profit at
all — pure binary in/out allocation of the whole $1,000 book. Needs 201 days
of warm-up before it can vote at all, which is part of why — per the current
state check — it's been sitting idle since deployment on July 13th: if BTC's
three signals haven't crossed the 2-of-3 threshold yet, or the arm only just
finished warm-up, zero trades is expected behavior, not a bug.

## 8. Funding-rate arb (three sub-arms: aggressive, majors, Kraken)

Same engine, different dials. Every opportunity from the live funding
scanner runs this gauntlet before becoming a position:

1. Not halted (kill switch or the arm's own drawdown cap breached) and not
   vetoed by the "deleveraging regime" check (≥60% of majors going negative
   funding = fear regime, pause all new entries).
2. Symbol not already held, not in a 48h post-loss cooldown.
3. Exchange allowed for this arm (e.g. Kraken arm only trades Kraken
   Futures).
4. Majors/Kraken arms: funding must be positive (no shorting-spot-to-collect
   for these two — only the aggressive/fantasy arm takes negative-funding
   trades).
5. APY between 15% and 150% (Kraken cap 300%).
6. **Breakeven gate**: the funding rate must repay the round-trip cost within
   a set number of 8h cycles — 10 cycles for aggressive/majors, tightened to
   just 4 for Kraken. This is the arm asking "will this actually be
   profitable before it decays?" before entering.
7. Persistence check: verified (not assumed) that the rate has actually held
   for the required number of cycles and hasn't flip-flopped too many times.

Position size scales linearly with how far above the cost floor the APY sits
— a juicier rate gets more capital, up to a cap. Direction: long spot / short
perp when collecting from longs, reverse when collecting from shorts (which
also means borrowing the spot leg — a real cost, charged separately: 10%/yr
for majors, 50%/yr for alts).

**Exit**: hard 7-day max hold; or funding flips sign, or decays to under 40%
of its entry rate — but either of those must persist for 2 continuous hours
before it actually closes, so one noisy data point can't force a costly
round-trip. Also exits if the symbol vanishes from the scanner for 24h.

This is the arm you saw net-negative in the last check (majors −$11, Kraken
−$28) — it's designed to be a small, honest, market-neutral drag most of the
time, not a big winner; the value is that it stays correct even when it
loses.

## 9. rebalance_paper — 50% crypto / 25% gold / 25% cash

10 crypto majors equal-weighted to 5% each, PAXG (tokenized gold) to 25%,
cash 25%. Purely time-triggered — every 30 days, no matter what price did,
sell whatever drifted above target and buy whatever drifted below, back to
those weights. Cost = total turnover × 0.26%. It also tracks an identical
never-rebalanced buy-and-hold portfolio side by side, so every rebalance
event records not just its own return but the "premium" over just holding —
that premium, not raw return, is the actual thing being tested. No
stop-loss; it can lose in a real bear market by design, just less than raw
holding, via diversification and buying-low/selling-high on the rebalance.

## 10. brain — the Claude-Opus discretionary arm

The only non-mechanical arm. Once a day per coin, it calls the Claude API
and is forced to return a decision (long/short/flat), a conviction 1-10, and
a size multiplier, via a tool call — never free text.

What it's actually shown: a fixed system prompt (cost discipline, trend
protects downside not upside, sizing is bimodal — go meaningful or don't go
at all, never a token-sized bet), a separately-cached "lessons learned"
knowledge base, its own track record (win rate by coin/action, past
worst-trade post-mortems, whether its own past "conviction 8" calls actually
won ~80% of the time), system-wide proof-bar status (so it knows if nothing
is actually proven yet and should stay humble), session/flow/macro context
per coin, and optionally weekly/daily candlestick chart images explicitly
framed as confirm-or-veto only — "when the chart and the numbers disagree,
trust the numbers."

Conviction converts to size in fixed bands: 9-10 → 2-2.5x base size, 7-8 →
1.2-1.8x, 5-6 → 0.5-0.9x, ≤4 → flat regardless of direction called. Any API
failure or parse failure fails safe to holding yesterday's position, never a
blind trade. It also runs a second, non-trading role: reviewing every other
arm's book for hidden correlation, arms fighting each other, or overtrading
— advisory only, it can flag but never execute.

---

# The shared pipeline (directional intraday engine)

This is `src/paper_trading.py`'s main loop — the microstructure scalper that
all the OFI/CVD/lead-lag/probability-gate machinery in CLAUDE.md refers to.
**It is currently shelved by default** (`DIRECTIONAL_ENABLED=0` — its own
proof scorecard read t=-8.82 on 229 trades, a clear fail), so in production
almost every tick dies at step 5 below. Everything else in this pipeline —
and every other arm above — still runs live regardless of this flag; only
*this specific* engine's new entries are switched off.

Per symbol, every 2 seconds:

1. **Price/feed refresh** — live websocket price preferred over REST, order
   book and spread updated, funding rate pulled.
2. **Exit check runs first, unconditionally.** If a signal says exit
   (partial or full), it executes and the tick ends there — no new entry
   logic runs on a tick that just closed something.
3. **Shelved check** — if the directional engine is off, bump the funnel
   counter `skip:directional_shelved` and move to the next symbol. (This is
   the dominant log line right now.)
4. **Kill switch** — if engaged, skip.
5. **Strategy evaluation** — the microstructure strategy (OFI v2 + CVD +
   lead-lag + structure) produces a signal or HOLD. If HOLD, two fallback
   paths can still produce a synthetic signal: a mean-reversion path (only in
   ranging/volatile regimes) or an OFI+lead-lag "fast-track" (only if order
   flow and BTC lead-lag both agree strongly). Otherwise the tick ends here.
6. **Enrichment** — multi-timeframe alignment nudges confidence up or down;
   a stop-hunt detector adds a confidence bonus if a wick swept and reclaimed
   a swing level.
7. **Dual-direction check** (optional) — if both long and short pass the
   probability gate within 5 points of each other, that's read as noisy,
   contradictory tape and BOTH get rejected rather than picking one.
8. **Entry checklist** (`entry_checklist.py`) — a sequence of hard vetoes,
   any one of which kills the trade outright: minimum confidence, circuit
   breaker not tripped, not in cooldown (300s after a stop-loss, 60s after a
   normal exit), no repeat entry on the same bar, price not stale, under the
   max open-position count, order flow aligned, sentiment not extreme-fear
   (longs only), no active kill-filter (funding extreme / whale print / book
   imbalance / CVD divergence), **ATR-alive** — volatility must be at least
   0.15% of price, i.e. the bar's expected move must be able to clear its own
   cost, or it's refused outright — spread not blown out, VPIN (toxic flow)
   under threshold. Then a *soft* score (RSI health, ADX strength, volume,
   lead-lag alignment, funding favorability) must clear 0.4 if no hard veto
   already killed it.
9. **Position sizing (first pass)** — confidence-tiered base size as a
   percent of equity, capped at 15% of equity; adjusted for realized/implied
   volatility (smaller size when the market's noisier). Below $1.50, skip.
10. **Probability gate** (`probability_gate.py`) — this is the "stacks ~10
    edges" system:
    - Up to 9 independent edges (rule confidence, order flow, lead-lag,
      regime, RSI, ADX, higher-timeframe, funding, one deduped macro edge)
      are combined with a correlation-shrunk formula — not simple averaging,
      because naively combining correlated edges overstates confidence; each
      edge is capped at 95% before combining so no single edge can dominate.
    - If there's enough of its own trade history (40+ trades), that combined
      probability gets recalibrated against reality (isotonic calibration) —
      the raw math gets corrected by "how often did a call like this
      actually win historically."
    - **Kelly sizing**: computes full Kelly from the calibrated win
      probability and the trade's actual reward:risk, then uses a quarter of
      that (conservative fractional Kelly), scaling size accordingly.
    - **Conviction tiers**: the trade is classified into one of four buckets
      (conviction/position/swing/scalp) based on how high the probability is
      AND how many independent edges support it — a single very-confident
      edge doesn't qualify for a top tier, it needs both confidence and
      corroboration.
    - **Reject** if the calibrated probability is below 65%, or if the tier
      it qualifies for ranks below "swing" — this is specifically designed
      to kill "technically high probability but from one flimsy edge"
      scalps.
11. **Final sizing & gates** — tier-based dollar size (replacing the earlier
    confidence-based size), correlation check (a trade correlated with an
    already-open position is blocked *unless* it's macro-driven, in which
    case it's deliberately allowed to stack), and an expectancy gate that
    caps size for any entry path (main signal vs mean-reversion vs
    fast-track) until that path has proven itself with real trades — a brand
    new entry path starts on a leash regardless of how confident this one
    signal looks.
12. **Execute** — only after every one of the above passes does an actual
    buy/short fire.

**Where trades actually die, in order**: shelved flag → kill switch → base
strategy returns HOLD → checklist hard veto → checklist soft score < 0.4 →
size under $1.50 → probability gate below 65% or wrong tier → correlation
block → expectancy-gate cap. A running counter for every one of these stages
prints once a minute in the logs as `[FUNNEL] seen=N hold=N actionable=N
executed=N | skips: ...` — that line is the direct, real-time answer to "why
isn't it trading."

**The core design idea running through everything above**: nothing here is
one signal deciding to trade. Every arm either has a hard-coded cost gate
that refuses trades where the expected move can't pay its own fee, or (in
the shelved intraday engine) an entire stacked-evidence system where a
single strong-looking signal is not enough on its own — it needs
corroboration from independent edges, a calibrated track record, and
Kelly-disciplined sizing before real money-equivalent capital moves. The
arms that stay simple (trend_ensemble, tsmom) trade that sophistication away
for transparency: fewer, dumber conditions, no discretion, and let time do
the work instead of confidence scoring.

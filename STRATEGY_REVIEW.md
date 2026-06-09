# Strategy Review & Diagnosis

*Rultless review grounded in the actual code, not the stale docs. Paper mode; no
real money. Constraints respected: US geo-block (Kraken-only execution), Kraken
spot maker floor 0.25%/side → 0.54% round-trip wall, percentage-based costs, no
unjustified ML.*

**Premise correction:** the intraday directional book is *already shelved*
(`DIRECTIONAL_ENABLED=0` default, `src/paper_trading.py`; proof scorecard already
failed it at 229 trades, expectancy −$0.088/trade, t=−8.82). The historical −$20
directional bleed is **legacy and already stopped** — the live levers are entirely
on the funding side.

---

## PART 1 — DIAGNOSIS

### 1. Per-arm scorecard

| Arm / signal | Verdict |
|---|---|
| **Aggressive funding** (Binance/Bybit, all symbols) | **THEATER.** Geo-blocked, rates not capturable on this account; its +P&L is fiction the code itself labels "fantasy." *Now quarantined off by default (this PR).* |
| **Majors funding** (Kraken-confined, positive-only, persistence-gated) | **WORKING but idle.** The honest conservative arm. Believable P&L, too few trades to prove anything. |
| **Kraken funding** (the only executable arm, 0.54% maker wall) | **WORKING but marginal + churning.** Structurally fixed for the cycle-0 bleed, but its exit logic round-tripped on single noisy snapshots — re-paying the wall. *Fixed this PR.* This is the only arm whose dollars are real. |
| **Microstructure scalper** (OFI/CVD/lead-lag on REST) | **DEAD.** Needs tick data, gets 2s REST snapshots; gate ~never fires. Pure compute + false comfort. |
| **MR + OFI/lead-lag fast-track** | **DISPROVEN, already shelved.** Part of the t=−8.82 directional book. mr-extreme was 7.1% WR (dead code removed this PR). |
| **Swing (long majors, forward paper)** | **UNPROVEN but honest.** The only directional bet built to clear the pre-registered bar; not there yet. |
| **DEX / stablecoin / triangular arb** | **DEAD weight.** Wired, disabled, geo-blocked. |

### 2. Root cause of every dollar lost (mechanisms, not symptoms)

- **Kraken −$29:** *cycle-0 funding flips on microcap PF_\* perps, amplified by
  exit churn.* The breakeven gate **assumed** persistence but never **verified** it,
  so the arm entered 300–600% APY microcaps whose funding reverted within the first
  hour/cycle. It collected ≈0 funding and ate the full 0.54% round-trip. With no
  re-entry cooldown, the same symbol (e.g. DEXE) was re-bought and flipped again.
  *Secondary, still-live until this PR:* `_should_exit` closed on a **single**
  snapshot showing sign-flip or APY < 40% of entry. On Kraken's noisy hourly
  funding, one bad print forced a full round-trip, then re-entry paid the wall
  again. The 0.54% wall punishes round-trips; the exit logic manufactured them.
- **Directional −$20:** *cost wall on negative-EV scalps.* Fast-track/MR signals
  fired on 2s REST snapshots (OFI from stale books), bypassed the conf gate at
  conf 50–55, and paid ~0.3% round-trip on edge-less signals → death by a thousand
  cuts. **Already diagnosed and shelved.**

### 3. Edge audit per signal

| Signal | Status | Why |
|---|---|---|
| **OFI** (book imbalance v1, REST) | **DEAD** | Real OFI needs L2 tick deltas; 2s snapshots alias it into noise. |
| **CVD** | **UNVALIDATED** | Needs trade-tick feed; reconstructed coarse on REST. |
| **Lead-lag** (BTC → alts) | **UNVALIDATED** (plausible theory) | Correlation + strength heuristic, no lag estimation or cointegration rigor. |
| **Funding persistence** | **PLAUSIBLE → becoming PROVEN** | The only signal with a real mechanism *and* live evidence: winners persisted 7–12 cycles, losers 0–1 (basis of `min_persistence_cycles=3`). |
| **Mean reversion** | **DEAD** (extreme) / **UNVALIDATED** (ranging) | mr-extreme measured 7.1% WR; ranging MR never validated. |
| **Sentiment filter** (FNG, BTC dom) | **UNVALIDATED** | Soft gate, no isolated P&L contribution. |

---

## PART 2 — BEST-PRACTICE GAP ANALYSIS

*(now → fund practice → gap → expected impact)*

### Funding arb
- **Persistence scoring:** have `consecutive_positive_cycles` + `flip_count`. Funds
  also score **funding half-life / decay rate** and **funding z-score vs trailing
  distribution**. Gap: entry is a binary stability test, exit was a single-snapshot
  threshold (now confirmation-gated). Largest single lever (Part 3).
- **Flip-rate per symbol:** have `flip_count` (30d). Gap: not used to *size* — a
  1-flip and a 5-flip symbol get equal conviction. Minor.
- **Basis / term-structure:** **absent.** Funds trade the **perp-spot basis** and
  **calendar/term structure** directly. On Kraken this is the real path to break the
  maker wall (carry the basis, roll the perp, pay spot fees once). High impact, high
  effort.
- **Cross-exchange spread capture:** **infeasible** (geo-block leaves only Kraken). Drop.
- **Funding decay modeling:** **absent.** Model funding as mean-reverting
  (**Ornstein-Uhlenbeck**) to time exits. Medium impact; the exit-churn fix is the
  cheap 80%.

### Microstructure
- **OFI normalized by volume; queue position; Kyle's λ (price impact per unit
  flow); Hasbrouck information share; VPIN:** VPIN monitor exists, rest absent. **The
  gap is the data feed, not the math** — all need a real Kraken L2/trade websocket
  (`src/kraken_ws.py` exists but isn't wired into the gate). Given the REST
  constraint, **none are reachable today**; don't invest until the feed exists.
- **Lead-lag with cointegration (Engle-Granger / Johansen):** absent (correlation
  only). Only worth it if directional is revived. Defer.
- **CVD divergence, liquidation heatmap:** need tick/liquidation feeds. Defer.

### Execution
- **Post-only with cancel-replace; maker-rebate optimization; fill-quality tracking;
  implementation shortfall; adverse-selection measurement; Almgren-Chriss sizing:**
  all absent. The sim **assumes maker fills** at 0.54%; *post-only + cancel-replace*
  (chase the touch without crossing) is what would make that assumption true. There
  is no live execution at all yet — until an edge is proven, the only execution work
  worth doing is **measuring realized vs assumed fill** so paper P&L stops lying.
  Medium impact (truthfulness, not dollars yet).

### Risk
- **Per-strategy VaR/CVaR; per-arm drawdown limits; correlation caps:** only a global
  `DailyCircuitBreaker`; each arm has a ledger but no per-arm DD kill.
  **Kelly by strategy:** directional uses quarter-Kelly (`probability_gate`); funding
  arms use conviction-by-APY, no Kelly. **Regime detection (HMM / vol-regime):**
  `regime_detector` is rule-based, not an HMM. A per-arm drawdown kill is cheap
  insurance; the rest is over-engineering for current size.

### Research
- **Walk-forward:** one mention (`src/altperp/research_trend.py`); not systematic.
- **Purged k-fold; combinatorial purged CV (López de Prado); deflated Sharpe (DSR);
  probabilistic Sharpe (PSR); minimum backtest length:** **all absent.** The proof
  scorecard uses a clustered (design-effect) t-stat — good, but **not deflated for
  the number of configs tried.** With this many arm tweaks the in-sample t-stat is
  multiple-testing-inflated. Adding DSR/PSR to `proof_scorecard.py` is the
  highest-value research upgrade and is cheap. High impact on *decision quality*.

### Attribution
- **P&L per arm:** yes (separate ledgers). **Per signal / symbol / regime /
  time-of-day:** **absent.** For funding, per-symbol and per-hold-length attribution
  would directly show which majors actually pay. Medium impact, low effort.

---

## PART 3 — DECISIONS

### 4. The single highest-impact change this week

**Stop the funding-arb exit churn: require *sustained* decay/flip before closing
(implemented this PR).** Soft exits (`funding_flipped`, `apy_decayed`) are now gated
behind `EXIT_CONFIRM_HOURS` (default 2h) via persisted `pending_exit_*` state;
recovery clears it; hard exits (max_hold, off_scanner_24h) stay immediate.

- **Reasoning:** the 0.54% round-trip is the binding constraint. A single noisy
  Kraken hourly print used to force a full round-trip + re-entry ≈ **1.08% paid for
  noise.** Confirming over 2h costs at most ~0.007% extra paid funding on a real flip
  (30% APY × 2h) — **two orders of magnitude cheaper** than the round-trip avoided.
- **Metric:** average `cycles_collected` per closed position rises; closes tagged
  `funding_flipped`/`apy_decayed` **with `cycles_collected < 2`** trend to zero
  (surfaced as `soft_exit_churn` in `get_summary()` and the rollup log); Kraken-arm
  rolling-7d net (`net_pnl_since`) improves.

### 5. Top 3 by P&L-per-hour-of-work

1. **Exit-confirmation window** (done). ~1–2h. Recovers churned cost on the only real
   arm. Metric: avg cycles/trade ↑, sub-2-cycle soft-exits → 0.
2. **Quarantine the FANTASY aggressive arm** (done). ~15 min via
   `FUNDING_ARB_AGGRESSIVE_ENABLED=0` default. Stops misleading "we're up" Telegram
   alerts and self-deception. Metric: only majors+Kraken arms in heartbeat/rollups.
3. **Deflated / probabilistic Sharpe in `proof_scorecard.py`** (next). ~1–2h. Adds
   PSR/DSR alongside the clustered t-stat so the proof bar accounts for multiple
   testing. This stands between "paper green" and "funding a mined artifact." Metric:
   scorecard prints DSR; bar requires DSR-adjusted significance, not raw t.

### 6. Kill list (all reversible)

- **Aggressive funding arm** → OFF by default (`FUNDING_ARB_AGGRESSIVE_ENABLED=0`).
  FANTASY and actively misleading via Telegram. **Done.**
- **mr-extreme dead branch** (`_rsi_extreme=False` path in `paper_trading.py`) →
  deleted; only the plain (still-unvalidated, directional-gated) RANGING MR remains.
  **Done.**
- **Microstructure scalper compute** → already idle under `DIRECTIONAL_ENABLED=0`;
  left in place behind the flag (no live cost) but treated as dead.
- **Dual-direction probe** → meaningful only for the shelved directional book;
  no-op while directional is off (no change).
- **DEX / stablecoin / triangular arb** → already disabled; left archived.
- *Do NOT* touch the persistence/cooldown gates or `atr_alive` — those are the good
  filters, not overhead.

### 7. 30-day roadmap

- **Week 1 — Stop the bleed, stop the lies.** Ship the exit-confirmation window,
  quarantine the aggressive arm (both done), add per-symbol + per-hold-length
  attribution to the Kraken/majors rollups. **Gate:** Kraken arm posts *zero*
  sub-2-cycle soft-exits for 7 days.
- **Week 2 — Make the proof honest.** Add DSR/PSR to `proof_scorecard.py`; add
  per-symbol funding-decay (half-life) stats to `FundingHistory`. **Gate:** scorecard
  reports DSR for every arm.
- **Week 3 — Earn the carry.** Time entries on decay/half-life (enter only when
  funding z-score is high *and* persistence ≥3 cycles *and* predicted hold clears the
  wall). Begin perp-spot **basis** measurement on Kraken as the real wall-breaker.
- **Week 4 — Validate, don't deploy.** Walk-forward the Kraken arm on the accumulated
  forward-paper ledger; compute DSR. **Real-money gate:** executable AND n≥30 closed
  AND expectancy>0 AND DSR-significant AND ≥30 days forward paper. If it doesn't
  clear, it stays paper — no exceptions.

---

**If you do nothing else this month, do this: make the Kraken funding arm hold its
carry instead of churning it — confirm decay/flips before closing so you stop paying
the 0.54% maker wall twice for noise.**

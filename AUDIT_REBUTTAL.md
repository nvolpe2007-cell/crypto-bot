# Rebuttal & Correction — SYSTEM_AUDIT.md

*Re-grounding the 2026-05-05 "Comprehensive System Audit" (signed Claude Opus 4.7)
against this project's actual edge thesis and live code paths.*

*Author: Claude Opus 4.8 — 2026-05-30*

---

## TL;DR

The audit is well-structured and parts of it are useful (overfitting detection,
fail-closed OFI, detailed skip logging). But it has two classes of problem:

1. **Code bugs** — several proposed snippets won't run as written (syntax errors,
   a typo'd variable, invalid f-string format specs, a tangled ternary).
2. **A thesis that contradicts the project's documented core principle.** The audit
   frames the ~5-15% win rate as a *tuning* problem fixable by upweighting ML and
   adding entry rules, targeting "40-55% win rate." Our own diagnosis
   (`CLAUDE.md`, memories `strategy_cost_expectancy_fix`, `bot_idle_dead_vol`,
   `user_goal_minimize_losers`) is different: **transaction costs (~0.3% round-trip)
   dominate at this size, so most candidate trades are negative-EV and idleness is
   correct.** The audit never mentions the cost floor at all.

Treat the audit as a source of *individual* ideas, not as a plan. Do not implement
its Tier-1 "raise ML weight to 70%, force more structure" wholesale — that is exactly
the "loosen/retune gates to force trades" move `CLAUDE.md` warns against.

---

## Part 1 — Code that won't run as written

Line references are into `SYSTEM_AUDIT.md`.

| Where | Problem | Fix |
|---|---|---|
| `:124` | `import sklearn.model_selection import train_test_split` — double `import`, `SyntaxError`. | `from sklearn.model_selection import train_test_split` |
| `:839` | `if of_i < 0:` — typo'd variable inside the new "strict OFI" `confirms_buy`; `NameError` on first bearish-but-not-strong tick. | `if ofi < 0:` |
| `:1037` | `f"... ml_prob={ml_prob:.2f if ml_prob else 'N/A'} ..."` — the conditional is *inside* the format spec. Raises at runtime. | Compute first: `mlp = f"{ml_prob:.2f}" if ml_prob else "N/A"`, then interpolate `{mlp}`. |
| `:1086`, `:1088` | Same invalid f-string pattern (`{ml_prob:.2f if ...}`, `{sig.ofi:.3f if ...}`). | Same fix — precompute the string. |
| `:1013` | `if ofi_calc and not ofi_calc.confirms_buy(sym) if sig.is_buy else not ofi_calc.confirms_sell(sym):` — ternary binds across the whole boolean chain; the `ofi_calc and ...` guard is not applied to the sell branch, and the precedence is almost certainly not the intent. | Split explicitly: `ok = ofi_calc.confirms_buy(sym) if sig.is_buy else ofi_calc.confirms_sell(sym); if ofi_calc and not ok: return False, "OFI_BLOCKS"` |

None of these are fatal to the *ideas*, but they show the audit's code was not
executed. Anything copied from it must be run before it goes anywhere near
`live_trading.py`.

---

## Part 2 — Stale / mismatched targeting

- The audit attributes line numbers to `ml_scorer.py` and `learner.py` and writes
  several fixes against **`live_trading.py:510-600`**. Both `ml_scorer` and `learner`
  *are* real and live (imported by `paper_trading.py:39,41` and instantiated at
  `:754,:760`), so that premise holds. **But the authoritative live loop is
  `paper_trading.py`** (`run_paper_trading_session`), per `CLAUDE.md` and memory
  `deployment_topology`. `live_trading.py` is a parallel/secondary path. Patches
  written only against `live_trading.py` (the audit's Optimizations 19-20) would not
  affect what the VPS actually runs.
- The cited line numbers (e.g. `ml_scorer.py:226-244`) should be re-verified against
  the current files before editing — the audit is ~3.5 weeks old and predates several
  commits (Kraken fee model, ProbabilityGate tightening, WS L2 book, weekly report).

---

## Part 3 — The thesis problem (the important part)

The audit's headline table (`SYSTEM_AUDIT.md:1116`) promises:

> Win Rate 5-15% → **40-55%** (+300-400%), Profit Factor 0.0 → 1.5-2.5

by (Tier 1) raising ML weight to 70%, adding a 65% ML win-prob hard gate, and adding
RSI/ADX/hours entry filters.

Three objections:

**1. It treats low win rate as the disease. It's a symptom.**
Per memory `strategy_cost_expectancy_fix`, the ~1% historical win rate's root cause
was **targets smaller than round-trip costs (negative EV)** — not a weak classifier.
You can have a 70% win rate and still lose money if winners are smaller than the
~0.3% cost drag. The audit optimizes win rate (a recall-ish proxy) while the project's
stated goal (memory `user_goal_minimize_losers`) is **expectancy and precision**. Win
rate is the wrong objective to headline.

**2. "Upweight ML to 70%" increases reliance on the weakest component.**
The audit itself flags (correctly) that the ML model is trained on tiny samples
(`MIN_TRADES=30`), overfits, and has redundant features. Its own Optimization 1
then makes that fragile model the *dominant* 70% vote. That's backwards: fix the
overfitting (Optimizations 2-4, 18 — which are genuinely good) **before**, not while,
upweighting it. Until the model clears a held-out AUC bar, its weight should go *down*,
not up.

**3. It never mentions the cost floor.**
`CLAUDE.md`'s core principle — costs dominate at this size, `atr_alive` correctly
refuses negative-EV trades, **don't loosen gates to force trades** — appears nowhere
in the audit. The whole document is premised on the bot trading *more* and *better*;
the project's actual finding is that the directional side has **no proven edge** and
the believable P&L comes from the **funding-arb arms** (`arbitrage/funding_arb_paper.py`,
memory `funding_arb_live`). An honest audit would lead with: "the directional scalper
may not have positive expectancy net of cost; prove it has an edge before tuning it."

---

## Part 4 — What to actually keep

Salvage these (they're cost-aware and don't fight the thesis):

- **Optimization 4 / 18 — train/val split + held-out AUC gate, reject non-predictive
  models.** Good. This is *gating* the ML, not loosening it. Implement the AUC≥0.52
  and overfit-gap checks. (Fix the `:124` import bug first.)
- **Optimization 16 — fail-closed OFI when data is missing.** Aligns with "refuse
  negative-EV trades." (Fix the `of_i` typo.) Note: this overlaps with the existing
  microstructure `_confluence_gate` (`microstructure_strategy.py:684`), which already
  fails *closed* on missing OFI v2 (`:714-716`) — so apply this only to the v1
  fast-track path, not the confluence gate.
- **Optimizations 19-20 — centralized decision function + per-reason skip counters.**
  Useful observability, *if* ported to `paper_trading.py` (not `live_trading.py`) and
  reconciled with the existing `[FUNNEL]` heartbeat (`paper_trading.py:2041`) rather
  than duplicating it.

Drop or defer:

- **Optimization 1 (ML weight → 70%, before fixing overfitting).** Contradicts the
  thesis; do the opposite ordering.
- The **40-55% win-rate target and the impact table** — unsupported, and the wrong
  objective. Replace with an **expectancy-net-of-cost** target and a **trades-taken /
  EV-per-trade** dashboard.
- Anything whose only home is `live_trading.py`.

---

## Suggested re-framed priority

1. **Measure first.** Compute current expectancy *net of the Kraken fee model* per
   strategy arm (directional vs funding-arb). Establish whether the directional side
   is even positive-EV before tuning it. This is the step the audit skipped.
2. **Gate the ML, don't lean on it.** Land the held-out-AUC / overfit checks; keep ML
   weight low until it clears the bar.
3. **Observability.** Per-reason skip counts into the existing funnel.
4. **Only then** consider entry-quality changes — and judge them by EV-per-trade and
   drawdown, never by raw win rate or trade count.

The one-line version: *the bot sitting idle in flat markets is the audit's "problem"
and the project's correct behavior. Don't fix the feature.*

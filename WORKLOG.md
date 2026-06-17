# WORKLOG — multi-agent coordination

This repo is worked by more than one autonomous agent (e.g. an interactive Claude Code
session on the owner's computer, and scheduled cloud routines like "dispatch"). They do
NOT share memory. **This file is the shared, git-tracked state.** Read it before you start;
append an entry when you begin meaningful work and when you finish.

## Rules (also in CLAUDE.md → "Multi-agent coordination")
1. **Do not push to `master` directly.** Work on a branch, open a PR, let the owner merge.
2. **`git fetch` before you start.** Master may have moved since your last context.
3. **Stay in your lane** (see map below). Touching another lane's files → coordinate here first.
4. After pushing, **verify `python -m pytest tests/ -q` still collects** (2 known pre-existing
   fails: `test_exchange` batching, `test_notifications` env-default — not yours).

## Lane map
| Lane | Owner | Primary files |
|---|---|---|
| Directional strategy | dispatch | `src/paper_trading.py`, `src/scientific_strategy.py`, `src/entry_checklist.py`, `src/live_trading.py`, `src/pairs_strategy.py`, `src/orderflow_ws.py`, `src/indicators.py` |
| Brain / risk / observability | Claude Code (computer) | `brain_paper.py`, `brain_overseer.py`, `src/trade_brain.py`, `src/market_context.py`, `src/attribution.py`, `arbitrage/funding_arb_paper.py`, `src/kill_switch.py`, `proof_scorecard.py` |
| Shared — coordinate before editing | either | `config.yaml`, `run_all_bots.py`, `CLAUDE.md`, this file |

---

## In-flight / open items
- [ ] **Combine pending.** Dispatch's rewrite (`54675f4`) replaced `src/paper_trading.py` and
  dropped the multi-arm wiring (funding arb, brain arm, brain overseer, triarb, funnel). The
  arm code still exists as standalone files (`brain_*.py`, `trade_brain.py`, `market_context.py`)
  — only the ~30 lines of in-process launchers + the heartbeat brain-MTM segment need re-attaching
  to the new loop. Held until concurrent work is confirmed idle.
- [ ] **Policy fork — ATR gate.** `cef65a2` lowered `atr_alive` 0.15% → 0.08% (below the ~0.3%
  round-trip cost wall). This contradicts the documented core principle ("do not loosen gates to
  force trades"). NOT yet on the VPS. Owner to decide keep vs revert.
- [ ] **Policy fork — funding arms.** Removed by the rewrite. They were the only thing trading in
  calm markets, but the attribution ledger had them net-negative. Owner to decide re-add vs retire.

## Log
| Date (UTC) | Agent | Branch/PR | What |
|---|---|---|---|
| 2026-06-15 22:00–22:55 | Claude (computer) | merged to master | Brain MTM + drawdown stop; triarb phantom killed + ledger purged; brain in heartbeat; portfolio overseer; desk-context enrichment (commits 31b6e96→f1a3cc6). |
| 2026-06-15 ~23:00–05:15 | dispatch | merged to master | Strategy rewrite (ATR trailing stops / 3-signal consensus / session filter / tiered sizing), statarb pairs trading, supertrend fix, ATR-gate loosening (commits 54675f4→cef65a2). Overwrote `paper_trading.py`, dropping the multi-arm wiring above. |
| 2026-06-16 | Claude (computer) | `coordination-scaffolding` → PR | Added this WORKLOG + CLAUDE.md coordination section. No code/strategy changes. |
| 2026-06-16 | Claude (computer) | `btc-trend-focus` → PR | Owner-requested focused single strategy: `btc_trend_paper.py` — BTC-only, 100%-book, SMA100 + 20d-momentum confluence (long/cash), paper forward. Wired into `proof_scorecard.py` as another forward arm (k→+1). Non-destructive: other arms untouched. Seeds CASH (BTC confluence currently off). |
| 2026-06-16 | Claude (computer) | `brain-chart-vision` → PR | Owner-requested chart-vision for the AI brain: `src/chart_render.py` (dependency-free candlestick+SMA PNG via numpy/zlib, no matplotlib), brain `decide()` now attaches per-coin chart images for the vision model + chart-reading discipline in the prompt (charts confirm/veto, never trade a pattern alone). Brain lane (mine). Fail-safe to text-only; BRAIN_CHARTS=0 disables. Tests added; suite green (2 known fails). |

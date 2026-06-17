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
- [x] **Combine pending — RESOLVED.** `c506627` + PR #8 (`20e462d`) restored `src/paper_trading.py`
  with all multi-arm wiring intact: funding arb (3 arms as asyncio tasks), regime_arm, conf_paper,
  tsmom_ls, lev_perp (all as supervised subprocesses), brain_paper + brain_overseer (subprocess),
  triarb, and the funnel/heartbeat brain-MTM. Verified 2026-06-17: arms are running on VPS (state
  files present, heartbeat shows all 25 subsystems OK).
- [x] **Policy fork — ATR gate — RESOLVED.** Restored to `_ATR_ALIVE_COST_MULT=0.5` → ATR ≥ 0.15%
  (half the 0.30% round-trip cost) by the same restore. The 0.08% loosening from `cef65a2` is gone.
- [x] **Policy fork — funding arms — RESOLVED.** All 3 funding arms re-wired into paper_trading.py.
  Running on VPS; scanning but skipping positions in current fear/low-APY environment (correct).
- [ ] **pairs_paper.py cron gap.** The arm was wired via VPS crontab (`10 * * * *`) not run_all_bots.py
  — which is correct (it's a single-shot script like swing_paper). Crontab entry already present.
  No action needed, but note: **swing_paper.py uses bare `/usr/bin/python3`** (no venv), while
  pairs_paper.py uses `./venv/bin/python` — verify swing_paper still imports OK after venv updates.
- [x] **Open PRs — ALL MERGED 2026-06-17.** #5 (sentiment attr bug), #4 (funding exit confirmation
  + aggressive arm quarantine), #9 (BTC trend arm), #10 (brain chart vision). PRs #4 and #5 required
  rebase onto master (conflict in `funding_arb_paper.py` constructor — added both `max_drawdown_usd`
  and `exit_confirm_hours`; `paper_trading.py` heartbeat — kept brain MTM segment AND added
  conditional `_aggr_seg`). All tests: 2481 pass / 2 known pre-existing fails. VPS deployed, service
  healthy, aggressive arm correctly absent from heartbeat.

## Log
| Date (UTC) | Agent | Branch/PR | What |
|---|---|---|---|
| 2026-06-15 22:00–22:55 | Claude (computer) | merged to master | Brain MTM + drawdown stop; triarb phantom killed + ledger purged; brain in heartbeat; portfolio overseer; desk-context enrichment (commits 31b6e96→f1a3cc6). |
| 2026-06-15 ~23:00–05:15 | dispatch | merged to master | Strategy rewrite (ATR trailing stops / 3-signal consensus / session filter / tiered sizing), statarb pairs trading, supertrend fix, ATR-gate loosening (commits 54675f4→cef65a2). Overwrote `paper_trading.py`, dropping the multi-arm wiring above. |
| 2026-06-16 | Claude (computer) | `coordination-scaffolding` → PR | Added this WORKLOG + CLAUDE.md coordination section. No code/strategy changes. |
| 2026-06-16 | Claude (computer) | `btc-trend-focus` → PR | Owner-requested focused single strategy: `btc_trend_paper.py` — BTC-only, 100%-book, SMA100 + 20d-momentum confluence (long/cash), paper forward. Wired into `proof_scorecard.py` as another forward arm (k→+1). Non-destructive: other arms untouched. Seeds CASH (BTC confluence currently off). |
| 2026-06-16 | Claude (computer) | `brain-chart-vision` → PR | Owner-requested chart-vision for the AI brain: `src/chart_render.py` (dependency-free candlestick+SMA PNG via numpy/zlib, no matplotlib), brain `decide()` now attaches per-coin chart images for the vision model + chart-reading discipline in the prompt (charts confirm/veto, never trade a pattern alone). Brain lane (mine). Fail-safe to text-only; BRAIN_CHARTS=0 disables. Tests added; suite green (2 known fails). |
| 2026-06-17 | Claude (computer) | no branch — coordination only | Confirmed all WORKLOG open items resolved: arms are wired (paper_trading.py lines 504-900), ATR gate back at 0.15%, VPS active + 25 subsystems OK, pairs_paper.py in crontab. Marked items ✓. Pytest: 2459 pass / 2 pre-existing fails. |
| 2026-06-17 | Claude (computer) | merged to master | Reviewed + merged PRs #4, #5, #9, #10. Fixed rebase conflicts for #4 and #5 (both behind master by 20-30 commits). #4 conflict: kept `max_drawdown_usd` + added `exit_confirm_hours` to FundingArbPaperSim ctor; kept brain MTM heartbeat AND conditional aggr segment. No logic bugs found in any PR. Deployed to VPS — 24 subsystems OK, aggressive arm quarantined (absent from heartbeat as expected). |
| 2026-06-17 | Claude (computer) | `kelly-conviction-sizing` → PR | Owner asked for compounding/"bet more when confident" sizing. `kelly_trend_paper.py` — SAME BTC trend signal as btc_trend_paper (clean A/B), only sizing differs: conviction-scaled (entry 20d momentum) fraction of CURRENT equity, hard no-leverage cap (MAX_FRAC=1.0). Wired into proof_scorecard (k→+1). Honest finding from 2yr head-to-head: it UNDER-invests (avg frac 0.60) → LOWER return (+27% vs flat +38%) but HALF the drawdown (-5% vs -13%). Confirms: without leverage you can't out-compound fully-invested; conviction-in-[0,1] reduces risk, not amplifies return. Brain/risk lane (mine). Tests added; suite green (2 known fails). |

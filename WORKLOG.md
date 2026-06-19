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
| 2026-06-17 | Claude (computer) | `brain-multi-timeframe` → PR | Owner-requested multi-timeframe vision for the brain. `chart_render`: parametrized SMA periods + `resample_weekly()` + `render_multi_timeframe()` (weekly SMA13/26/52w + daily SMA50/100/200d, labeled pair). `trade_brain._build_user_content` now accepts coin→list of (label,b64) (back-compat with bare str); prompt's chart section rewritten for weekly-bias + daily-timing alignment. `brain_paper` renders both per coin. LIVE-VERIFIED: vs daily-only (BTC FLAT 'no regime'), weekly+daily flipped BTC to SHORT conv6 size0.8 ('weekly downtrend, daily bounce into resistance → modest size') — feature materially changes decisions as designed. Brain lane (mine). +13 tests; suite 2488 pass / 2 known fails. Also noted: local .env has a UTF-8 BOM (shell-source mangles it). |
| 2026-06-17 | Claude (computer) | `brain-memory-opus` → PR (stacks on #12) | Owner: "give it more power like a pro trader". (1) MEMORY/feedback loop: `brain_paper.build_memory()` feeds the brain its own recent decisions + closed-trade outcomes (entry thesis carried onto positions via `_open`/`_close`) + equity/drawdown + conviction-calibration table; prompt's new "TRACK RECORD & MEMORY" section says learn-by-sample-size-not-recency, calibrate conviction, respect drawdown. (2) ENGINE: default model → claude-opus-4-8 + adaptive thinking (`thinking:{type:adaptive}` + `output_config.effort=high`; NOT enabled/budget_tokens — that 400s on 4.8) via BRAIN_THINKING/BRAIN_EFFORT; tool_choice falls back to auto (forced tool incompatible w/ thinking), prompt now says "you MUST call submit_decisions". LIVE-VERIFIED end-to-end on Opus 4.8 (~$0.10/call, ~$3/mo). Cost note: pricier than Sonnet. +9 tests; suite 2494 pass / 2 known fails. |
| 2026-06-17 | Claude (computer) | `brain-desk-blocks` → PR (stacks on #13) | Owner: "keep going down the list, add them like lego blocks that work together". New `src/desk_blocks.py`: composable, fail-safe, env-toggleable context providers merged into one `macro.desk_blocks` bundle — (1) cross_asset (S&P/dxy/gold/10Y 20d + risk_on/off, free Yahoo daily; stooq was 404-blocked), (2) flow (per-coin SLOW daily up/down-volume CVD proxy from Kraken vol + divergence read — NOT ticks), (3) risk_budget (brain's own net/gross exposure + all_long/short correlation concentration). `brain_paper` adds volume to fetch + composes blocks; prompt gains a DESK BLOCKS section (refine/risk-check, not new triggers). LIVE-VERIFIED on Opus 4.8: brain cited all three (closed a wrong-way long to avoid "a third correlated bet", read positive 5d flow as squeeze risk). BRAIN_DESK_BLOCKS=0 / per-block flags. +12 tests; suite 2506 pass / 2 known fails. |
| 2026-06-17 | Claude (computer) | `kelly-conviction-sizing` → PR | Owner asked for compounding/"bet more when confident" sizing. `kelly_trend_paper.py` — SAME BTC trend signal as btc_trend_paper (clean A/B), only sizing differs: conviction-scaled (entry 20d momentum) fraction of CURRENT equity, hard no-leverage cap (MAX_FRAC=1.0). Wired into proof_scorecard (k→+1). Honest finding from 2yr head-to-head: it UNDER-invests (avg frac 0.60) → LOWER return (+27% vs flat +38%) but HALF the drawdown (-5% vs -13%). Confirms: without leverage you can't out-compound fully-invested; conviction-in-[0,1] reduces risk, not amplifies return. Brain/risk lane (mine). Tests added; suite green (2 known fails). |
| 2026-06-17 | Claude (computer) | `fee-repricing-study` → PR | Owner scoped low-fee venue. `scripts/fee_repricing.py` re-prices 6 strategies at 0.045%→0.54% RT. Verdict: cheaper fees revive NOTHING (high-freq edge-dead at zero fee; money-makers already profitable at Kraken taker). Hyperliquid US-geoblocked anyway. Owner confirmed KRAKEN-ONLY. Real prize = legal shorting via Kraken US perps. Read-only. |
| 2026-06-19 | Claude (routine) | `fix/ofi-htf-per-symbol-failure-tracking` → PR | Cross-lane note: touches `src/paper_trading.py` (directional lane), fixing an observability bug, not strategy logic. Checked the 5 already-open PRs (#16-20, all live-trading-readiness/test-coverage work) plus every stale unmerged branch first to avoid duplicating effort — found that `_ofi_prefetcher`/`_htf_fetcher` in `paper_trading.py` still use a single shared `consecutive_failures` counter across all symbols (an old abandoned branch by the same name, orphaned by a master history rewrite, had flagged this exact bug back on 2026-06-03 but was never merged; reimplemented fresh against current master). Bug: any symbol's successful fetch resets the counter, so a symbol that fails *every* cycle (e.g. BTC/USD OFI down) while others succeed never reaches the alert threshold — the Telegram degraded-alert can go permanently dark for exactly the failure mode it exists to catch. Added `_SubsystemFailureTracker`: independent per-symbol counts, one-shot alert at threshold, recovery message on next success. +15 tests (`TestSubsystemFailureTracker`). Full suite: 2530 passed, 0 failures (the 2 previously-documented pre-existing fails are no longer present on master). `config.yaml` untouched (`sandbox: true`), no `.env` touched. |

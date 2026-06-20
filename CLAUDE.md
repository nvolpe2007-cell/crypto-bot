# Crypto Bot — Claude Context

## Multi-agent coordination (READ FIRST)
More than one autonomous agent works this repo (an interactive Claude Code session on the
owner's computer, plus scheduled cloud routines such as "dispatch"). **They do not share
memory** — coordination state lives in-repo, in [`WORKLOG.md`](WORKLOG.md) and here, never in
any agent's private memory. Rules:
1. **Never push to `master` directly. Work on a branch, open a PR, let the owner merge.** This
   is the gate that prevents one agent clobbering another's work (it has already happened once).
2. **`git fetch` before starting**, and read `WORKLOG.md` — master may have moved.
3. **Stay in your lane** (lane map in `WORKLOG.md`): directional-strategy files vs.
   brain/risk/observability files. Cross-lane edits → note it in `WORKLOG.md` first.
4. After pushing, confirm `python -m pytest tests/ -q` still collects (2 known pre-existing
   fails). Distrust "dead code" verdicts on files with fresh tests — they may be another agent's
   in-flight work. See memory `multi_agent_master_races`.

## Project Location
`D:\crypto-bot\` (authoritative). Deployed to a Hetzner VPS at `178.105.41.226`.

## What This Is
A multi-strategy crypto bot in **paper trading mode** (no real money). It is **not**
the simple "EMA+RSI scalper" older notes describe — it's an accretion of several
strategies sharing one execution loop. Honest status: the directional side has no
proven edge and is usually idle; the funding-arb arms are the only thing that trades
in calm markets. See memory `bot-idle-dead-vol` and `strategy_cost_expectancy_fix`.

## How it actually runs (verified — see memory `deployment_topology`)
- VPS systemd service `crypto-bot` → `python run_all_bots.py` (from `/opt/crypto-bot`).
- `run_all_bots.py` → `ScalpingBot.start()` (`src/bot.py`) → **`run_paper_trading_session()` in `src/paper_trading.py`** ← this is where the real loop lives.
- DEX/stablecoin arb are wired but **disabled** (Binance/Bybit geo-blocked for US).
- Several repo files describing other entry points are stale; trust the above.

## Strategies in the loop (all run at once)
1. **Microstructure directional scalper** (`microstructure_strategy.py`) — OFI v2 +
   CVD + lead-lag + structure confluence. Currently **dormant**: its gate needs
   tick-level websocket data but gets 2s REST snapshots, so it ~never fires.
2. **Mean-reversion + OFI/lead-lag fast-track** — fallback signal paths in
   `paper_trading.py` when the microstructure gate returns HOLD.
3. **Funding-rate arbitrage** (`arbitrage/funding_arb_paper.py`) — two market-neutral
   paper arms (see memory `funding-arb-live`):
   - **"Funding Arb"** — aggressive: all symbols, both sides, taker cost (optimistic).
   - **"Funding Arb (majors)"** — conservative/honest: liquid majors, positive funding
     only, maker cost. Rarely trades; its P&L is believable.

## Decision pipeline (entry)
signal → **entry checklist** (`entry_checklist.py`: `atr_alive` min-volatility, cooldown,
kill filters, circuit breaker, sentiment, MTF) → **ProbabilityGate** (`probability_gate.py`:
stacks ~10 edges, quarter-Kelly sizing, conviction tiers, isotonic calibration) → execute.
`[FUNNEL]` heartbeat log shows where signals die.

## Core principle (do NOT violate)
Costs (~0.3% round-trip) dominate at this size. Filters like `atr_alive` correctly
**refuse negative-EV trades** — that's why the bot sits idle in flat markets. **Do not
loosen gates to force more trades**; that recreates the old ~1% win rate.

## Key Files
```
src/bot.py                  # ScalpingBot entry; starts the paper session
src/paper_trading.py        # THE live loop: signals, gates, execution, funding arms, funnel
src/microstructure_strategy.py  # OFI/CVD/lead-lag confluence (dormant on REST)
src/probability_gate.py     # edge-stacking + Kelly/tier sizing + calibration
src/entry_checklist.py      # hard/soft entry gates (atr_alive, spread, vpin, session live here)
src/session_filter.py       # time-of-day edge: rates Asia/EU/US from the bot's own realised record
src/regime_detector.py      # trending/ranging/volatile/crash classification
src/order_flow.py / ofi_v2.py   # OFI v1 (book imbalance) / v2 (delta) — v1 feeds fast-track
arbitrage/funding_scanner.py    # real Binance/Bybit funding → state.json
arbitrage/funding_arb_paper.py  # cost-aware delta-neutral paper sim (2 arms)
config.yaml                 # pairs, risk, strategy params
run_all_bots.py             # process entry point (what the VPS runs)
```

## How to Run / Deploy
```powershell
python run_all_bots.py                          # run locally (what the VPS runs)
python -m pytest tests/ -q                      # tests
D:\crypto-bot\deploy\auto_deploy.ps1 -Commit -Message "..."   # commit→push→VPS pull→restart
ssh crypto-bot-vps "journalctl -u crypto-bot -f"             # watch live
```

## Funding-arb env knobs (no code change)
`FUNDING_ARB_MAX_APY` (cap, 150), `FUNDING_ARB_MIN_SIZE`/`MAX_SIZE`/`MAX_TOTAL`,
`FUNDING_ARB_COST_FRAC`, `FUNDING_ARB_ROLLUP_HOURS`, `FUNDING_ARB_NOTIFY_PER_TRADE`,
`FUNDING_ARB_ENABLED` (master switch for all arms),
`FUNDING_ARB_AGGRESSIVE_ENABLED` (default **0** — the Binance/Bybit "aggressive"
arm is FANTASY/not capturable for a US account and is QUARANTINED by default; set
1 to restore it as a research baseline),
`FUNDING_ARB_EXIT_CONFIRM_HOURS` (default **2.0** — a soft exit, funding_flipped or
apy_decayed, must hold continuously this long before closing, so one noisy snapshot
can't churn a position into a costly round-trip; 0 = fire on first snapshot; also a
per-arm constructor arg `exit_confirm_hours`). **Spot-borrow carry (short-spot legs, i.e. negative-funding
SHORT_SPOT_LONG_PERP trades — only the aggressive arm takes these):**
`FUNDING_ARB_BORROW_APY_MAJOR` (default 10), `FUNDING_ARB_BORROW_APY_ALT` (default 50);
set both 0 to restore the old borrow-free optimistic baseline. Without this the sim
charged only a flat entry cost and no carry, inflating the aggressive arm's microcap
shorts. **Kraken arm (aggressive maker-only config):**
`FUNDING_ARB_KRAKEN_COST_FRAC` (default 0.0054, maker-only),
`FUNDING_ARB_KRAKEN_MAX_BREAKEVEN_CYCLES` (persistence gate, default 6),
`FUNDING_ARB_KRAKEN_MAX_APY` (cap, default 300),
`FUNDING_ARB_KRAKEN_ALLOC` (all-in size per trade, default 100; arm is
`max_positions=1`), `FUNDING_ARB_KRAKEN_SYMBOLS` (base-symbol whitelist, default
= MAJOR_SYMBOLS; restricts the arm to liquid Kraken-majors so it stops chasing
microcaps), `FUNDING_ARB_KRAKEN_MIN_PERSISTENCE_CYCLES` (persistence
gate, default 2; 0 disables), `FUNDING_ARB_KRAKEN_MAX_FLIPS` (serial-flipper
blacklist, default 6). Funding-history tracker (`arbitrage/funding_history.py`,
`data/funding_history.json`) tuned via `FUNDING_HISTORY_RETENTION_DAYS`/
`_SAMPLE_MIN`/`_MAX_GAP_HOURS`/`_SAVE_SEC`. See memory `funding_arb_kraken_bleed`.

## Session-filter env knobs (no code change)
Time-of-day gate (`src/session_filter.py`): `SESSION_MIN_SAMPLES` (default 20 — below this a
session is NEUTRAL/fail-open), `SESSION_WINRATE_FLOOR` (default 0.40 — Wilson lower-bound
floor; a window is only UNFAVORABLE when it BOTH loses money on average AND its win-rate LB
sits below this). `SESSION_FILTER_HARD=1` flips the gate from soft (measure-first: tag-only
in `swing_paper.py`, down-score in `entry_checklist.py`) to a hard veto — do this only after
the proof scorecard's "by session verdict" attribution confirms FAVORABLE windows out-earn
UNFAVORABLE ones. The gate reads the realised record only (`data/trade_journal.csv` +
`data/swing_paper_state.json`) — it never fabricates an edge.

## Risk controls (kill switch + per-arm loss caps)
**Master kill switch** (`src/kill_switch.py`): halts ALL new entries (directional +
every funding arm) while exits keep running. Two triggers — env `BOT_KILL_SWITCH=1`
or the flag file `data/KILL_SWITCH` (live-toggleable: `ssh … "touch
/opt/crypto-bot/data/KILL_SWITCH"` to stop, `rm` to resume; no restart). Fails OPEN.
**Per-arm funding loss cap** (`FundingArbPaperSim.max_drawdown_usd`): an arm halts new
entries once its cumulative net ≤ -cap (alert once on engage + on resume). Defaults:
`FUNDING_ARB_KRAKEN_MAX_DRAWDOWN` 25 (lowered from 40 on 2026-06-13 to retire the
arm: realized net ~-$28 trips this immediately → no new entries; negative EV +
funding-arb dead in the fear regime; re-arm by raising it), `FUNDING_ARB_MAJORS_MAX_DRAWDOWN` 25,
`FUNDING_ARB_MAX_DRAWDOWN` 0 (aggressive/fantasy baseline left uncapped); 0 disables.
**Global funding cap** `FUNDING_ARB_GLOBAL_MAX_DRAWDOWN` (default 0/off): when the 3
arms' combined net breaches it, the merge loop engages the master kill. The directional
arm also honors the kill (funnel `skip:killed`). See memory `attribution_ledger` /
`risk_controls`.

## Swing cadence env knobs (no code change)
`swing_paper.py` now CAPS new entries toward "a few good trades, day and night" — it never
forces a trade (flat sessions take 0). `SWING_MAX_TRADES_DAY` (default 3) per day-window
(EU+US, 8-23 UTC), `SWING_MAX_TRADES_NIGHT` (default 3) per night-window (Asia, 0-7 UTC),
`SWING_MAX_OPEN_POSITIONS` (default 7; 7×$62.50≈$440 of the $500 paper bankroll). When more
setups qualify on one bar-close than the budget allows, conviction ranking (`_conviction`:
20-bar ROC + distance above EMA50) keeps the strongest. The locked 4h-majors entry edge
(`src/swing_strategy.py`) is untouched. **Universe stays the proven 6 majors** — the
2026-06-08 sweep found broadening bled, so `deploy/swing_cron.txt` keeps
`SWING_SYMBOLS=BTC,ETH,SOL,LTC,BCH,XRP`; a wider set must forward-prove on a SEPARATE
ledger first (`SWING_STATE_FILE` override + the commented measure-first cron line). See
memory `swing-frequency-1h-band`.

## Proof bar (proof_scorecard.py)
Pre-registered bar unchanged (executable & n≥30 & expectancy>0 & correlation-adjusted t>2)
but now **selection-bias aware**: with k arms judged at once it also requires a Šidák
family-wise t-bar (`_family_t_bar(k)`), so "best of k strategies cleared t>2" no longer
counts as proof. An arm clearing the single bar but not the family bar reads `PROVEN
(single) — NOT family-wise robust`; only `PROVEN ✓` counts in the final verdict. k=1
reproduces the original bar exactly. The weekly Telegram report (`scripts/weekly_report.py`)
now surfaces both the per-arm verdicts (§7) and the session-edge table (§8).

## AI brain context (what it's fed — no code change)
The Claude-Opus discretionary brain (`src/trade_brain.py`, runner `brain_paper.py`) is
prompt/context-based — it "learns" from what we feed it each run, not gradient training. It is
fed only **curated repo truth + its own record**, scoped to six understanding-targets (cost,
this system's unproven epistemic state, regime+persistence, correlation/BTC-beta, self-
calibration, the graveyard). Surfaces: (1) a durable **curated knowledge base**
(`src/brain_knowledge.py`) injected as a 2nd cached system block (`BRAIN_KNOWLEDGE=1`); (2)
**measure-first desk blocks** (`src/desk_blocks.py`): `proof_status` (system-wide proof verdicts
→ epistemic humility), `session_edge`, `swing_attribution` — toggles `BRAIN_BLOCK_PROOF`/
`_SESSION`/`_SWING` (default 1), each fail-safe→absent; (3) an enriched **memory** loop
(`build_memory`: win-rate by coin & action + worst-trade post-mortems with the losing thesis).
All REFINE/RISK-CHECK only — never new triggers; the brain's risk controls (drawdown stop,
F&G short-veto, correlation cap) are untouched. **ML models NOT trained:** XGBoost/calibrator
only train on the shelved negative-EV directional journal (empty OFI/lead-lag features); training
them would teach the losing scalper's patterns. Revisit only after retargeting ML to a strategy
that actually trades + full-feature labeled data. See memory `trade_brain`.

## Telegram
Buy/sell/error + funding-arb alerts → chat ID `7553694317`.

## Status
- Paper mode. Scalper idle in low volatility (by design). Funding arms active.
- `archive/` holds removed one-off scripts (kept for reference, not on any path).
```

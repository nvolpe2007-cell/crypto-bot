# Crypto Bot — Claude Context

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
src/entry_checklist.py      # hard/soft entry gates (atr_alive lives here)
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

## Telegram
Buy/sell/error + funding-arb alerts → chat ID `7553694317`.

## Status
- Paper mode. Scalper idle in low volatility (by design). Funding arms active.
- `archive/` holds removed one-off scripts (kept for reference, not on any path).
```

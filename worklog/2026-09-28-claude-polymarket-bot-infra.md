# 2026-09-28 — Claude (computer) — polymarket-bot-infra

**Branch:** `claude/vigilant-wozniak-ycaxze`

Owner asked for a Polymarket bot (BTC up/down style markets). Chose (via
AskUserQuestion): paper trading first, new folder in this repo, infra
wired up before any real signal.

New `polymarket_bot/` package:
- `client.py` — read-only Polymarket Gamma/CLOB HTTP wrapper (market search,
  midpoint price, order book). No wallet, no API key, no order-placement
  code anywhere in the module.
- `paper_sim.py` — paper position open/settle against real Polymarket prices;
  writes `data/polymarket_paper_state.json` in this repo's standard arm-state
  shape (`equity`/`start_equity`/`open`/`closed`) so `src/dashboard_data.py`
  will auto-discover it once it's actually run.
- `signal.py` — placeholder, always `HOLD` (owner explicitly chose infra-first
  over a real signal this pass). Left a note that short-duration binary
  markets have even higher effective turnover than the directional scalper
  that already failed this repo's turnover/cost screen — a real signal here
  should clear the same bar, not get a pass because it's a different venue.
- `README.md` — states plainly what this is not: not connected to a real
  wallet, cannot place a real order, not wired into `run_all_bots.py` or cron.

Not in either lane (new, independent subsystem) — no existing directional or
brain/risk files touched. +6 tests (`tests/test_polymarket_paper_sim.py`),
covering settlement PnL for winning/losing YES and NO positions, condition_id
isolation, and state save/load round-trip. Full suite: 3590 passed (excluding
2 modules that fail to collect in this environment for an unrelated reason —
`tests/test_dashboard.py`/`tests/test_bot_main.py` need `fastapi`, not
installed here; not touched by this change).

**Next steps, explicitly not done here:** a real signal with a pre-registered
backtest, a runner + cron entry, and — only after the paper arm proves
itself — live execution via a funded wallet, which needs separate explicit
owner approval per this repo's own principles.

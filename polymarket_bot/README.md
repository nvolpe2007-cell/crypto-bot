# polymarket_bot

Infra scaffold for a Polymarket paper-trading bot (BTC up/down style markets).
Built infra-first at the owner's request — **no live signal, no real trading.**

## Status
- `client.py` — read-only Polymarket Gamma/CLOB API wrapper (market search, midpoint
  price, order book). No wallet, no API key, no order placement anywhere in this module.
- `paper_sim.py` — paper position open/settle, writes `data/polymarket_paper_state.json`
  in this repo's standard arm-state shape (`equity`/`start_equity`/`open`/`closed`), so
  `src/dashboard_data.py` picks it up automatically.
- `signal.py` — placeholder, always `HOLD`. Real signal work is a separate, deliberate
  next step — see the cost/turnover discipline in `CLAUDE.md` before trusting one.

## What this is NOT
- Not connected to a real wallet or private key.
- Cannot place a real order — there is no signing/execution path implemented.
- Not wired into `run_all_bots.py` or any cron yet.

## Next steps (not done yet)
1. A real signal (with a pre-registered backtest + cost/turnover screen, same discipline
   as every other arm in this repo).
2. A runner script + cron entry once the paper arm has run long enough to judge.
3. Live execution (`py-clob-client`, funded Polygon wallet) is a separate, explicit,
   owner-approved step for later — not before the paper arm proves itself.

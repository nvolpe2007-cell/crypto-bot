# polymarket_bot

Scaffold for a Polymarket bot (BTC up/down style markets): paper-trading
infra, plus a human-in-the-loop live order tool. **No automated signal drives
real money — every real order is manually reviewed and typed-confirmed by
you, every time.**

## Status
- `client.py` — read-only Polymarket Gamma/CLOB API wrapper (market search, midpoint
  price, order book). No wallet, no API key, no order placement anywhere in this module.
- `paper_sim.py` — paper position open/settle, writes `data/polymarket_paper_state.json`
  in this repo's standard arm-state shape (`equity`/`start_equity`/`open`/`closed`), so
  `src/dashboard_data.py` picks it up automatically.
- `signal.py` — placeholder, always `HOLD`. No code path here ever reads this to place a
  real order — it only exists for a future paper-sim runner. Real signal work is a
  separate, deliberate next step — see the cost/turnover discipline in `CLAUDE.md`
  before trusting one.
- `live_execution.py` — a manual CLI for placing REAL Polymarket orders. Dry-run by
  default; submitting requires `--live`, your own `POLYMARKET_PRIVATE_KEY` /
  `POLYMARKET_FUNDER_ADDRESS` env vars (set by you, on your own machine — never shared
  with or stored by any agent), and typing `CONFIRM` at an interactive prompt naming the
  exact order. There is no non-interactive submit flag. See the module docstring for
  full usage. Run this yourself; it is not wired into any automation.

## What this is NOT
- Not an automated trading bot — nothing here decides to trade or submits an order on
  its own. `signal.py` is inert; `live_execution.py` requires a human to choose the
  market/side/size and type CONFIRM every single time.
- Not wired into `run_all_bots.py` or any cron.
- Does not use, store, or transmit your private key anywhere but Polymarket's own API,
  and only when you run `live_execution.py --live` yourself.

## Next steps (not done yet)
1. A real signal (with a pre-registered backtest + cost/turnover screen, same discipline
   as every other arm in this repo) — to inform paper trading and, eventually, what you
   choose to act on manually. It would not, by itself, ever auto-submit a live order.
2. A runner script + cron entry for the paper arm once there's a real signal to test.

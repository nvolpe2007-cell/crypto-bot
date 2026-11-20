# 2026-10-15 — Claude (computer) — dashboard-greg-paper-book

**Branch:** `feat/dashboard-greg-paper-book`

Owner asked to see Greg's paper book on the live dashboard. Greg (the chief-of-staff Telegram
bot, `D:\chief-of-staff`) keeps a $1,000 paper book on live Kraken prices via its own
`tools/paper-trade.py`, which writes `/home/agent/chief-of-staff/paper/` on agent-vps. It is not a
crypto-bot arm and is not judged by `proof_scorecard`.

`scripts/dashboard_server.py`:
- `_load_greg_book()` reads `positions.json` + `trades.jsonl` **read-only** (dir from
  `DASHBOARD_GREG_BOOK_DIR`, default the path above). Missing book → panel hidden; a torn ledger
  line is skipped. Unrealized P&L uses `state.json` prices when present.
- `/api/state` gains `greg_book`; new "Greg's Paper Book · paper money only" panel (equity, P&L,
  unrealized, win rate with a "too few to judge" note under 30 trades, avg R, open positions
  with stop/target/thesis, last 20 closes). Free text (thesis, reason) goes through `esc()`.
- No change to the lev_perp view or to anything the bot writes.

Tests: `tests/test_dashboard_greg_book.py` (5) + existing `test_dashboard_data.py` → 28 passed.
Ran the server locally against a sample book; panel renders, no console errors, `<b>` in a
thesis renders as text.

Also on 2026-10-15 (no repo change): `crypto-dashboard.service` had never been installed on
agent-vps after the 2026-09-17 move; installed from `deploy/crypto-dashboard.service` and started
(owner approved). Loopback only, port 8787.

# Meme radar — early callouts, and the ledger that checks them

Three scripts, one shared ledger. The radar finds tokens, the ingest reads what
human channels are calling, and the scorecard tells you whether either is worth
acting on.

```
scripts/meme_radar.py       -> Telegram callouts + ledger      (source="radar")
scripts/telegram_ingest.py  -> channel calls     + ledger      (source="tg:<chan>")
scripts/callout_scorecard.py-> reads the ledger, scores both
scripts/callout_ledger.py   -> the shared store (data/callout_ledger.json)
```

All four build on the existing `screener_sources.py` / `screener_risk.py` risk
layer, so every callout is priced through the same cost model the rest of the
repo uses. Stdlib only, except Telethon for the optional ingest.

---

## Read this before you use it

**The bot cannot trade any of these.** These tokens live on Solana/Base/ETH
AMMs; the crypto-bot trades Kraken spot and Bybit-signalled majors. Every
callout is a manual trade in your own wallet. `executable=False`, same as the
arb arm.

**A callout is not a buy signal.** It means a token cleared liquidity and
round-trip-cost gates *and* something measurably changed. Nothing here reads a
contract, checks whether LP is locked, or looks at holder concentration.
Honeypots, live mint authority, and freeze authority are all invisible to it.

**The base rate is brutal.** Measured live on 2026-08-08: **0 of 12** tokens
from the DexScreener `new` feed cleared the gates — all were sub-48h, under
$50k liquidity, or pump.fun bonding curves with no modellable exit. The
`boosted` feed did better at 2 of 20. Most runs will alert on nothing, and that
is the tool working.

**Net is the only number that counts.** Every result is reported after the
round-trip cost of actually taking it. A +8% move on a pool needing 12% to
break even is a loss and is scored as one. This repo has killed three candidate
edges on exactly that gap (arb: 0/34 with fees 12x gross; funding-decay: null;
lev_perp: a symbol-clustering artifact). Meme calls are not going to be the
exception because you want them to be.

---

## What "early" actually means

The screener's age gate is 48h, because most rugs and sniper dumps resolve
inside two days. The radar does **not** quietly lower it. Two tiers:

| Tier | Age | How you get it |
|---|---|---|
| `SURVIVED` | ≥ 48h | default |
| `EARLY` | below 48h, down to your floor | only with `--early-window H` |

`EARLY` alerts carry an explicit warning line saying the pair is inside the
window where the base rate is worst. Running with `--early-window` is a
decision to trade there. The tool will do it and tell you every time.

## The delta signal

A snapshot cannot express "early" — change can. The radar keeps a rolling
per-token history (`data/meme_radar_state.json`, last 24 polls, 72h TTL) and
fires when a token clears the risk gates **and**:

- **liquidity growth ≥ +25%** across the window — real money entering the pool,
  the least fakeable signal available, because inflating it costs the deployer
  capital that stays at risk; **or**
- **trade-rate acceleration ≥ 1.6x**, recent half of the window vs older half;

**and** buys are ≥ 55% of new flow. Without that last condition, a token being
dumped looks identical to a token being bought — acceleration alone would call
out the exit.

Two guards on top: never alert on a token's first sighting (nothing to compare
to), and never trust a window under 12 minutes (that measures noise). This is
why the radar cron runs every 5 minutes — stretch it past ~10 and the
acceleration measurement loses its resolution.

## Scoring

Every alert is written to the ledger and marked forward at **1h / 4h / 24h /
72h**. A horizon whose window passes with no reachable price is recorded as
**-100%**, not dropped — a token you cannot exit is a total loss, and omitting
those is precisely how a hit rate gets manufactured.

`n < 20` at any horizon is a story, not a statistic. The scorecard says so
inline, per horizon and per caller.

---

## Setup

Nothing to install for the radar and scorecard. They use the Telegram bot token
already in `.env`.

```bash
# see what it would send, without sending anything
python scripts/meme_radar.py --ticket 100 --source boosted --dry-run --verbose

# same, but also allow pairs younger than the 48h rug window
python scripts/meme_radar.py --ticket 100 --early-window 6 --dry-run

# results, once alerts have reached their horizons
python scripts/callout_scorecard.py --callers
python scripts/callout_scorecard.py --list --limit 20
```

Deploy: append the entries in `deploy/meme_radar_cron.txt` to the VPS crontab
next to the existing `trade_close_notifier` line.

### Optional: reading alpha channels

This is the part that answers *which channels are actually early and which are
using you as exit liquidity*. It needs a user session — the bot token in `.env`
can only send.

1. Get `api_id` / `api_hash` from https://my.telegram.org → API development tools.
2. Add to `.env`:
   ```
   TELEGRAM_API_ID=...
   TELEGRAM_API_HASH=...
   TELEGRAM_INGEST_CHANNELS=chan_one,chan_two
   ```
3. `pip install telethon`
4. **Log in yourself** — it prompts for your phone, an SMS code, and your 2FA
   password. Run it at a real terminal; none of those should pass through an
   agent:
   ```
   python scripts/telegram_ingest.py --login
   python scripts/telegram_ingest.py --list-channels
   ```
5. Then uncomment the ingest line in the cron file.

The session lands at `data/tg_ingest.session`. **Treat it like a password** —
anyone holding it reads your Telegram as you, no 2FA prompt. It is gitignored
twice over.

The ingest is read-only by construction: it only ever calls `iter_messages`. It
never sends, joins, leaves, reacts, or marks anything read.

Calls are timestamped from **when the message was posted**, not when cron read
it — otherwise a channel would look better the later you polled.

---

## How to decide whether to keep it

Let it run two weeks without trading a single callout. Then:

```bash
python scripts/callout_scorecard.py --horizon 24 --callers
```

Look at **TOTAL P&L**, not the hit rate. The classic meme distribution is a
positive median with a negative total: most calls tick up slightly, a few go to
zero, and the zeros dominate. If the dollar total at your ticket size is
negative across n≥20, the radar has no edge and the honest move is to shut off
the alerts and keep the ledger running — the same treatment the arb arm got.

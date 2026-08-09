# Meme cohort — the dataset that makes the studies possible

`scripts/meme_cohort.py` registers every new Solana pool GeckoTerminal reports,
snapshots each one forward on an age-tiered schedule, and **keeps every member
of the cohort permanently — including the ones that rug, drain, or vanish.**

It is a pure data layer. No signals, no scores, no gates, no backtest. Studies
read these files; they do not live here. Same discipline as
`scripts/onchain_data.py`.

Free tier only — GeckoTerminal's public API, no key, stdlib only.

---

## Why it exists

There is no purchasable history of **dead** meme coins. DexScreener and
GeckoTerminal both answer *"what exists now"* — a token that rugged and got
delisted is simply absent, with no tombstone.

So a study built on whatever the API hands you today is a study of **survivors**,
and every base rate it produces comes out flattering and wrong. The only fix is
to write down the birth cohort as it happens and follow every member to its end.
That sample cannot be bought back later. It can only be collected forward, which
is why this runs on a cron from the day it's installed.

## The four anti-survivorship rules

These are the whole value of the thing, and each failure mode is silent — a
corrupted cohort still produces clean-looking numbers, and you'd only find out
months later in a study you can't re-run.

1. **Pools are never deleted.** Terminal states are recorded, not removed.
2. **A failed API call is never a death.** Misses count only when the request
   *succeeded* and the pool was absent. Otherwise one rate-limit window reads as
   a mass extinction. Death needs 3 consecutive confirmed absences.
3. **Death is recorded as an interval** (`last_alive_ms`, `first_miss_ms`),
   because that's what's actually known. A single death timestamp would be a
   guess dressed as a measurement.
4. **Discovery lag is stored per pool**, so a study needing true t=0 coverage can
   filter on it instead of silently mixing in pools we caught late.

All four are locked down by `tests/test_meme_cohort.py` (49 tests).

## What gets captured

Per snapshot, 42 fields — raw only, no derived ratios:

- `price_usd`, `reserve_usd` (liquidity), `fdv_usd`, `mcap_usd`
- across six windows (`m5 m15 m30 h1 h6 h24`): `buys`, `sells`, **`buyers`**,
  **`sellers`**, `vol`, `chg`

That `buyers`/`sellers` pair is unique-wallet counts, distinct from trade
counts, and it's a **free wash-trading proxy** — 400 buys from 3 buyers is a
script, not a market. I'd assumed this needed trade-level data; it doesn't.
Recorded raw and interpreted nowhere.

## Measured behaviour (2026-08-09)

| | |
|---|---|
| Discovery lag | median **98s** from pool birth, p90 194s |
| Solana launch rate via API | ~4 pools/min |
| Rate limit | 429 after ~3–4 unspaced calls; self-throttles to 20/min |
| Batch endpoint | `pools/multi` takes 30 addresses per call |
| OHLCV depth | ~180 bars per request, paginate backwards for more |

## Coverage is measured, not assumed

Solana launches more pools than `new_pools` returns per call, so this collects a
**sample, not a census**. The honest question isn't "did we get everything" — we
didn't — but "do we know how much we missed".

The test is exact. `new_pools` is newest-first, so if the oldest pool on the
deepest page is one we already knew, our polls overlap and nothing was missed. If
every pool on that page is new, the window **overflowed**.

This matters because overflow isn't random — it happens during launch-rate
spikes, so an overflowing collector systematically under-samples busy periods,
exactly the periods a study of manias cares about. `status` reports the overflow
rate so any result can be qualified by it.

## Age tiers

Young pools carry both the price action and the mortality, so they're sampled
hard and old ones cheaply:

| Age | Refreshed |
|---|---|
| < 6h | every 15 min |
| < 48h | every 60 min |
| < 7d | every 6h |
| ≥ 7d | daily |

When the call budget binds, selection is **youngest-first** — the oldest pools
get skipped, never the newborns. Nothing leaves the registry; it just gets
sampled less often.

## What the free tier cannot see

Holder concentration, LP lock/burn, mint authority, freeze authority, deployer
wallet history, and block-0 sniper bundles. Those need an RPC with enhanced APIs
(Helius/QuickNode class, ~$50–100/mo). **Any conclusion from this cohort alone is
a conclusion about price, liquidity and flow only** — and that's the half of the
problem everyone else can already see.

The RPC only becomes worth buying once the cohort exists and there's something to
join it against. That's the sequencing.

---

## Usage

```bash
python scripts/meme_cohort.py tick --pages 5 --budget 16   # cron entry
python scripts/meme_cohort.py status                       # coverage + mortality
python scripts/meme_cohort.py backfill-ohlcv --budget 12
```

Deploy: the entries in `deploy/meme_cohort_cron.txt`.

### Storage

```
data/meme_cohort/registry.json              one record per pool ever seen
data/meme_cohort/snapshots-YYYY-MM-DD.jsonl append-only time series
data/meme_cohort/ohlcv/<pool>.json          candle cache
```

~600 bytes per snapshot line; low GBs per year. Daily files are safe to gzip
once complete.

---

## Before running a study on this

Two weeks of collection, then look at raw base rates **before** forming any
hypothesis. The things to establish first:

- What fraction of pools are dead at 24h / 7d / 30d?
- What does the return distribution actually look like — and how far apart are
  median and mean? (Expect violently far. Fat tails are the defining feature.)
- What fraction of reported volume survives a `buys`-vs-`buyers` sanity check?
- What's the overflow rate, i.e. how much of the market are we actually seeing?

Then **one pre-registered hypothesis**, not a fan-out — the reason is the same as
everywhere else in this repo: hunting many hypotheses at once raises the Šidák
bar until nothing can clear it.

The good news is that sample size stops being the binding constraint here for
the first time. Where the lev_perp work needed ~25 years of data to clear a DSR
bar, this hits n=1,000 in weeks. The constraints move to fat tails, wash trading,
and the cost wall — which is why the collector is built to measure all three
from day one.

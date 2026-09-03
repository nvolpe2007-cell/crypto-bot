# Order-flow capture schema — design draft

**Status:** design only. No recorder is implemented by this document or the PR carrying it.
**Date:** 2026-09-03
**Related:** PR #110 (`src/orderflow_indicator.py`), `research/hypothesis_registry.yaml`
(`scalper-microstructure-ofi-v2` 2026-09-03 evidence item)

---

## Why this exists

Three separate hypotheses are blocked on the same missing thing, and none of them is
blocked on a missing *idea*:

| Hypothesis | Status | Blocked by |
|---|---|---|
| `scalper-microstructure-ofi-v2` | paper-only | no persisted tape or book — nothing to score OOS |
| `altperp-flush-long` | paper-only | no liquidation history; the **only** ledger entry with zero measurement |
| `liq-cascade-continuation` | sit-out | liquidations *proxied* by return-z + volume-z; its own disposition says "start recording the real forceOrder feed **now**" |

Every order-flow number this bot has ever computed was built in memory and discarded.
The live components (`order_flow.OrderFlowImbalance`, `ofi_v2.OFICalculatorV2`,
`cvd_tracker.*`, `orderflow_ws.obi_from_book`, `vpin_monitor.VPINMonitor`) are all
stateful and return the latest scalar; none writes anything down.

**The asymmetry that makes this urgent:** derived features can always be recomputed from
raw capture, but raw capture cannot be recovered retroactively. A window not recorded in
September is gone. So the schema decision is one-shot in a way the analysis is not.

## What must be captured, and why each field is load-bearing

The field list below is derived from what the indicators actually consume. Anything
dropped here silently makes the corresponding indicator untestable on the captured data.

### 1. Book snapshots — **depth to N=5, not top-of-book**

```
ts_local_ns     int64     receipt time, single local monotonic-backed clock
ts_exchange_ms  int64     exchange-stamped time, kept SEPARATE (see Clocks)
symbol          string
bid_px[5]       float64   best-first
bid_sz[5]       float64
ask_px[5]       float64
ask_sz[5]       float64
seq             int64     exchange sequence/checksum id where available
is_snapshot     bool      true = full refresh, false = post-update state
```

Top-of-book alone is the tempting cheap option and it is the trap. `multi_level_ofi`
needs levels 2–5, and depth cannot be reconstructed from a top-of-book log afterwards.
The anti-spoof argument is the reason to want it: a single fake order at level 1 is
partly offset by genuine depth behind it, so moving a depth-weighted signal requires
faking the whole stack.

Record the **post-update book state**, not raw deltas. Deltas are smaller but replaying
them correctly requires bug-free sequence handling forever; a state log is
self-describing and survives a gap.

### 2. Trades

```
ts_local_ns     int64
ts_exchange_ms  int64
symbol          string
price           float64
qty             float64
taker_side      string    'buy' | 'sell' — from the venue, NOT inferred
trade_id        string
```

Kraken's WS supplies the taker side directly, so bulk-volume classification
(Easley/López de Prado's Φ(Z) method) is **not needed here** — it is only required for a
venue that withholds the aggressor flag. Record the venue's flag verbatim;
`signed_volume_from_ticks` already prefers it and falls back to the tick rule only for
gaps.

### 3. Open interest — **the field I would have omitted**

```
ts_local_ns     int64
symbol          string
open_interest   float64   contracts or base units — record the unit, do not normalise
source          string    venue + endpoint, since OI definitions differ per venue
```

This is the discriminator for `flow_oi_regime`. Without it, forced liquidation and fresh
conviction are the same footprint in flow — a burst of one-sided taker volume — with
opposite implications. It is the field that makes `altperp-flush-long` measurable without
paid CoinGlass data, and the field that would upgrade `liq-cascade-continuation` from a
return-z/volume-z proxy to the real thing.

**Sampling rate is the live constraint.** OI is REST-polled, so it cannot match
event-time book updates. Poll on a fixed cadence matched to the *coarsest* OFI window you
intend to classify on (5-minute windows → poll every 5–15s is ample; sub-minute
classification would need a different approach and is out of scope for v1). Do not poll
faster than the rate limit budget allows — the DEX side is already Multicall3-batching
against RPC limits.

## Clocks

Use **one local receipt clock** for all alignment, and keep exchange timestamps as a
separate column, never as the join key. Cross-venue timestamp skew is the classic silent
killer of lead-lag estimation: it manufactures apparent lead/lag that is pure clock drift
and looks exactly like a real signal. Store `ts_local_ns` from a monotonic source anchored
once to wall time, so NTP steps cannot make the log non-monotonic mid-session.

## Format, volume, retention

**Format:** Parquet + zstd, one file per (symbol, hour), with an NDJSON+gzip fallback if
the writer proves fiddly on the VPS. Columnar matters here because the analysis is almost
always "one or two columns over a long window", and the book columns are highly
compressible (prices barely move between adjacent snapshots).

**Volume, rough order of magnitude** — 6 symbols, an assumed ~20 book updates/sec/symbol
in active tape, 5 levels both sides:

| Encoding | Per day (est.) |
|---|---|
| NDJSON raw | ~4–5 GB |
| NDJSON + gzip | ~0.4–0.5 GB |
| Parquet + zstd | ~0.05–0.15 GB |

These are estimates, not measurements — real rates vary by an order of magnitude with
volatility, and the first week of capture should be treated as a sizing experiment rather
than a commitment.

**Retention:** keep raw for a rolling window (30 days suggested), and keep *derived*
features forever — they are tiny. Derived can always be recomputed from raw while raw
exists; nothing can recompute raw. Roll up before deleting.

## What NOT to capture in v1

Deliberately excluded to keep the first version shippable and its disk cost bounded:
full book depth beyond 5 levels; order add/cancel event streams (as opposed to resulting
book state); mempool data (different system, different adversarial model); anything from a
venue not already connected.

## Open decisions — owner input needed

1. **Disk budget on the Hetzner VPS.** Everything above is sized against an unknown. Needs
   a `df -h` before committing to a retention window.
2. **Venue.** The indicator brief was written against Bybit. Bybit/Binance are geo-blocked
   for this account, though the existing altperp arm already runs Bybit-signals /
   Kraken-execution, so *reading* Bybit is established practice. But OI-conditioned
   entries signalled on Bybit and executed on Kraken carry a cross-venue basis that should
   be measured before it is trusted — the aggressive funding arm was quarantined for
   exactly that class of unvalidated assumption. Recording Kraken only is the conservative
   v1; recording both is the informative one.
3. **Symbol universe.** The proven-6 majors, or wider? Wider costs disk linearly and the
   swing work already found broadening bled.
4. **Does the recorder run in the bot process or beside it?** Beside it is safer — a
   recorder bug must never be able to take the trading loop down. `supervised()` exists for
   this, but a separate process is stronger isolation.

## What this does NOT claim

That any of this will produce an edge. The repo's record on intraday order flow is
uniformly negative at the cost wall, and this document does not dispute it. The claim is
narrower and, I think, defensible: **three hypotheses currently sit unmeasurable, and one
capture layer makes all three measurable.** Whether the answers are positive is exactly
what is not yet known — which is the point.

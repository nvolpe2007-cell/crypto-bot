# Micro-maker arm meets a real tape — and the gate is not selective

**Agent:** claude-computer · **Branch:** `feat/micro-maker-forward-arm`
**Lane:** directional · **PR:** [#120](https://github.com/nvolpe2007-cell/crypto-bot/pull/120)

## What I set out to do

Open the PR the previous session left uncreated, then work step 1 of its own
"not done" list: run `--dry-run` and check the gate fires at a sane rate —
"neither never nor constantly."

## The blocker was not a sandbox limitation

Last session recorded that Kraken WS "could not connect from this machine
(sandbox DNS)" and deferred all live testing to the VPS. That diagnosis was
wrong, and it cost the arm a session of real data.

`socket.gethostbyname('ws.kraken.com')` resolves fine here. Only aiohttp failed.
The cause: aiohttp selects `AsyncResolver` (c-ares) whenever aiodns is installed,
and that resolver speaks UDP to the nameservers directly — bypassing
`/etc/hosts`, systemd-resolved, VPN split-DNS and container DNS policy. A/B test:

| resolver | `GET api.kraken.com/0/public/Time` |
|---|---|
| `AsyncResolver` (aiohttp default) | `ClientConnectorDNSError: Could not contact DNS servers` |
| `ThreadedResolver` (getaddrinfo) | `HTTP 200` |

This affects **every Kraken feed in the process**, not just this arm, and the
reconnect backoff faithfully retried a lookup that could never succeed. Fixed in
`40226d5`: all five `ClientSession` sites route through `_new_session()`, which
pins `ThreadedResolver`. `KRAKEN_WS_ASYNC_DNS=1` restores the old behaviour.

The VPS is separately unreachable right now (port 22 times out), so it could not
have unblocked this either.

## First real ticks through the code path

20-minute `--dry-run`, BTC/USD + ETH/USD, live Kraken v2 book + trade feeds,
zero reconnects. Connected on the first attempt.

## The finding: the gate is close to non-selective

- **Gate true on 378 of ~2400 tick evaluations — 15.8% of the time.**
- Collapsing consecutive true-ticks into episodes: **40 entry opportunities in
  20 minutes = ~120/hour** (BTC 23, ETH 17; median episode 6-8s).
- OBI at fire: min 0.581, median 0.664, max 0.910 against a 0.58 threshold.
- CVD slope at fire: min 0.0000, median 0.0043, **max 9169.9**.

Two things follow, and neither is encouraging:

1. **The gate is not the binding constraint — the 120s cooldown is.** With
   `max_positions=1` the arm tops out near 30 entries/hour, so in live paper the
   cooldown timer would be choosing the trades, not the signal. A gate that is
   true 16% of the time on a strategy needing 0.65% moves to clear a 0.50%
   round trip is not identifying an edge; it is describing a common state of the
   book.
2. **`MICRO_CVD_SLOPE_MIN=0.0` is barely a filter, and its units are unbounded.**
   Slope is raw signed volume, so it ranges over six orders of magnitude and the
   threshold reduces to "any net buying at all." Comparing it against a fixed
   constant is not meaningful across symbols or volatility regimes.

## What I deliberately did not do

I did not tune OBI/slope upward until the rate looked respectable. Picking
thresholds because they produce a pleasing trade count on the 20 minutes of tape
I happened to observe is exactly the overfitting the proof bar exists to catch,
and `win_rate_trap` is the standing rule against it. The thresholds were first
guesses; they have now met real data and been shown non-selective. Replacing
them needs a pre-registered rule fit on tape that is not the tape it is judged
on.

## Where this leaves the arm

The wiring is proven; the signal is not, and the 90-day clock should **not**
start yet. Step 2 of the original list (watch the fill rate) is now the live
question — but it is worth answering only after the gate stops firing on a sixth
of all ticks, because a non-selective gate makes the fill rate a statement about
the tape rather than about the strategy.

Honest read: this arm currently has an entry condition that fires constantly and
a cost floor that demands rarity. Those two facts point in opposite directions.

## Verification

- Full suite: **3590 passed, 5 failed** — the known Windows-only pre-existing
  failures (dashboard/exchange/notifications), unchanged by this work.
- `tests/conftest.py` stubs `aiohttp` as a bare module; it needed `TCPConnector`
  and `ThreadedResolver` added or every session site dies on `AttributeError`.

Related: `scalper-microstructure-ofi-v2` · `tick-ofi-cvd-standalone` (killed) ·
`btc-intraday-and-leverage` (sit-out) · `win_rate_trap`

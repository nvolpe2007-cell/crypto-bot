#!/usr/bin/env python3
"""Meme cohort collector — capture every new Solana pool at birth, keep the dead.

WHAT THIS IS
------------
A pure DATA LAYER. It registers every new pool GeckoTerminal reports, snapshots
each one forward on an age-tiered schedule, and retains every member of the
cohort permanently -- including the ones that rug, drain, or vanish.

There is deliberately NO signal construction, NO feature engineering, NO
scoring, NO gating and NO backtest anywhere in this module, exactly as in
`scripts/onchain_data.py`. It answers one question and no others:

    "What was true about this pool, at this timestamp, and is it still alive?"

Anything that ranks, filters, or predicts belongs in a separate script that
reads these files. If a gate creeps in here, the cohort stops being a cohort and
becomes a watchlist, and every base rate computed from it becomes wrong.

WHY CAPTURE-AT-BIRTH IS THE WHOLE POINT
---------------------------------------
There is no purchasable history of DEAD meme coins. DexScreener and
GeckoTerminal both answer "what exists now" -- a token that rugged and got
delisted is simply absent, with no tombstone. So a study built on whatever the
API will hand you today is a study of survivors, and every base rate it produces
comes out flattering and wrong.

The only fix is to write down the full birth cohort as it happens and follow
every member to its end. That sample cannot be bought back later; it can only be
collected forward. That is why this exists and why it runs on a cron from the
day it is installed.

Concretely, the anti-survivorship rules here are:

  * A pool is NEVER deleted from the registry. Terminal states are recorded, not
    removed.
  * A failed HTTP call is NEVER recorded as a death. Misses are only counted
    when the request SUCCEEDED and the pool was absent from the response --
    otherwise a rate limit would read as a mass extinction.
  * Death timing is recorded as an INTERVAL (`last_alive_ms`, `first_miss_ms`),
    because that is what is actually known. A single death timestamp would be a
    guess dressed as a measurement.
  * Discovery lag (birth -> when we first saw it) is stored per pool, so any
    study can exclude pools we caught late instead of silently mixing them in.

FREE TIER, AND WHAT IT COSTS
----------------------------
GeckoTerminal's public API, no key. Measured 2026-08-09: a 429 arrives after
~3-4 unspaced calls, so the throttle here defaults to a conservative ~20
calls/min with exponential backoff. The `pools/multi` endpoint takes up to 30
addresses per call, which is what makes the whole thing viable: 20 calls/min x
30 pools = ~600 pools refreshed per minute of budget.

Age tiering does the rest of the work. Young pools are where both the action and
the mortality are, so they are sampled hard and old ones cheaply:

    age < 6h    -> every 15 min
    age < 48h   -> every 60 min
    age < 7d    -> every 6h
    age >= 7d   -> every 24h

WHAT THE FREE TIER CANNOT SEE
-----------------------------
Holder concentration, LP lock/burn status, mint authority, freeze authority,
deployer wallet history, and block-0 sniper bundles are all invisible here. They
need an RPC with enhanced APIs (Helius/QuickNode class). Any conclusion drawn
from this cohort alone is a conclusion about price, liquidity and flow only.

One partial exception worth knowing: the `transactions` block reports `buyers`
and `sellers` (unique wallets) alongside `buys` and `sells` (trade counts). The
ratio between them is a free wash-trading proxy -- 400 buys from 3 buyers is a
script, not a market. It is recorded raw here and interpreted nowhere.

STORAGE
-------
    data/meme_cohort/registry.json          one record per pool ever seen
    data/meme_cohort/snapshots-YYYY-MM-DD.jsonl   append-only time series
    data/meme_cohort/ohlcv/<pool>.json      optional candle cache

Roughly 600 bytes per snapshot line. Age tiering keeps this in the low GBs per
year, not TBs.

USAGE
-----
    python scripts/meme_cohort.py tick              # cron entry: discover+refresh
    python scripts/meme_cohort.py discover
    python scripts/meme_cohort.py refresh --budget 20
    python scripts/meme_cohort.py status
    python scripts/meme_cohort.py backfill-ohlcv --budget 15
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data" / "meme_cohort"
REGISTRY = DATA_DIR / "registry.json"
OHLCV_DIR = DATA_DIR / "ohlcv"

API = "https://api.geckoterminal.com/api/v2"
NETWORK = "solana"
USER_AGENT = "crypto-bot-cohort/1.0 (read-only research collector)"

# Measured 2026-08-09: 429 after ~3-4 unspaced calls. GeckoTerminal documents 30
# calls/min on the free tier; 20 leaves headroom so a research backfill and the
# cron tick can coexist without either getting throttled into uselessness.
CALLS_PER_MIN = 20
MIN_SPACING_S = 60.0 / CALLS_PER_MIN

MULTI_BATCH = 30          # pools/multi accepts up to 30 addresses
TIMEOUT_S = 25.0
MAX_RETRIES = 3

# Age-tiered refresh cadence: (max_age_hours, refresh_interval_minutes)
TIERS: tuple[tuple[float, float], ...] = (
    (6.0, 15.0),
    (48.0, 60.0),
    (168.0, 360.0),
    (float("inf"), 1440.0),
)

# Consecutive CONFIRMED absences before a pool is called gone. Three spaced
# misses is a lot of independent evidence; one is an API hiccup.
DEATH_MISSES = 3

# Reserve below this is "drained" -- the LP is effectively gone even though the
# pool object still exists. Not terminal: drained pools keep being polled,
# because they do occasionally refill.
DRAINED_USD = 100.0


# ── plumbing ──────────────────────────────────────────────────────────────────


def _say(line: str = "") -> None:
    print(line.encode("ascii", "replace").decode("ascii"))


def _now_ms() -> int:
    return int(time.time() * 1000)


def _f(value: Any) -> float | None:
    """GeckoTerminal returns numbers as strings in several blocks."""
    if value is None or isinstance(value, bool):
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    if v != v or v in (float("inf"), float("-inf")):
        return None
    return v


def _i(value: Any) -> int | None:
    v = _f(value)
    return None if v is None else int(v)


def _iso_to_ms(value: Any) -> int | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        s = value.replace("Z", "+00:00")
        return int(datetime.fromisoformat(s).timestamp() * 1000)
    except ValueError:
        return None


class Throttle:
    """Spaced calls plus exponential backoff on 429.

    Shared across a whole run so the batch refresher and any backfill draw from
    the same budget. Without this the collector cheerfully rate-limits itself
    into a hole and the resulting gaps look like market events.
    """

    def __init__(self, spacing_s: float = MIN_SPACING_S) -> None:
        self.spacing = spacing_s
        self._last = 0.0
        self.calls = 0
        self.throttled = 0

    def wait(self) -> None:
        delta = time.monotonic() - self._last
        if delta < self.spacing:
            time.sleep(self.spacing - delta)
        self._last = time.monotonic()

    def get(self, url: str) -> tuple[Any | None, str | None]:
        """(payload, error). Never raises; the caller decides what a failure means."""
        for attempt in range(MAX_RETRIES):
            self.wait()
            try:
                req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT,
                                                           "Accept": "application/json"})
                with urllib.request.urlopen(req, timeout=TIMEOUT_S) as fh:
                    self.calls += 1
                    return json.load(fh), None
            except urllib.error.HTTPError as exc:
                if exc.code == 429:
                    self.throttled += 1
                    back = (2 ** attempt) * 5.0 + random.uniform(0, 2.0)
                    _say("  rate limited; backing off %.1fs" % back)
                    time.sleep(back)
                    continue
                if exc.code == 404:
                    return None, "http 404"
                return None, "http %d" % exc.code
            except Exception as exc:
                if attempt == MAX_RETRIES - 1:
                    return None, type(exc).__name__
                time.sleep(1.5 * (attempt + 1))
        return None, "rate limited"


# ── storage ───────────────────────────────────────────────────────────────────


def load_registry() -> dict:
    if not REGISTRY.exists():
        return {"version": 1, "pools": {}}
    try:
        with REGISTRY.open("r", encoding="utf-8") as fh:
            doc = json.load(fh)
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        # Never overwrite a registry we failed to parse -- it is the only copy
        # of the cohort and it cannot be re-collected.
        backup = REGISTRY.with_suffix(".corrupt-%d.json" % _now_ms())
        try:
            REGISTRY.replace(backup)
            _say("WARN: registry unreadable, preserved at %s" % backup.name)
        except OSError:
            pass
        return {"version": 1, "pools": {}}
    if not isinstance(doc, dict) or not isinstance(doc.get("pools"), dict):
        return {"version": 1, "pools": {}}
    return doc


def save_registry(doc: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = REGISTRY.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=1, sort_keys=True, default=str)
    os.replace(tmp, REGISTRY)


def _snapshot_path(now_ms: int) -> Path:
    day = datetime.fromtimestamp(now_ms / 1000.0, tz=timezone.utc).strftime("%Y-%m-%d")
    return DATA_DIR / ("snapshots-%s.jsonl" % day)


def append_snapshots(rows: list[dict], now_ms: int) -> int:
    if not rows:
        return 0
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    path = _snapshot_path(now_ms)
    with path.open("a", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, sort_keys=True, default=str) + "\n")
    return len(rows)


# ── parsing ───────────────────────────────────────────────────────────────────

_WINDOWS = ("m5", "m15", "m30", "h1", "h6", "h24")


def _rel_id(item: dict, name: str) -> str | None:
    try:
        return item["relationships"][name]["data"]["id"]
    except (KeyError, TypeError):
        return None


def birth_record(item: dict, now_ms: int) -> dict | None:
    """One registry entry from a `new_pools` / `multi` element."""
    attrs = item.get("attributes") or {}
    addr = attrs.get("address")
    if not addr:
        return None
    created_ms = _iso_to_ms(attrs.get("pool_created_at"))
    return {
        "pool": addr,
        "name": attrs.get("name"),
        "dex": _rel_id(item, "dex"),
        "base_token": _rel_id(item, "base_token"),
        "quote_token": _rel_id(item, "quote_token"),
        "created_ms": created_ms,
        "first_seen_ms": now_ms,
        # How late we were to the party. A study that needs true birth coverage
        # can filter on this instead of assuming every row was caught at t=0.
        "discovery_lag_s": (None if created_ms is None
                            else max(0.0, (now_ms - created_ms) / 1000.0)),
        "status": "alive",
        "miss_count": 0,
        "last_alive_ms": None,
        "first_miss_ms": None,
        "drained_since_ms": None,
        "last_poll_ms": None,
        "snapshots": 0,
    }


def snapshot_row(item: dict, now_ms: int) -> dict | None:
    """One observation. Raw values only -- no ratios, no scores, no judgements."""
    attrs = item.get("attributes") or {}
    addr = attrs.get("address")
    if not addr:
        return None

    txns = attrs.get("transactions") or {}
    vol = attrs.get("volume_usd") or {}
    chg = attrs.get("price_change_percentage") or {}

    row: dict = {
        "pool": addr,
        "ts_ms": now_ms,
        "price_usd": _f(attrs.get("base_token_price_usd")),
        "reserve_usd": _f(attrs.get("reserve_in_usd")),
        "fdv_usd": _f(attrs.get("fdv_usd")),
        "mcap_usd": _f(attrs.get("market_cap_usd")),
    }
    for w in _WINDOWS:
        t = txns.get(w) or {}
        row["buys_%s" % w] = _i(t.get("buys"))
        row["sells_%s" % w] = _i(t.get("sells"))
        # Unique wallets. buys=400 against buyers=3 is a script, not a market.
        # Recorded raw; interpreted nowhere in this module.
        row["buyers_%s" % w] = _i(t.get("buyers"))
        row["sellers_%s" % w] = _i(t.get("sellers"))
        row["vol_%s" % w] = _f(vol.get(w))
        row["chg_%s" % w] = _f(chg.get(w))
    return row


# ── discovery ─────────────────────────────────────────────────────────────────


def discover(reg: dict, th: Throttle, pages: int = 1) -> dict:
    """Poll new_pools and register anything unseen.

    COVERAGE, AND WHY IT IS MEASURED RATHER THAN ASSUMED
    ----------------------------------------------------
    Solana launches far more pools per minute than `new_pools` returns per call
    (pump.fun alone runs to five figures a day). So this collects a SAMPLE, not
    a census, and the honest question is not "did we get everything" -- we did
    not -- but "do we know how much we missed".

    The test is cheap and exact. `new_pools` is ordered newest-first, so if the
    OLDEST pool on the last page we fetched is one we already knew about, then
    our previous poll and this one overlap and nothing launched in between went
    unseen. If instead every pool on that page is new to us, the window
    OVERFLOWED: more pools were born between polls than we can see, and an
    unknown number fell off the end.

    That matters beyond raw counts, because overflow is not random. It happens
    precisely during launch-rate spikes, so an overflowing collector
    systematically under-samples busy periods -- exactly the periods a study of
    manias would care most about. The overflow rate is therefore recorded per
    poll and reported by `status`, so any result can be qualified by it (or the
    cadence raised until it goes to zero).
    """
    now = _now_ms()
    pools = reg.setdefault("pools", {})
    seen = new = 0
    rows: list[dict] = []
    last_page_items = 0
    last_page_new = 0

    for page in range(1, max(1, pages) + 1):
        url = "%s/networks/%s/new_pools?page=%d" % (API, NETWORK, page)
        payload, err = th.get(url)
        if err or not payload:
            _say("  discover page %d failed (%s)" % (page, err or "empty"))
            break
        items = payload.get("data") or []
        if not items:
            break
        last_page_items = len(items)
        last_page_new = 0
        for item in items:
            rec = birth_record(item, now)
            if rec is None:
                continue
            seen += 1
            if rec["pool"] not in pools:
                pools[rec["pool"]] = rec
                new += 1
                last_page_new += 1
            node = pools[rec["pool"]]
            node["last_poll_ms"] = now
            node["last_alive_ms"] = now
            node["miss_count"] = 0
            node["snapshots"] = int(node.get("snapshots") or 0) + 1
            srow = snapshot_row(item, now)
            if srow:
                rows.append(srow)

    append_snapshots(rows, now)

    # Overflow: every pool on the deepest page we read was unknown, so the feed
    # rolled past whatever we last saw. First run is exempt -- everything is new
    # by definition and that says nothing about the cadence.
    first_run = len(pools) == new
    overflowed = bool(last_page_items) and last_page_new == last_page_items \
        and not first_run

    cov = reg.setdefault("coverage", {"polls": 0, "overflows": 0, "last_ms": None})
    cov["polls"] = int(cov.get("polls") or 0) + 1
    if overflowed:
        cov["overflows"] = int(cov.get("overflows") or 0) + 1
    cov["last_ms"] = now
    cov["last_overflowed"] = overflowed

    return {"seen": seen, "new": new, "overflowed": overflowed,
            "first_run": first_run}


# ── refresh ───────────────────────────────────────────────────────────────────


def _interval_min(age_h: float | None) -> float:
    if age_h is None:
        return TIERS[0][1]
    for max_age, interval in TIERS:
        if age_h < max_age:
            return interval
    return TIERS[-1][1]


def _due(node: dict, now_ms: int) -> tuple[bool, float]:
    """(is_due, overdue_minutes). Terminal pools are never due."""
    if node.get("status") == "gone":
        return (False, 0.0)
    created = node.get("created_ms") or node.get("first_seen_ms")
    age_h = None if not created else max(0.0, (now_ms - float(created)) / 3_600_000.0)
    interval = _interval_min(age_h)
    last = node.get("last_poll_ms")
    if not last:
        return (True, 1e9)
    elapsed_min = (now_ms - float(last)) / 60_000.0
    return (elapsed_min >= interval, elapsed_min - interval)


def refresh(reg: dict, th: Throttle, budget_calls: int) -> dict:
    """Snapshot every due pool, in youngest-first priority, within the budget."""
    now = _now_ms()
    pools = reg.setdefault("pools", {})

    due = []
    for addr, node in pools.items():
        ok, overdue = _due(node, now)
        if not ok:
            continue
        created = node.get("created_ms") or node.get("first_seen_ms") or now
        age_h = max(0.0, (now - float(created)) / 3_600_000.0)
        due.append((age_h, -overdue, addr))
    # Youngest first: that is where both the price action and the mortality are.
    # Ties broken by how overdue the pool is, so nothing starves.
    due.sort()

    max_pools = max(0, budget_calls) * MULTI_BATCH
    selected = [a for _, _, a in due[:max_pools]]

    stats = {"due": len(due), "selected": len(selected), "snapshotted": 0,
             "missed": 0, "died": 0, "drained": 0, "batches": 0, "failed_batches": 0}
    rows: list[dict] = []

    for i in range(0, len(selected), MULTI_BATCH):
        batch = selected[i:i + MULTI_BATCH]
        url = "%s/networks/%s/pools/multi/%s" % (API, NETWORK, ",".join(batch))
        payload, err = th.get(url)
        stats["batches"] += 1

        if err or payload is None:
            # CRITICAL: a failed call is not evidence about any pool in it. No
            # miss counters move, no deaths are recorded. Otherwise one rate
            # limit reads as a mass extinction.
            stats["failed_batches"] += 1
            _say("  batch %d failed (%s) -- no misses counted" % (stats["batches"], err))
            continue

        returned = {}
        for item in (payload.get("data") or []):
            attrs = item.get("attributes") or {}
            addr = attrs.get("address")
            if addr:
                returned[addr] = item

        for addr in batch:
            node = pools.get(addr)
            if node is None:
                continue
            node["last_poll_ms"] = now
            item = returned.get(addr)

            if item is None:
                # Confirmed absent from a SUCCESSFUL response.
                node["miss_count"] = int(node.get("miss_count") or 0) + 1
                if node.get("first_miss_ms") is None:
                    node["first_miss_ms"] = now
                stats["missed"] += 1
                if node["miss_count"] >= DEATH_MISSES and node.get("status") != "gone":
                    node["status"] = "gone"
                    stats["died"] += 1
                continue

            node["miss_count"] = 0
            node["first_miss_ms"] = None
            node["last_alive_ms"] = now
            node["snapshots"] = int(node.get("snapshots") or 0) + 1

            row = snapshot_row(item, now)
            if row:
                rows.append(row)
                res = row.get("reserve_usd")
                if res is not None and res < DRAINED_USD:
                    if node.get("drained_since_ms") is None:
                        node["drained_since_ms"] = now
                        stats["drained"] += 1
                    node["status"] = "drained"
                else:
                    node["drained_since_ms"] = None
                    if node.get("status") != "gone":
                        node["status"] = "alive"

    stats["snapshotted"] = append_snapshots(rows, now)
    return stats


# ── ohlcv backfill ────────────────────────────────────────────────────────────


def backfill_ohlcv(reg: dict, th: Throttle, budget_calls: int,
                   timeframe: str = "minute", aggregate: int = 5,
                   min_age_h: float = 24.0) -> dict:
    """Cache candles for pools old enough to have a shape worth storing.

    Deliberately opt-in and budgeted: candles cost one call per pool and the
    free tier returns ~180 bars per call, so this is the expensive mode. Pools
    already cached are skipped; `gone` pools are prioritised because their
    candle history stops being fetchable once the indexer drops them.
    """
    OHLCV_DIR.mkdir(parents=True, exist_ok=True)
    now = _now_ms()
    pools = reg.setdefault("pools", {})

    def _priority(item):
        addr, node = item
        status = node.get("status")
        created = node.get("created_ms") or node.get("first_seen_ms") or now
        age_h = (now - float(created)) / 3_600_000.0
        # gone first (their history is perishable), then oldest
        return (0 if status == "gone" else 1, -age_h)

    candidates = []
    for addr, node in pools.items():
        created = node.get("created_ms") or node.get("first_seen_ms") or now
        age_h = (now - float(created)) / 3_600_000.0
        if age_h < min_age_h:
            continue
        if (OHLCV_DIR / ("%s.json" % addr)).exists():
            continue
        candidates.append((addr, node))
    candidates.sort(key=_priority)

    stats = {"eligible": len(candidates), "fetched": 0, "empty": 0, "failed": 0}
    for addr, node in candidates[:max(0, budget_calls)]:
        url = ("%s/networks/%s/pools/%s/ohlcv/%s?aggregate=%d&limit=1000"
               % (API, NETWORK, addr, timeframe, aggregate))
        payload, err = th.get(url)
        if err or not payload:
            stats["failed"] += 1
            continue
        bars = (((payload.get("data") or {}).get("attributes") or {})
                .get("ohlcv_list") or [])
        if not bars:
            stats["empty"] += 1
            continue
        doc = {
            "pool": addr,
            "timeframe": timeframe,
            "aggregate": aggregate,
            "fetched_ms": now,
            "status_at_fetch": node.get("status"),
            # [ts, open, high, low, close, volume]
            "ohlcv_list": bars,
        }
        with (OHLCV_DIR / ("%s.json" % addr)).open("w", encoding="utf-8") as fh:
            json.dump(doc, fh, separators=(",", ":"), default=str)
        stats["fetched"] += 1
    return stats


# ── status ────────────────────────────────────────────────────────────────────


def status(reg: dict) -> None:
    pools = reg.get("pools") or {}
    now = _now_ms()
    if not pools:
        _say("")
        _say("Cohort is empty. Run: python scripts/meme_cohort.py tick")
        return

    alive = sum(1 for n in pools.values() if n.get("status") == "alive")
    drained = sum(1 for n in pools.values() if n.get("status") == "drained")
    gone = sum(1 for n in pools.values() if n.get("status") == "gone")
    snaps = sum(int(n.get("snapshots") or 0) for n in pools.values())

    ages = [(now - float(n["created_ms"])) / 3_600_000.0
            for n in pools.values() if n.get("created_ms")]
    lags = [n["discovery_lag_s"] for n in pools.values()
            if n.get("discovery_lag_s") is not None]
    first = min((n.get("first_seen_ms") or now) for n in pools.values())

    _say("")
    _say("MEME COHORT -- %s" % NETWORK)
    _say("-" * 62)
    _say("  collecting since : %s (%.1f days)"
         % (datetime.fromtimestamp(first / 1000.0, tz=timezone.utc)
            .strftime("%Y-%m-%d %H:%M UTC"), (now - first) / 86_400_000.0))
    _say("  pools registered : %d" % len(pools))
    _say("    alive          : %d" % alive)
    _say("    drained        : %d  (reserve < $%.0f, still polled)" % (drained, DRAINED_USD))
    _say("    gone           : %d  (absent %dx from successful calls)" % (gone, DEATH_MISSES))
    _say("  snapshots taken  : %d" % snaps)
    if ages:
        ages.sort()
        _say("  cohort age       : median %.1fh, oldest %.1fd"
             % (ages[len(ages) // 2], max(ages) / 24.0))
    if lags:
        lags.sort()
        _say("  discovery lag    : median %.0fs, p90 %.0fs"
             % (lags[len(lags) // 2], lags[int(len(lags) * 0.9)]))
        _say("                     (birth -> first sighting; filter on this if a")
        _say("                      study needs true t=0 coverage)")

    cov = reg.get("coverage") or {}
    polls = int(cov.get("polls") or 0)
    overs = int(cov.get("overflows") or 0)
    if polls:
        rate = 100.0 * overs / polls
        _say("  discover polls  : %d, %d overflowed (%.0f%%)" % (polls, overs, rate))
        if rate >= 20.0:
            _say("    ^ the feed rolled past us on %.0f%% of polls, so an unknown" % rate)
            _say("      number of launches were never seen. Overflow clusters in")
            _say("      launch-rate SPIKES, so the cohort under-represents busy")
            _say("      periods. Raise --pages or shorten the interval, and treat")
            _say("      any current base rate as provisional.")
        elif rate > 0:
            _say("    ^ mostly complete; %d gap(s) where launches were missed" % overs)
        else:
            _say("    ^ no overflow: every launch between polls was captured")

    files = sorted(DATA_DIR.glob("snapshots-*.jsonl"))
    if files:
        total = sum(p.stat().st_size for p in files)
        _say("  snapshot files   : %d, %.1f MB" % (len(files), total / 1e6))
    cached = len(list(OHLCV_DIR.glob("*.json"))) if OHLCV_DIR.exists() else 0
    if cached:
        _say("  ohlcv cached     : %d pools" % cached)

    if gone + drained == 0 and len(pools) > 30:
        _say("")
        _say("  No deaths recorded yet. Expect that to change -- if it does not,")
        _say("  suspect the collector, not the market.")
    _say("")
    _say("  This is a data layer only: no scores, no gates, no signals. Studies")
    _say("  read these files; they do not live here.")


# ── cli ───────────────────────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="meme_cohort",
        description="Capture every new Solana pool at birth and follow it to its "
                    "end, deaths included. Pure data layer -- no signals, no "
                    "scoring, no gates.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = p.add_subparsers(dest="cmd")

    for name, help_text in (("tick", "discover + refresh (the cron entry)"),
                            ("discover", "poll new_pools and register newcomers"),
                            ("refresh", "snapshot due pools")):
        s = sub.add_parser(name, help=help_text)
        s.add_argument("--budget", type=int, default=18, metavar="N",
                       help="max API calls for the refresh phase (default: 18)")
        s.add_argument("--pages", type=int, default=1, metavar="N",
                       help="new_pools pages to scan (default: 1, 20 pools/page)")

    s = sub.add_parser("backfill-ohlcv", help="cache candles for aged/dead pools")
    s.add_argument("--budget", type=int, default=15, metavar="N",
                   help="max pools to fetch this run (1 call each, default: 15)")
    s.add_argument("--timeframe", default="minute", choices=("minute", "hour", "day"))
    s.add_argument("--aggregate", type=int, default=5, metavar="N")
    s.add_argument("--min-age-h", type=float, default=24.0, metavar="H",
                   dest="min_age_h")

    sub.add_parser("status", help="cohort summary")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    cmd = args.cmd or "status"

    reg = load_registry()

    if cmd == "status":
        status(reg)
        return 0

    th = Throttle()
    t0 = time.time()

    if cmd == "backfill-ohlcv":
        st = backfill_ohlcv(reg, th, args.budget, args.timeframe,
                            args.aggregate, args.min_age_h)
        save_registry(reg)
        _say("ohlcv: %d fetched, %d empty, %d failed (%d eligible) in %.0fs, %d calls"
             % (st["fetched"], st["empty"], st["failed"], st["eligible"],
                time.time() - t0, th.calls))
        return 0

    if cmd in ("tick", "discover"):
        d = discover(reg, th, args.pages)
        _say("discover: %d pools seen, %d new%s"
             % (d["seen"], d["new"],
                "  [FIRST RUN]" if d["first_run"] else
                "  [WINDOW OVERFLOWED -- launches were missed]" if d["overflowed"]
                else ""))
        if d["overflowed"]:
            _say("          raise --pages or shorten the cron interval; until "
                 "then the sample under-represents launch-rate spikes")

    if cmd in ("tick", "refresh"):
        st = refresh(reg, th, args.budget)
        _say("refresh: %d due, %d selected, %d snapshotted, %d missed, "
             "%d newly gone, %d newly drained"
             % (st["due"], st["selected"], st["snapshotted"], st["missed"],
                st["died"], st["drained"]))
        if st["failed_batches"]:
            _say("         %d/%d batches failed -- those pools were NOT counted "
                 "as misses" % (st["failed_batches"], st["batches"]))

    save_registry(reg)
    _say("registry: %d pools total  |  %d api calls, %d throttled, %.0fs"
         % (len(reg.get("pools") or {}), th.calls, th.throttled, time.time() - t0))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        _say("Interrupted.")
        sys.exit(130)
    except Exception as exc:
        _say("ERROR: unexpected failure (%s: %s)" % (type(exc).__name__, exc))
        sys.exit(1)

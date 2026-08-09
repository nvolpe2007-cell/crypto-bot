"""Callout ledger — the scorecard layer behind every meme-coin alert.

WHAT THIS IS
------------
An append-only record of every token this repo has ever *called out*, plus the
forward price marks that say whether the callout was worth acting on. Two
producers write into it:

    scripts/meme_radar.py      source="radar"   -- tokens the bot found itself
    scripts/telegram_ingest.py source="tg:<ch>" -- tokens a human channel called

Both land in the same schema so bot-found and human-called tokens are scored
head to head, by the same rule, against the same cost model.

WHY IT EXISTS
-------------
Every research verdict in this repo died on the same wall: a signal that looked
real gross was dead net of cost (the arb arm went 0/34 with fees 12x gross; the
funding-decay study was a null; the lev_perp 73% streak was one trend, not an
edge). Meme callouts have all the same failure modes plus a worse base rate, so
the alerting and the scoring are built at the same time, deliberately. An alert
stream with no ledger behind it is a machine for manufacturing hindsight.

THE ONLY NUMBER THAT COUNTS IS NET
----------------------------------
A callout is scored on `net_ret_pct` -- the price move MINUS the round-trip cost
of actually taking it at the ticket size the caller was screened against. A
token that went +8% on a pool where breakeven was 12% did not win. Gross return
is recorded too, but it is decoration; `wins` counts net-positive marks only.

MARK HORIZONS
-------------
Each callout is marked forward at 1h / 4h / 24h / 72h from the alert. Marks are
taken opportunistically by whatever run happens to be closest to the horizon
(cron granularity is minutes, so the recorded `at_ms` is what actually happened,
never the nominal horizon). A horizon whose window has passed without a
reachable price is recorded as unreachable rather than dropped -- a token that
stopped being quotable is a total loss, not missing data, and silently omitting
it is precisely how a hit rate gets inflated.

SAFETY
------
Read-only with respect to money. No wallet, no keys, no order placement. Writes
exactly one file (`data/callout_ledger.json`) via atomic replace.

Stdlib only, matching screener_sources / screener_risk.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any, Iterable

__all__ = [
    "LEDGER_FILE",
    "HORIZONS_H",
    "load_ledger",
    "save_ledger",
    "callout_id",
    "find_callout",
    "add_callout",
    "due_horizons",
    "record_mark",
    "scorecard",
    "caller_table",
]

ROOT = Path(__file__).resolve().parent.parent
LEDGER_FILE = ROOT / "data" / "callout_ledger.json"

SCHEMA_VERSION = 1

# Forward-mark horizons, in hours from the alert.
HORIZONS_H: tuple[float, ...] = (1.0, 4.0, 24.0, 72.0)

# A mark is accepted for a horizon once that horizon has arrived, and the slot
# is closed out once we are this far past it. Anything later would be marking a
# 24h horizon at 40h and calling it 24h.
MARK_GRACE_H = 2.0

# After the last horizon plus grace, a callout is done: no further marks, and it
# counts toward the scorecard denominator forever.
_LAST_H = max(HORIZONS_H)


# ── io ────────────────────────────────────────────────────────────────────────


def _now_ms() -> int:
    return int(time.time() * 1000)


def _empty() -> dict:
    return {"version": SCHEMA_VERSION, "callouts": []}


def load_ledger(path: Path | str | None = None) -> dict:
    """Read the ledger. A missing or corrupt file reads as empty, never raises.

    A corrupt ledger is renamed aside rather than overwritten in place, because
    the alternative is silently destroying the only record of what was called.
    """
    p = Path(path) if path is not None else LEDGER_FILE
    if not p.exists():
        return _empty()
    try:
        with p.open("r", encoding="utf-8") as fh:
            doc = json.load(fh)
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        try:
            p.replace(p.with_suffix(p.suffix + ".corrupt-%d" % _now_ms()))
        except OSError:
            pass
        return _empty()
    if not isinstance(doc, dict) or not isinstance(doc.get("callouts"), list):
        return _empty()
    doc.setdefault("version", SCHEMA_VERSION)
    return doc


def save_ledger(doc: dict, path: Path | str | None = None) -> Path:
    """Atomically write the ledger (temp file in the same dir, then replace)."""
    p = Path(path) if path is not None else LEDGER_FILE
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix=".callout_ledger.",
                               suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, indent=2, sort_keys=True, default=str)
        os.replace(tmp, p)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return p


# ── identity ──────────────────────────────────────────────────────────────────


def callout_id(chain: str, token_address: str, source: str) -> str:
    """Stable key for one (token, source) pair.

    Source is part of the key on purpose: if the radar finds a token AND three
    channels call it, that is four separate callouts to score. Collapsing them
    would erase exactly the comparison this ledger exists to make -- who was
    early and who was late to the same token.
    """
    return "%s:%s:%s" % (
        str(chain or "?").lower(),
        str(token_address or "?").lower(),
        str(source or "?").lower(),
    )


def find_callout(doc: dict, cid: str) -> dict | None:
    for c in doc.get("callouts", []):
        if isinstance(c, dict) and c.get("id") == cid:
            return c
    return None


# ── writing ───────────────────────────────────────────────────────────────────


def _f(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    if v != v or v in (float("inf"), float("-inf")):
        return None
    return v


def add_callout(
    doc: dict,
    *,
    record: Any,
    screen: dict,
    source: str,
    tier: str,
    ticket_usd: float,
    caller: str | None = None,
    trigger: str = "",
    called_at_ms: int | None = None,
) -> dict | None:
    """Append one callout. Returns the new entry, or None if already present.

    `record` is a screener_sources.TokenRecord (duck typed); `screen` is the
    screener_risk.score() dict it was alerted with. Both are read defensively --
    a missing field becomes None, never an exception, because the ledger write
    must never be the thing that kills an alert run.
    """
    chain = str(getattr(record, "chain", "") or "")
    addr = str(getattr(record, "token_address", "") or "")
    cid = callout_id(chain, addr, source)
    if find_callout(doc, cid) is not None:
        return None

    cost = screen.get("cost") if isinstance(screen.get("cost"), dict) else {}
    metrics = screen.get("metrics") if isinstance(screen.get("metrics"), dict) else {}

    entry = {
        "id": cid,
        "source": source,
        "caller": caller,
        "trigger": trigger,
        "tier": tier,
        "symbol": str(getattr(record, "symbol", "") or "?"),
        "name": str(getattr(record, "name", "") or ""),
        "chain": chain,
        "dex": str(getattr(record, "dex", "") or ""),
        "token_address": addr,
        "pair_address": str(getattr(record, "pair_address", "") or ""),
        "url": str(getattr(record, "url", "") or ""),
        "called_at_ms": int(called_at_ms if called_at_ms is not None else _now_ms()),
        "verdict": screen.get("verdict"),
        "ticket_usd": float(ticket_usd),
        "entry_price_usd": _f(getattr(record, "price_usd", None)),
        "entry_liquidity_usd": _f(getattr(record, "liquidity_usd", None)),
        "entry_fdv_usd": _f(getattr(record, "fdv_usd", None)),
        "entry_age_hours": _f(metrics.get("age_hours")),
        "entry_txns_h24": metrics.get("txns_h24"),
        "rt_cost_usd": _f(cost.get("total_usd")),
        "rt_cost_pct": _f(cost.get("total_pct")),
        "breakeven_move_pct": _f(cost.get("breakeven_move_pct")),
        "cost_basis": cost.get("basis"),
        "flags": [str(f) for f in (screen.get("flags") or [])],
        "reasons": [str(r) for r in (screen.get("reasons") or [])],
        "marks": {},
        "status": "open",
    }
    doc.setdefault("callouts", []).append(entry)
    return entry


def _horizon_key(hours: float) -> str:
    return ("%gh" % hours)


def due_horizons(entry: dict, now_ms: int | None = None) -> list[float]:
    """Horizons that have arrived, are still inside the grace window, and have
    no mark yet. Empty list means this callout needs no work right now."""
    now = now_ms if now_ms is not None else _now_ms()
    called = entry.get("called_at_ms")
    if not isinstance(called, (int, float)):
        return []
    elapsed_h = (now - float(called)) / 3_600_000.0
    marks = entry.get("marks") or {}
    due = []
    for h in HORIZONS_H:
        if _horizon_key(h) in marks:
            continue
        if elapsed_h >= h and elapsed_h <= h + MARK_GRACE_H:
            due.append(h)
    return due


def expire_stale(entry: dict, now_ms: int | None = None) -> list[float]:
    """Horizons whose window closed with no mark taken. Recorded as unreachable.

    This is the anti-survivorship hook. A token that went untradeable, delisted,
    or rugged stops returning a price -- and if those simply stayed blank, the
    hit rate would be computed over only the tokens that survived to be marked.
    """
    now = now_ms if now_ms is not None else _now_ms()
    called = entry.get("called_at_ms")
    if not isinstance(called, (int, float)):
        return []
    elapsed_h = (now - float(called)) / 3_600_000.0
    marks = entry.get("marks") or {}
    return [h for h in HORIZONS_H
            if _horizon_key(h) not in marks and elapsed_h > h + MARK_GRACE_H]


def record_mark(
    entry: dict,
    hours: float,
    *,
    price_usd: float | None,
    liquidity_usd: float | None = None,
    at_ms: int | None = None,
    reachable: bool = True,
) -> dict:
    """Record one forward mark and compute gross + net return against entry.

    net_ret_pct subtracts the round-trip cost of the position that was screened.
    When the cost was never modellable (bonding curve, no liquidity object) net
    is left None rather than set equal to gross -- an unpriceable exit is not a
    free one, and pretending otherwise would flatter every pump.fun callout in
    the book.
    """
    key = _horizon_key(hours)
    entry_price = _f(entry.get("entry_price_usd"))
    price = _f(price_usd)

    mark: dict = {
        "at_ms": int(at_ms if at_ms is not None else _now_ms()),
        "nominal_h": hours,
        "reachable": bool(reachable and price is not None and price > 0),
        "price_usd": price,
        "liquidity_usd": _f(liquidity_usd),
        "ret_pct": None,
        "net_ret_pct": None,
    }

    if not mark["reachable"]:
        # No quotable price at the horizon. That is a -100% outcome for a
        # position you cannot exit, and it is scored as one.
        mark["ret_pct"] = -100.0
        mark["net_ret_pct"] = -100.0
    elif entry_price and entry_price > 0 and price is not None:
        gross = (price / entry_price - 1.0) * 100.0
        mark["ret_pct"] = gross
        be = _f(entry.get("breakeven_move_pct"))
        if be is not None:
            # Breakeven is the move needed to get back to flat, so net of the
            # full round trip the position is up (gross - breakeven).
            mark["net_ret_pct"] = gross - be

    entry.setdefault("marks", {})[key] = mark

    called = entry.get("called_at_ms")
    if isinstance(called, (int, float)):
        elapsed_h = (mark["at_ms"] - float(called)) / 3_600_000.0
        if elapsed_h > _LAST_H + MARK_GRACE_H or len(entry["marks"]) >= len(HORIZONS_H):
            entry["status"] = "complete"
    return mark


# ── scoring ───────────────────────────────────────────────────────────────────


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    s = sorted(values)
    n = len(s)
    mid = n // 2
    return s[mid] if n % 2 else (s[mid - 1] + s[mid]) / 2.0


def scorecard(
    callouts: Iterable[dict],
    horizon_h: float = 24.0,
    *,
    ticket_usd: float | None = None,
) -> dict:
    """Roll up outcomes at one horizon.

    Returns counts, net hit rate, median/mean net return, and the dollar result
    of having taken every single callout at its screened ticket size -- which is
    the number that actually answers "should I be trading these".
    """
    key = _horizon_key(horizon_h)
    rows = [c for c in callouts if isinstance(c, dict)]
    marked = [c for c in rows if key in (c.get("marks") or {})]

    nets: list[float] = []
    grosses: list[float] = []
    dollars = 0.0
    dollar_n = 0
    wins = 0
    unreachable = 0

    for c in marked:
        m = c["marks"][key]
        if not m.get("reachable", True):
            unreachable += 1
        net = _f(m.get("net_ret_pct"))
        gross = _f(m.get("ret_pct"))
        if gross is not None:
            grosses.append(gross)
        if net is None:
            continue
        nets.append(net)
        if net > 0:
            wins += 1
        ticket = _f(ticket_usd) or _f(c.get("ticket_usd")) or 0.0
        if ticket:
            dollars += ticket * net / 100.0
            dollar_n += 1

    return {
        "horizon": key,
        "called": len(rows),
        "marked": len(marked),
        "scored": len(nets),          # marked AND net-computable
        "unpriced": len(marked) - len(nets),
        "unreachable": unreachable,
        "wins": wins,
        "losses": len(nets) - wins,
        "hit_rate_pct": (100.0 * wins / len(nets)) if nets else None,
        "median_net_pct": _median(nets),
        "mean_net_pct": (sum(nets) / len(nets)) if nets else None,
        "median_gross_pct": _median(grosses),
        "best_net_pct": max(nets) if nets else None,
        "worst_net_pct": min(nets) if nets else None,
        "total_pnl_usd": dollars if dollar_n else None,
        "pnl_n": dollar_n,
    }


def caller_table(callouts: Iterable[dict], horizon_h: float = 24.0) -> list[dict]:
    """Per-source scorecard, worst-last. Answers: who is actually early?

    A channel whose calls are reliably net-negative at 24h is not a bad caller
    by accident -- it is one whose subscribers are the exit. That is the single
    most useful thing this ledger can tell you, and it needs a real sample
    (n>=20) before it means anything at all.
    """
    groups: dict[str, list[dict]] = {}
    for c in callouts:
        if not isinstance(c, dict):
            continue
        key = str(c.get("caller") or c.get("source") or "?")
        groups.setdefault(key, []).append(c)

    table = []
    for name, rows in groups.items():
        card = scorecard(rows, horizon_h)
        card["caller"] = name
        table.append(card)

    table.sort(key=lambda r: (
        -(r["median_net_pct"] if r["median_net_pct"] is not None else -1e9),
        -r["scored"],
    ))
    return table

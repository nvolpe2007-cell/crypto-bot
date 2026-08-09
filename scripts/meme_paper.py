#!/usr/bin/env python3
"""Meme paper book — a CONTROLLED test of whether the filters have any power.

WHAT THIS IS
------------
A paper-trading book over the captured cohort (`data/meme_cohort/`), run as a
three-arm experiment rather than a strategy. It opens a notional position in
every pool that reaches a decision point and sorts it into one of three arms:

    pass     cleared the risk gates AND the delta triggers fired
    gates    cleared the risk gates, but the delta triggers did NOT fire
    control  FAILED the risk gates (random age-matched sample)

WHY THREE ARMS AND NOT ONE
--------------------------
A book that only trades what passes tells you the strategy's P&L. It cannot
tell you whether the FILTER did anything, because there is no counterfactual: a
positive result is equally consistent with "the filter picked winners", "the
whole market went up", and "the filter discarded the winners and kept the
mediocre ones".

The control arm supplies the counterfactual. The `gates` arm then decomposes the
result further:

    pass vs control   -> do the gates + delta together select anything?
    gates vs control  -> do the risk gates alone select anything?
    pass vs gates     -> does the delta trigger add anything ON TOP of the gates?

That last comparison is the one that actually judges the delta logic, and it is
invisible to a single-arm book.

NOT A STRATEGY
--------------
No money moves. No orders. Nothing here is executable -- these are Solana AMM
pools and the bot trades Kraken. This exists to answer one measurement question
and should be switched off the moment it has.

COSTS ARE CHARGED, BOTH LEGS
----------------------------
Entry and exit each pay half of `screener_risk.round_trip_cost` for the ticket.
An edge that only exists gross is the way every previous candidate in this repo
died, so gross is recorded but never headlined.

Control-arm pools frequently have NO modellable exit (bonding curve, no
liquidity object). Those are flagged `tradeable=False` and the report gives the
control result as a RANGE: once excluding them, once scoring them -100%.
Excluding them alone would flatter the control by dropping exactly the positions
that would have been impossible to sell -- which is the filter's whole point.

DEATHS
------
A pool that goes `gone` or `drained` closes at -100%. Never dropped.

USAGE
-----
    python scripts/meme_paper.py tick            # open/mark/close from cohort
    python scripts/meme_paper.py report
    python scripts/meme_paper.py report --json data/meme_paper_report.json
"""

from __future__ import annotations

import argparse
import glob
import json
import os

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import meme_radar, screener_risk  # noqa: E402

COHORT_DIR = ROOT / "data" / "meme_cohort"
BOOK_FILE = ROOT / "data" / "meme_paper_book.json"

TICKET_USD = 100.0
HOLD_H = 24.0            # primary exit: fixed horizon
MIN_OBS = 2              # snapshots needed before a pool can be judged
MIN_N = 30               # below this, any arm comparison is noise

# Fraction of gate-REJECTING pools sampled into the control arm.
#
# Sized independently of how many pools pass, on purpose. Tying the control rate
# to the pass count (the obvious design) starves the control arm exactly when
# passes are rare -- and passes ARE rare, measured at ~4%. The result is no
# counterfactual on precisely the days the filter is most selective.
#
# 10% of rejects against a ~4% pass rate accumulates a control arm several times
# larger than the pass arm, which is what you want: the comparison is limited by
# the SMALLER arm, so oversampling the cheap side costs nothing and buys a
# tighter estimate of the base rate.
CONTROL_SAMPLE_RATE = 0.10

# Deterministic control sampling: the same cohort must always produce the same
# control arm, or the experiment is unreproducible and the control becomes a
# free parameter to fiddle with. Keyed on the pool address rather than a running
# RNG so the decision does not depend on iteration order or on which tick
# happened to see the pool first.
CONTROL_SEED = 20260809


def _in_control_sample(pool: str) -> bool:
    """Stable per-pool coin flip. Same pool -> same answer, forever."""
    import hashlib
    h = hashlib.sha256(("%d:%s" % (CONTROL_SEED, pool)).encode("utf-8")).digest()
    return (int.from_bytes(h[:4], "big") / 0xFFFFFFFF) < CONTROL_SAMPLE_RATE


def _say(line: str = "") -> None:
    print(line.encode("ascii", "replace").decode("ascii"))


def _f(v):
    if v is None or isinstance(v, bool):
        return None
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return None if (x != x or x in (float("inf"), float("-inf"))) else x


def _now_ms() -> int:
    return int(time.time() * 1000)


# ── cohort access ─────────────────────────────────────────────────────────────


def load_cohort() -> tuple[dict, dict]:
    reg_path = COHORT_DIR / "registry.json"
    if not reg_path.exists():
        raise SystemExit("No cohort yet at %s" % COHORT_DIR)
    with reg_path.open("r", encoding="utf-8") as fh:
        pools = (json.load(fh) or {}).get("pools") or {}
    series: dict[str, list] = {}
    for path in sorted(glob.glob(str(COHORT_DIR / "snapshots-*.jsonl"))):
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                p = row.get("pool")
                if p:
                    series.setdefault(p, []).append(row)
    for rows in series.values():
        rows.sort(key=lambda r: r.get("ts_ms") or 0)
    return pools, series


class _Rec:
    """screener_risk-compatible shim built from cohort registry + snapshot.

    screener_risk is duck typed and imports nothing from the sources layer, so a
    plain object with the right attributes is a first-class input.
    """

    def __init__(self, node: dict, snap: dict, now_ms: int):
        self.symbol = (node.get("name") or "?").split("/")[0].strip()[:12] or "?"
        self.name = node.get("name") or ""
        self.chain = "solana"
        self.dex = (node.get("dex") or "").replace("solana_", "")
        self.token_address = node.get("base_token") or node.get("pool") or ""
        self.pair_address = node.get("pool") or ""
        self.url = "https://dexscreener.com/solana/%s" % (node.get("pool") or "")
        self.liquidity_usd = _f(snap.get("reserve_usd")) or 0.0
        self.volume_h24_usd = _f(snap.get("vol_h24"))
        self.fdv_usd = _f(snap.get("fdv_usd"))
        self.market_cap_usd = _f(snap.get("mcap_usd"))
        self.pair_created_ms = node.get("created_ms")
        created = node.get("created_ms")
        self.age_hours = (None if not created
                          else max(0.0, (now_ms - float(created)) / 3_600_000.0))
        self.price_usd = _f(snap.get("price_usd"))
        self.buys_h24 = snap.get("buys_h24") or 0
        self.sells_h24 = snap.get("sells_h24") or 0
        self.pair_count = 1
        self.boosted = False
        self.price_change_h24 = _f(snap.get("chg_h24"))


def _delta_node(rows: list) -> dict:
    """Adapt cohort snapshots into the poll shape meme_radar.evaluate_delta wants."""
    return {"polls": [{
        "ms": r.get("ts_ms"),
        "liq": _f(r.get("reserve_usd")),
        "buys": r.get("buys_h24"),
        "sells": r.get("sells_h24"),
        # Unique wallets: the cohort records these natively, so the v2 trigger
        # can be evaluated here with no enrichment call.
        "buyers": r.get("buyers_h24"),
        "sellers": r.get("sellers_h24"),
        "price": _f(r.get("price_usd")),
    } for r in rows]}


# ── book ──────────────────────────────────────────────────────────────────────


def load_book() -> dict:
    if not BOOK_FILE.exists():
        return {"version": 1, "positions": {}}
    try:
        with BOOK_FILE.open("r", encoding="utf-8") as fh:
            doc = json.load(fh)
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        return {"version": 1, "positions": {}}
    if not isinstance(doc, dict) or not isinstance(doc.get("positions"), dict):
        return {"version": 1, "positions": {}}
    return doc


def save_book(doc: dict) -> None:
    BOOK_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = BOOK_FILE.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=1, sort_keys=True, default=str)
    os.replace(tmp, BOOK_FILE)


def open_position(book: dict, pool: str, arm: str, rec: _Rec, screen: dict,
                  trigger: str, now_ms: int,
                  v1_fired: bool = False, v2_fired: bool = False) -> dict | None:
    if pool in book["positions"]:
        return None
    price = rec.price_usd
    if not price or price <= 0:
        return None
    cost = screen.get("cost") or {}
    total_pct = _f(cost.get("total_pct"))
    return book["positions"].setdefault(pool, {
        "pool": pool, "arm": arm, "symbol": rec.symbol,
        "opened_ms": now_ms, "entry_price": price,
        "entry_liq": rec.liquidity_usd, "entry_age_h": rec.age_hours,
        "ticket_usd": TICKET_USD,
        "rt_cost_pct": total_pct,
        "cost_basis": cost.get("basis"),
        # No modellable exit means you could not actually have sold it. Recorded,
        # not silently dropped -- dropping these is what flatters a control arm.
        "tradeable": total_pct is not None,
        "trigger": trigger,
        # Both delta variants recorded on EVERY position, independent of arm.
        # Arm assignment is untouched so the pre-registered comparisons stay
        # valid; v1-vs-v2 is then a declared slice of the same data rather than
        # a fourth arm competing for the same pools.
        "delta_v1_fired": bool(v1_fired),
        "delta_v2_fired": bool(v2_fired),
        "status": "open",
        "mfe_pct": 0.0, "mae_pct": 0.0,
        "marks": 0,
        "exit_price": None, "exit_ms": None, "exit_reason": None,
        "gross_pct": None, "net_pct": None, "net_usd": None,
    })


def close_position(pos: dict, price, now_ms: int, reason: str) -> None:
    entry = _f(pos.get("entry_price"))
    p = _f(price)
    if reason == "dead" or p is None or p <= 0 or not entry:
        gross = -100.0
    else:
        gross = (p / entry - 1.0) * 100.0
    rt = _f(pos.get("rt_cost_pct"))
    # Unmodellable cost: leave net None. Assigning gross would price a position
    # you could not have exited as if exiting were free.
    net = None if rt is None else (gross - rt if gross > -100.0 else -100.0)
    pos["status"] = "closed"
    pos["exit_price"] = p
    pos["exit_ms"] = now_ms
    pos["exit_reason"] = reason
    pos["gross_pct"] = gross
    pos["net_pct"] = net
    pos["net_usd"] = None if net is None else pos["ticket_usd"] * net / 100.0


# ── tick ──────────────────────────────────────────────────────────────────────


def tick(book: dict, verbose: bool = False) -> dict:
    pools, series = load_cohort()
    now = _now_ms()
    stats = {"evaluated": 0, "opened_pass": 0, "opened_gates": 0,
             "opened_control": 0, "marked": 0, "closed": 0, "dead": 0}

    # 1. mark and close what is already open
    for pool, pos in list(book["positions"].items()):
        if pos.get("status") != "open":
            continue
        node = pools.get(pool) or {}
        rows = series.get(pool) or []
        last = rows[-1] if rows else None
        status = node.get("status")

        if status in ("gone", "drained"):
            close_position(pos, None, now, "dead")
            stats["closed"] += 1
            stats["dead"] += 1
            continue

        if last:
            price = _f(last.get("price_usd"))
            entry = _f(pos.get("entry_price"))
            if price and entry and entry > 0:
                pct = (price / entry - 1.0) * 100.0
                pos["mfe_pct"] = max(_f(pos.get("mfe_pct")) or 0.0, pct)
                pos["mae_pct"] = min(_f(pos.get("mae_pct")) or 0.0, pct)
                pos["marks"] = int(pos.get("marks") or 0) + 1
                stats["marked"] += 1

        held_h = (now - float(pos["opened_ms"])) / 3_600_000.0
        if held_h >= HOLD_H:
            close_position(pos, _f(last.get("price_usd")) if last else None,
                           now, "horizon")
            stats["closed"] += 1

    # 2. evaluate candidates for entry
    for pool, rows in series.items():
        if pool in book["positions"] or len(rows) < MIN_OBS:
            continue
        node = pools.get(pool)
        if not node or node.get("status") in ("gone", "drained"):
            continue

        rec = _Rec(node, rows[-1], now)
        try:
            screen = screener_risk.score(rec, ticket_usd=TICKET_USD)
        except Exception:
            continue
        stats["evaluated"] += 1

        passed_gates = screen.get("verdict") == "PASS"
        node = _delta_node(rows)
        delta = meme_radar.evaluate_delta(node)              # v1: trade counts
        delta2 = meme_radar.evaluate_delta_buyers(node)      # v2: unique buyers
        v1, v2 = bool(delta["fired"]), bool(delta2["fired"])

        if passed_gates and v1:
            if open_position(book, pool, "pass", rec, screen,
                             delta["trigger"], now, v1, v2):
                stats["opened_pass"] += 1
        elif passed_gates:
            if open_position(book, pool, "gates", rec, screen,
                             "gates only", now, v1, v2):
                stats["opened_gates"] += 1
        elif _in_control_sample(pool):
            if open_position(book, pool, "control", rec, screen,
                             "REJECT: " + "; ".join(
                                 screen.get("reasons") or [])[:80], now, v1, v2):
                stats["opened_control"] += 1

    return stats


# ── report ────────────────────────────────────────────────────────────────────


def _median(v):
    if not v:
        return None
    s = sorted(v)
    n = len(s)
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2.0


def arm_stats(positions: list, treat_untradeable_as_total_loss: bool) -> dict:
    closed = [p for p in positions if p.get("status") == "closed"]
    nets, gross, dollars = [], [], 0.0
    untradeable = 0
    for p in closed:
        g = _f(p.get("gross_pct"))
        if g is not None:
            gross.append(g)
        n = _f(p.get("net_pct"))
        if n is None:
            untradeable += 1
            if not treat_untradeable_as_total_loss:
                continue
            n = -100.0
        nets.append(n)
        dollars += p.get("ticket_usd", TICKET_USD) * n / 100.0
    wins = sum(1 for x in nets if x > 0)
    return {
        "open": sum(1 for p in positions if p.get("status") == "open"),
        "closed": len(closed), "scored": len(nets), "untradeable": untradeable,
        "wins": wins,
        "hit_pct": (100.0 * wins / len(nets)) if nets else None,
        "median_net": _median(nets), "mean_net": (sum(nets) / len(nets)) if nets else None,
        "median_gross": _median(gross),
        "worst": min(nets) if nets else None, "best": max(nets) if nets else None,
        "pnl_usd": dollars if nets else None,
        "dead": sum(1 for p in closed if p.get("exit_reason") == "dead"),
        "median_entry_age_h": _median([_f(p.get("entry_age_h")) for p in positions
                                       if _f(p.get("entry_age_h")) is not None]),
    }


def report(book: dict, as_json: str | None = None) -> None:
    by_arm: dict[str, list] = {"pass": [], "gates": [], "control": []}
    for p in book.get("positions", {}).values():
        by_arm.setdefault(p.get("arm", "?"), []).append(p)

    doc = {}
    _say("")
    _say("MEME PAPER BOOK -- controlled filter test, $%d/position, %gh hold"
         % (TICKET_USD, HOLD_H))
    _say("=" * 74)
    _say("Net of round-trip cost, charged on both legs. Deaths close at -100%.")
    _say("")
    hdr = "%-9s %6s %7s %7s %8s %10s %10s %11s" % (
        "ARM", "OPEN", "CLOSED", "DEAD", "HIT", "MED NET", "MEAN NET", "P&L")
    _say(hdr)
    _say("-" * len(hdr))
    for arm in ("pass", "gates", "control"):
        s = arm_stats(by_arm.get(arm, []), treat_untradeable_as_total_loss=False)
        doc[arm] = s
        _say("%-9s %6d %7d %7d %8s %10s %10s %11s" % (
            arm, s["open"], s["closed"], s["dead"],
            "n/a" if s["hit_pct"] is None else "%.0f%%" % s["hit_pct"],
            "n/a" if s["median_net"] is None else "%+.1f%%" % s["median_net"],
            "n/a" if s["mean_net"] is None else "%+.1f%%" % s["mean_net"],
            "n/a" if s["pnl_usd"] is None else
            ("-$" if s["pnl_usd"] < 0 else "$") + format(abs(s["pnl_usd"]), ",.0f"),
        ))

    ctrl_hi = arm_stats(by_arm.get("control", []), True)
    if ctrl_hi["untradeable"]:
        _say("")
        _say("  control arm: %d position(s) had NO modellable exit."
             % ctrl_hi["untradeable"])
        _say("  Excluding them (above): %s.  Scoring them -100%%: %s." % (
            "n/a" if doc["control"]["median_net"] is None
            else "%+.1f%%" % doc["control"]["median_net"],
            "n/a" if ctrl_hi["median_net"] is None
            else "%+.1f%%" % ctrl_hi["median_net"]))
        _say("  The truth is the second one -- you could not have sold those.")
        doc["control_untradeable_as_loss"] = ctrl_hi

    _say("")
    _say("THE THREE COMPARISONS THIS EXISTS TO MAKE")
    _say("-" * 74)
    for label, a, b in (("gates vs control  (do the risk gates select anything?)",
                         "gates", "control"),
                        ("pass  vs control  (gates + delta together)",
                         "pass", "control"),
                        ("pass  vs gates    (does the delta ADD anything?)",
                         "pass", "gates")):
        sa, sb = doc.get(a, {}), doc.get(b, {})
        na, nb = sa.get("scored") or 0, sb.get("scored") or 0
        if na < MIN_N or nb < MIN_N:
            _say("  %s" % label)
            _say("      n=%d vs %d -- below the %d floor, no read yet"
                 % (na, nb, MIN_N))
            continue
        ma, mb = sa.get("median_net"), sb.get("median_net")
        if ma is None or mb is None:
            continue
        _say("  %s" % label)
        _say("      median net %+.1f%% vs %+.1f%%   ->  %+.1f pts"
             % (ma, mb, ma - mb))

    _say("")
    _say("AGE CONFOUND -- read the control comparison with this in mind")
    _say("-" * 74)
    for arm in ("pass", "gates", "control"):
        a = doc.get(arm, {}).get("median_entry_age_h")
        _say("  %-9s median age at entry: %s"
             % (arm, "n/a" if a is None else "%.1fh" % a))
    _say("  The control arm is YOUNG BY CONSTRUCTION: a 48h age minimum is one")
    _say("  of the gates, so rejects skew newborn. `pass/gates vs control` is")
    _say("  therefore a test of the gates AS A WHOLE, age included -- not of")
    _say("  liquidity or cost in isolation. `pass vs gates` is the clean one:")
    _say("  both arms cleared the same gates, so only the delta differs.")

    _say("")
    _say("  Until every arm clears n=%d this table is descriptive only. Meme" % MIN_N)
    _say("  returns are violently fat-tailed; a handful of samples is a story.")
    _say("  A positive `pass` arm means nothing on its own -- it has to beat")
    _say("  `control`, or the filter selected nothing the market did not.")

    if as_json:
        p = Path(as_json)
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("w", encoding="utf-8") as fh:
            json.dump(doc, fh, indent=2, sort_keys=True, default=str)
        _say("")
        _say("Wrote %s" % p)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="meme_paper",
        description="Three-arm controlled paper test of the meme filters. "
                    "Not a strategy; no money moves.")
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("tick", help="open/mark/close from the cohort")
    r = sub.add_parser("report", help="arm comparison")
    r.add_argument("--json", dest="json_path", default=None)
    args = ap.parse_args(argv)

    book = load_book()
    if (args.cmd or "report") == "tick":
        s = tick(book)
        save_book(book)
        _say("evaluated %d | opened pass=%d gates=%d control=%d | "
             "marked %d | closed %d (%d dead)"
             % (s["evaluated"], s["opened_pass"], s["opened_gates"],
                s["opened_control"], s["marked"], s["closed"], s["dead"]))
        return 0

    report(book, getattr(args, "json_path", None))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
    except SystemExit:
        raise
    except Exception as exc:
        _say("ERROR: %s: %s" % (type(exc).__name__, exc))
        sys.exit(1)

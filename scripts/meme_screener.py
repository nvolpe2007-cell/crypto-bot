#!/usr/bin/env python3
"""Meme-coin RISK SCREENER -- command-line front end.

WHAT THIS IS
------------
A read-only risk filter over newly-listed DEX tokens. It pulls objective,
observable facts (liquidity, 24h volume, pair age, buy/sell counts, FDV) from
`screener_sources`, prices what a position of a given size would cost to enter
AND exit via `screener_risk`, and applies hard gates. The output answers exactly
one question:

    "Is this token so thin, so young, or so expensive to trade that a position
     of $N is untradeable before the price does anything at all?"

That is a DOWNSIDE question. The honest headline of this tool is that most new
tokens fail it -- the rejection summary is the product, not a leftover.

WHAT THIS IS *NOT*
------------------
* NOT investment advice, NOT a buy list, NOT a recommendation of any kind.
* NOT a ranking by upside. Rows are ordered by round-trip exit cost, cheapest
  first. That ordering says a position would be cheaper to unwind -- it says
  nothing whatsoever about whether the token will go up, and it must never be
  read as a preference ordering.
* NOT a safety certificate. A `PASS` means only "not obviously untradeable on
  liquidity and cost grounds". See the WHAT THIS DOES NOT TELL YOU footer, which
  is printed on every run for exactly this reason.
* NOT a scanner for winners. The base rate for brand-new tokens going to zero is
  very high, and nothing in this tool identifies the exceptions.

EXECUTABILITY
-------------
The tokens screened here live on DEXes (Solana/Base/Ethereum AMMs). The
crypto-bot cannot execute on any of them -- it trades Kraken spot and
Bybit-signalled majors. So this is a research/risk tool with `executable=False`
in the spirit of `arbitrage/flash_arb_paper.py`: it exists to characterise a
market honestly and to teach what the costs really are, not to feed an arm.

SAFETY
------
Read-only. No wallet, no private keys, no signing, no order placement. The only
network activity is HTTP GETs issued by `screener_sources`. The only write is
the optional `--json` report file.

USAGE
-----
    python scripts/meme_screener.py --limit 8 --ticket 500
    python scripts/meme_screener.py --source boosted --show-rejected
    python scripts/meme_screener.py --source search --query bonk --chain solana
    python -m scripts.meme_screener --json data/meme_screen.json

Stdlib only. ASCII-only output -- the target console is cp1252 and any
non-ASCII character (emoji, box-drawing, en-dash) will raise
UnicodeEncodeError on print.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import sys
from pathlib import Path

# --------------------------------------------------------------------------
# Import the sibling modules whether this is run as a script
# (`python scripts/meme_screener.py`) or as a module
# (`python -m scripts.meme_screener`). Mirrors the sys.path bootstrap used by
# scripts/lev_perp_v1_macross_universe_research.py.
# --------------------------------------------------------------------------

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    from scripts import screener_risk, screener_sources  # noqa: E402
except ImportError:  # pragma: no cover - fallback for odd invocations
    _HERE = Path(__file__).resolve().parent
    if str(_HERE) not in sys.path:
        sys.path.insert(0, str(_HERE))
    import screener_risk  # type: ignore[no-redef]  # noqa: E402
    import screener_sources  # type: ignore[no-redef]  # noqa: E402


class ScreenerError(RuntimeError):
    """User-facing failure. Printed as a plain message, never as a traceback."""


# --------------------------------------------------------------------------
# Formatting helpers -- ASCII only
# --------------------------------------------------------------------------

ROW_FMT = "{sym:<10} {chain:<8} {age:>7} {liq:>13} {volliq:>8} {rt:>10} {be:>10}  {verdict}"

HEADER = ROW_FMT.format(
    sym="TOKEN",
    chain="CHAIN",
    age="AGE",
    liq="LIQUIDITY",
    volliq="VOL/LIQ",
    rt="RT COST",
    be="BREAKEVEN",
    verdict="VERDICT",
)


def _clip(text: object, width: int) -> str:
    """Truncate to width and strip anything the console cannot encode."""
    s = "" if text is None else str(text)
    s = s.encode("ascii", "replace").decode("ascii")
    s = "".join(ch if ch.isprintable() else " " for ch in s)
    return s if len(s) <= width else s[: width - 1] + "."


_INF = float("inf")


def _finite(value: object) -> float | None:
    """float(value) if it is a real, finite number, else None.

    The cost model legitimately emits `inf` (a pool so thin that no price move
    gets you back to flat) and may emit `None` where the pool shape is not a
    constant-product AMM and the slippage math does not apply. Neither may be
    fed to a `.2f` format spec, and neither may be silently rendered as 0.
    """
    if value is None or isinstance(value, bool):
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    if v != v or v == _INF or v == -_INF:   # NaN or +/-inf
        return None
    return v


def _nonfinite_label(value: object) -> str:
    """How to render a value that is not a finite number."""
    if value is None:
        return "n/a"
    try:
        v = float(value)
    except (TypeError, ValueError):
        return "n/a"
    if v != v:
        return "n/a"
    if v == _INF:
        return "inf"
    if v == -_INF:
        return "-inf"
    return "n/a"


def _usd0(value: object, width: int = 12) -> str:
    """Dollar amount, no cents. The repo owner reads dollars, not percents."""
    v = _finite(value)
    if v is None:
        return _nonfinite_label(value).rjust(width + 1)
    return "$" + format(v, ",.0f").rjust(width)


def _usd2(value: object, width: int = 9) -> str:
    v = _finite(value)
    if v is None:
        return _nonfinite_label(value).rjust(width + 1)
    return "$" + format(v, ",.2f").rjust(width)


def _pct(value: object, width: int = 9) -> str:
    v = _finite(value)
    if v is None:
        return _nonfinite_label(value).rjust(width + 1)
    return format(v, ".2f").rjust(width) + "%"


def _age(hours: float | None) -> str:
    if hours is None:
        return "n/a"
    h = float(hours)
    if h >= 240.0:
        return format(h / 24.0, ".0f") + "d"
    return format(h, ".1f") + "h"


def _ratio(numer: float | None, denom: float | None) -> str:
    try:
        if not denom:
            return "n/a"
        return format(float(numer) / float(denom), ".1f") + "x"
    except (TypeError, ValueError, ZeroDivisionError):
        return "n/a"


def _say(line: str = "") -> None:
    """Print, guaranteed encodable on a cp1252 console."""
    print(line.encode("ascii", "replace").decode("ascii"))


# --------------------------------------------------------------------------
# Tolerant readers for the sibling modules' return shapes
# --------------------------------------------------------------------------

_LIQ_GATE_KEYS = (
    "LIQ_MIN_USD",
    "min_liquidity_usd",
    "min_liquidity",
    "liquidity_usd_min",
    "min_liq_usd",
)


def _base_gates() -> dict | None:
    for name in ("DEFAULT_GATES", "GATES"):
        candidate = getattr(screener_risk, name, None)
        if isinstance(candidate, dict):
            return candidate
    return None


def _gates_override(min_liquidity: float | None) -> dict | None:
    """Return a gates copy with the liquidity floor replaced, or None."""
    if min_liquidity is None:
        return None
    base = _base_gates()
    if base is None:
        raise ScreenerError(
            "--min-liquidity given but screener_risk exposes no gates dict "
            "(DEFAULT_GATES/GATES); cannot override the liquidity gate."
        )
    gates = dict(base)
    for key in _LIQ_GATE_KEYS:
        if key in gates:
            gates[key] = float(min_liquidity)
            return gates
    for key in list(gates):
        kl = key.lower()
        if "liq" in kl and ("min" in kl or "floor" in kl):
            gates[key] = float(min_liquidity)
            return gates
    raise ScreenerError(
        "--min-liquidity given but no liquidity floor found in the "
        "screener_risk gates (keys: " + ", ".join(sorted(gates)) + ")."
    )


def _get(mapping: object, *names, default=None):
    if not isinstance(mapping, dict):
        return default
    for name in names:
        if name in mapping and mapping[name] is not None:
            return mapping[name]
    return default


def _reason_pairs(summary: object, scores: list[dict]) -> list[tuple[str, int]]:
    """Most-common rejection reasons as (reason, count), descending.

    Prefers screener_risk.summarize()'s own tally; falls back to counting the
    REJECT rows locally so the SUMMARY block is never silently empty.
    """
    raw = _get(
        summary,
        "reason_counts",
        "rejection_reasons",
        "reasons",
        "top_reasons",
        "reasons_counts",
        "common_reasons",
    )
    pairs: list[tuple[str, int]] = []
    if isinstance(raw, dict):
        pairs = [(str(k), int(v)) for k, v in raw.items() if isinstance(v, (int, float))]
    elif isinstance(raw, list):
        for item in raw:
            if isinstance(item, (list, tuple)) and len(item) == 2:
                try:
                    pairs.append((str(item[0]), int(item[1])))
                except (TypeError, ValueError):
                    continue
            elif isinstance(item, dict):
                reason = _get(item, "reason", "name", "key")
                count = _get(item, "count", "n", "value")
                if reason is not None and isinstance(count, (int, float)):
                    pairs.append((str(reason), int(count)))

    if not pairs:
        tally: dict[str, int] = {}
        for sc in scores:
            if str(_get(sc, "verdict", default="")).upper() != "REJECT":
                continue
            for reason in _get(sc, "reasons", default=[]) or []:
                key = str(reason)
                tally[key] = tally.get(key, 0) + 1
        pairs = list(tally.items())

    pairs.sort(key=lambda kv: (-kv[1], kv[0]))
    return pairs


def _json_safe(value):
    """Recursively replace inf/NaN so the report is strict, parseable JSON.

    `json.dump` would otherwise emit bare `Infinity`/`NaN`, which most parsers
    reject. An infinite breakeven becomes the string "infinite" -- it is a real
    finding (no price move recovers the position), not missing data, so it must
    not be flattened to null.
    """
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    if isinstance(value, bool) or value is None or isinstance(value, (int, str)):
        return value
    if isinstance(value, float):
        if value != value:
            return None
        if value == _INF:
            return "infinite"
        if value == -_INF:
            return "-infinite"
        return value
    return value


def _record_dict(record) -> dict:
    try:
        if dataclasses.is_dataclass(record):
            return dataclasses.asdict(record)
    except (TypeError, ValueError):
        pass
    return dict(vars(record)) if hasattr(record, "__dict__") else {}


# --------------------------------------------------------------------------
# Data acquisition
# --------------------------------------------------------------------------


def _collect(args: argparse.Namespace) -> list:
    """Fetch TokenRecords for the requested source. Raises ScreenerError."""
    source = args.source
    limit = max(1, int(args.limit))
    try:
        if source == "search":
            records = screener_sources.search(args.query, limit=limit)
        elif source == "boosted":
            refs = screener_sources.discover_boosted(limit=limit)
            if not refs:
                raise ScreenerError(
                    "No boosted-token refs returned. The DexScreener boosts "
                    "endpoint returned nothing -- most likely a network failure "
                    "or a rate limit. Check connectivity and retry."
                )
            records = screener_sources.fetch_token_records(refs, boosted_refs=refs)
        else:
            refs = screener_sources.discover_new(limit=limit)
            if not refs:
                raise ScreenerError(
                    "No new-token refs returned. The DexScreener token-profiles "
                    "endpoint returned nothing -- most likely a network failure "
                    "or a rate limit. Check connectivity and retry."
                )
            records = screener_sources.fetch_token_records(refs)
    except ScreenerError:
        raise
    except Exception as exc:  # any failure in the data layer, incl. network
        raise ScreenerError(
            "Data source failed ({}: {}). No results were fetched; nothing was "
            "screened.".format(type(exc).__name__, exc)
        ) from exc

    if records is None:
        records = []
    if not isinstance(records, list):
        raise ScreenerError(
            "screener_sources returned {} where a list of TokenRecord was "
            "expected.".format(type(records).__name__)
        )

    wanted = {c.strip().lower() for c in (args.chain or []) if c and c.strip()}
    if wanted:
        records = [r for r in records if str(getattr(r, "chain", "")).lower() in wanted]
    return records[:limit]


def _score_all(records: list, ticket: float, gates: dict | None) -> list[dict]:
    scored: list[dict] = []
    for rec in records:
        try:
            if gates is None:
                result = screener_risk.score(rec, ticket_usd=ticket)
            else:
                result = screener_risk.score(rec, ticket_usd=ticket, gates=gates)
        except Exception as exc:
            # A scoring blow-up is itself a rejection: we could not establish
            # that the token is tradeable, so it does not pass.
            result = {
                "verdict": "REJECT",
                "reasons": ["scoring_error: {}".format(type(exc).__name__)],
                "flags": [],
                "cost": {},
                "metrics": {},
            }
        if not isinstance(result, dict):
            result = {
                "verdict": "REJECT",
                "reasons": ["scoring_error: non-dict result"],
                "flags": [],
                "cost": {},
                "metrics": {},
            }
        result = dict(result)
        result["_record"] = rec
        scored.append(result)
    return scored


# --------------------------------------------------------------------------
# Report
# --------------------------------------------------------------------------


def _cost_fields(entry: dict) -> tuple[object, object]:
    """(total_usd, breakeven_move_pct), with an unknown cost basis forced to n/a.

    If the cost model reports `basis` as anything other than a constant-product
    pool (e.g. a pump.fun bonding curve, where AMM slippage math is simply not
    applicable), the dollar figures are not meaningful and are shown as n/a
    rather than printed as if they were measured.
    """
    cost = entry.get("cost") or {}
    basis = cost.get("basis") if isinstance(cost, dict) else None
    if basis is not None and str(basis).lower() not in ("cpmm", "amm", "pool"):
        return None, None
    return _get(cost, "total_usd"), _get(cost, "breakeven_move_pct")


def _row(entry: dict) -> str:
    rec = entry.get("_record")
    liq = getattr(rec, "liquidity_usd", None)
    vol = getattr(rec, "volume_h24_usd", None)
    total_usd, breakeven = _cost_fields(entry)
    # Prefer the scorer's effective age: it derives one from pair_created_ms
    # when the record's own age_hours is missing.
    age_h = _get(entry.get("metrics"), "age_hours")
    if age_h is None:
        age_h = getattr(rec, "age_hours", None)
    return ROW_FMT.format(
        sym=_clip(getattr(rec, "symbol", "?"), 10),
        chain=_clip(getattr(rec, "chain", "?"), 8),
        age=_age(_finite(age_h)),
        liq=_usd0(liq, 12),
        volliq=_ratio(vol, liq),
        rt=_usd2(total_usd, 9),
        be=_pct(breakeven, 9),
        verdict=_clip(entry.get("verdict", "?"), 8),
    )


def _cost_key(entry: dict) -> float:
    """Sort key: cheapest round trip first. Unknown / infinite cost sorts last,
    never raises on a None-vs-float comparison."""
    value = _finite(_cost_fields(entry)[0])
    return _INF if value is None else value


def _report(args: argparse.Namespace, scored: list[dict], summary: object) -> None:
    passed = [e for e in scored if str(e.get("verdict", "")).upper() == "PASS"]
    rejected = [e for e in scored if str(e.get("verdict", "")).upper() != "PASS"]
    passed.sort(key=_cost_key)
    rejected.sort(key=_cost_key)

    ticket = float(args.ticket)

    _say()
    _say("MEME-COIN RISK SCREENER -- source=%s  ticket=$%s  screened=%d"
         % (args.source, format(ticket, ",.0f"), len(scored)))
    _say("RT COST is the DOLLAR cost to enter AND exit a $%s position "
         "(slippage + fees + gas)." % format(ticket, ",.0f"))
    _say("BREAKEVEN is the price move required just to get that $%s back to flat."
         % format(ticket, ",.0f"))
    _say("Rows are ordered by ROUND-TRIP EXIT COST, cheapest first. That is an "
         "ordering by")
    _say("how expensive the position is to unwind -- it is NOT a ranking by "
         "attractiveness,")
    _say("quality, or expected return, and must not be read as one.")
    _say()

    _say(HEADER)
    _say("-" * len(HEADER))

    if not scored:
        _say("(no tokens returned by the data source)")
    elif args.show_rejected:
        for entry in passed + rejected:
            _say(_row(entry))
    elif passed:
        for entry in passed:
            _say(_row(entry))
    else:
        _say("(no tokens passed the gates -- rerun with --show-rejected to see "
             "all %d rows)" % len(scored))

    # ---- SUMMARY -------------------------------------------------------
    _say()
    _say("SUMMARY")
    _say("-" * len(HEADER))
    _say("  screened : %d" % len(scored))
    _say("  passed   : %d" % len(passed))
    _say("  rejected : %d" % len(rejected))
    if scored:
        _say("  pass rate: %.1f%%" % (100.0 * len(passed) / len(scored)))

    pairs = _reason_pairs(summary, scored)
    if pairs:
        _say()
        _say("  most common rejection reasons:")
        for reason, count in pairs[:12]:
            _say("    %4d  %s" % (count, _clip(reason, 70)))
    elif rejected:
        _say()
        _say("  (rejections recorded no reasons)")

    unpriced = sum(1 for e in scored if _finite(_cost_fields(e)[0]) is None)
    if unpriced:
        _say()
        _say("  %d of %d could not be priced at all (RT COST shown as n/a):"
             % (unpriced, len(scored)))
        _say("  no reported pool, or a venue whose shape is not a "
             "constant-product AMM")
        _say("  (e.g. a pump.fun bonding curve). Unpriceable is strictly worse "
             "than expensive.")

    if passed:
        cheapest = passed[0]
        total = _finite(_cost_fields(cheapest)[0])
        if total is not None:
            _say()
            _say("  cheapest round trip on a $%s ticket: $%s (%s)"
                 % (format(ticket, ",.0f"),
                    format(total, ",.2f"),
                    _clip(getattr(cheapest.get("_record"), "symbol", "?"), 16)))

    # ---- FLAGS ---------------------------------------------------------
    _say()
    _say("FLAGS ON PASSING TOKENS")
    _say("-" * len(HEADER))
    _say("A PASS clears the hard gates only. These are the non-fatal warnings "
         "that came")
    _say("with it. A PASS carrying three flags is not a clean bill of health.")
    _say()
    if not passed:
        _say("  (nothing passed)")
    else:
        for entry in passed:
            rec = entry.get("_record")
            symbol = _clip(getattr(rec, "symbol", "?"), 16)
            chain = _clip(getattr(rec, "chain", "?"), 12)
            flags = [str(f) for f in (entry.get("flags") or [])]
            _say("  %s (%s) -- %d flag(s)" % (symbol, chain, len(flags)))
            if flags:
                for flag in flags:
                    _say("      - %s" % _clip(flag, 90))
            else:
                _say("      - none recorded (absence of flags is not evidence "
                     "of safety)")
            url = getattr(rec, "url", "") or ""
            if url:
                _say("      %s" % _clip(url, 100))

    # ---- FOOTER --------------------------------------------------------
    _say()
    _say("WHAT THIS DOES NOT TELL YOU")
    _say("-" * len(HEADER))
    _say("  - It does NOT read the contract. Honeypot code (you can buy but "
         "cannot sell),")
    _say("    a live mint authority (supply can be inflated at will), and a "
         "freeze authority")
    _say("    (your balance can be frozen) are all invisible here.")
    _say("  - It does NOT verify that liquidity is locked or burned. Reported "
         "liquidity can")
    _say("    be withdrawn in a single transaction by whoever controls the LP.")
    _say("  - It cannot see holder concentration. A token can pass every gate "
         "while one")
    _say("    wallet holds most of the supply.")
    _say("  - Volume and buy/sell counts on new tokens are routinely wash "
         "traded. High")
    _say("    VOL/LIQ may be one wallet trading against itself.")
    _say("  - It cannot detect a rug that has not happened yet. All gates are "
         "backward-looking.")
    _say("  - PASS means only: not obviously untradeable on liquidity and cost "
         "grounds.")
    _say("    It is not a safety rating, not a recommendation, and not advice.")
    _say("  - The base rate for new tokens going to zero is very high. This "
         "tool does not")
    _say("    identify winners and makes no attempt to. Its only job is to show "
         "why most")
    _say("    of these are untradeable before price is even a consideration.")
    _say("  - executable=False: the crypto-bot cannot trade any of these DEX "
         "venues. It")
    _say("    trades Kraken spot / Bybit-signalled majors. This is research "
         "only.")
    _say()


def _write_json(path_str: str, args: argparse.Namespace,
                scored: list[dict], summary: object) -> Path:
    out_path = Path(path_str)
    if not out_path.is_absolute():
        out_path = (Path.cwd() / out_path).resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)

    rows = []
    for entry in scored:
        payload = {k: v for k, v in entry.items() if k != "_record"}
        payload["token"] = _record_dict(entry.get("_record"))
        rows.append(payload)

    doc = {
        "tool": "meme_screener",
        "purpose": "risk filter (downside/tradeability), NOT a buy list",
        "executable": False,
        "not_advice": (
            "Ordering is by round-trip exit cost, not attractiveness. A PASS "
            "means only 'not obviously untradeable on liquidity and cost "
            "grounds'. No contract, LP-lock, or holder-concentration check is "
            "performed."
        ),
        "params": {
            "source": args.source,
            "query": args.query,
            "ticket_usd": float(args.ticket),
            "limit": int(args.limit),
            "chains": list(args.chain or []),
            "min_liquidity_usd": (
                float(args.min_liquidity) if args.min_liquidity is not None else None
            ),
        },
        "summary": summary if isinstance(summary, dict) else {},
        "counts": {
            "screened": len(scored),
            "passed": sum(1 for e in scored
                          if str(e.get("verdict", "")).upper() == "PASS"),
        },
        "results": rows,
    }
    with out_path.open("w", encoding="utf-8") as fh:
        json.dump(_json_safe(doc), fh, indent=2, sort_keys=True, default=str)
    return out_path


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="meme_screener",
        description=(
            "Read-only meme-coin RISK screener. Prices what a position costs to "
            "enter and exit, and rejects tokens that are untradeable on "
            "liquidity/cost grounds. Not investment advice; not a buy list; "
            "does not rank by upside."
        ),
        epilog=(
            "PASS means only 'not obviously untradeable'. No contract, LP-lock, "
            "or holder-concentration check is performed."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--source", choices=("new", "boosted", "search"), default="new",
        help="token population to screen (default: new)",
    )
    parser.add_argument(
        "--query", default=None,
        help="search text; required when --source search",
    )
    parser.add_argument(
        "--ticket", type=float, default=500.0, metavar="USD",
        help="position size the cost model prices, in dollars (default: 500)",
    )
    parser.add_argument(
        "--limit", type=int, default=30, metavar="N",
        help="maximum tokens to screen (default: 30)",
    )
    parser.add_argument(
        "--chain", action="append", default=None, metavar="CHAIN",
        help="only screen this chain (solana|base|ethereum|...); repeatable",
    )
    parser.add_argument(
        "--min-liquidity", type=float, default=None, metavar="USD",
        dest="min_liquidity",
        help="override the liquidity gate, in dollars",
    )
    parser.add_argument(
        "--show-rejected", action="store_true",
        help="print every row, not just the PASS rows plus the summary",
    )
    parser.add_argument(
        "--json", default=None, metavar="PATH", dest="json_path",
        help="also write full results to this JSON file",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.source == "search" and not (args.query or "").strip():
        _say("ERROR: --source search requires --query TEXT.")
        return 2
    if args.ticket <= 0:
        _say("ERROR: --ticket must be a positive dollar amount.")
        return 2
    if args.limit <= 0:
        _say("ERROR: --limit must be at least 1.")
        return 2

    try:
        gates = _gates_override(args.min_liquidity)
        records = _collect(args)
    except ScreenerError as exc:
        _say("ERROR: %s" % exc)
        return 1

    if not records:
        _say()
        _say("No tokens returned for source=%s%s."
             % (args.source,
                (" query=%r" % args.query) if args.source == "search" else ""))
        if args.chain:
            _say("A --chain filter was active (%s); it may have excluded "
                 "everything." % ", ".join(args.chain))
        _say("Nothing was screened. This is not a result about any token.")
        return 1

    scored = _score_all(records, float(args.ticket), gates)

    try:
        summary = screener_risk.summarize([
            {k: v for k, v in e.items() if k != "_record"} for e in scored
        ])
    except Exception as exc:
        _say("WARN: screener_risk.summarize failed (%s: %s); using local counts."
             % (type(exc).__name__, exc))
        summary = {}

    _report(args, scored, summary)

    if args.json_path:
        try:
            out_path = _write_json(args.json_path, args, scored, summary)
            _say("Wrote %s" % out_path)
        except OSError as exc:
            _say("ERROR: could not write --json %s (%s: %s)"
                 % (args.json_path, type(exc).__name__, exc))
            return 1

    # All-rejected is a legitimate, informative outcome -- not an error.
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        _say("Interrupted.")
        sys.exit(130)
    except ScreenerError as exc:
        _say("ERROR: %s" % exc)
        sys.exit(1)
    except Exception as exc:  # never let a traceback be the user-facing output
        _say("ERROR: unexpected failure (%s: %s)" % (type(exc).__name__, exc))
        sys.exit(1)

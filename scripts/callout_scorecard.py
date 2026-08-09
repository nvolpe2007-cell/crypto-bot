#!/usr/bin/env python3
"""Callout scorecard — did any of these alerts actually pay?

WHAT THIS IS
------------
The read side of `callout_ledger.py`. It answers one question about the meme
radar and the ingested channel calls, at each forward horizon:

    "If I had taken every single one of these at my ticket size, where would I
     be in dollars?"

Not "how good were the good ones". Every callout enters the denominator the
moment it is made, including the ones that stopped being quotable -- those are
scored -100%, because a position you cannot exit is a total loss and dropping
them from the sample is how a hit rate gets manufactured.

THE HEADLINE IS NET
-------------------
Returns here are net of the round-trip cost the token was screened against. A
+8% move on a pool needing 12% to break even is a LOSS and is counted as one.
Gross is shown alongside so the gap between the two is visible -- in this repo
that gap has killed every candidate edge so far.

READING IT HONESTLY
-------------------
* n < 20 at a horizon means nothing. Meme returns are violently fat-tailed; a
  handful of samples is a story, not a statistic. The report says so inline.
* A positive median with a negative total is the classic meme distribution:
  most calls slightly up, a few to zero. The dollar total is the one to trust,
  because it is what your account would actually show.
* The caller table needs n>=20 per caller before it separates anybody. Until
  then it is ranking noise.

USAGE
-----
    python scripts/callout_scorecard.py
    python scripts/callout_scorecard.py --horizon 24 --source radar
    python scripts/callout_scorecard.py --callers
    python scripts/callout_scorecard.py --telegram        # push a digest
    python scripts/callout_scorecard.py --list --limit 20
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import callout_ledger as ledger  # noqa: E402

# Below this many scored callouts, any rate or median is noise. Stated inline in
# the report rather than left for the reader to remember.
MIN_MEANINGFUL_N = 20


def _say(line: str = "") -> None:
    print(line.encode("ascii", "replace").decode("ascii"))


def _pct(v, dp: int = 1) -> str:
    return "n/a" if v is None else format(v, "+.%df" % dp) + "%"


def _usd(v, dp: int = 2) -> str:
    return "n/a" if v is None else ("-$" if v < 0 else "$") + format(abs(v), ",.%df" % dp)


def _age_str(ms) -> str:
    if not isinstance(ms, (int, float)):
        return "?"
    dt = datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc)
    return dt.strftime("%m-%d %H:%M")


def _filter(rows: list[dict], args) -> list[dict]:
    out = rows
    if args.source:
        want = args.source.lower()
        out = [c for c in out if str(c.get("source", "")).lower().startswith(want)]
    if args.tier:
        want = args.tier.upper()
        out = [c for c in out if str(c.get("tier", "")).upper() == want]
    return out


def _caveat(n: int) -> str:
    if n == 0:
        return "  (nothing scored yet at this horizon)"
    if n < MIN_MEANINGFUL_N:
        return ("  n=%d is below the %d-sample floor -- this is a story, not a "
                "statistic. Do not size off it." % (n, MIN_MEANINGFUL_N))
    return ""


def _report(rows: list[dict], args) -> None:
    _say()
    _say("CALLOUT SCORECARD -- %d callout(s) on the book" % len(rows))
    _say("Returns are NET of the round-trip cost each token was screened "
         "against.")
    _say("Unquotable at the horizon = -100%, counted, never dropped.")
    _say()

    hdr = ("%-7s %7s %7s %8s %10s %10s %10s %12s"
           % ("HORIZON", "SCORED", "WINS", "HIT", "MED NET", "MED GROSS",
              "WORST", "TOTAL P&L"))
    _say(hdr)
    _say("-" * len(hdr))

    horizons = [args.horizon] if args.horizon else list(ledger.HORIZONS_H)
    for h in horizons:
        card = ledger.scorecard(rows, h, ticket_usd=args.ticket)
        _say("%-7s %7d %7d %8s %10s %10s %10s %12s" % (
            card["horizon"],
            card["scored"],
            card["wins"],
            "n/a" if card["hit_rate_pct"] is None else "%.0f%%" % card["hit_rate_pct"],
            _pct(card["median_net_pct"]),
            _pct(card["median_gross_pct"]),
            _pct(card["worst_net_pct"]),
            _usd(card["total_pnl_usd"]),
        ))

    _say()
    for h in horizons:
        card = ledger.scorecard(rows, h, ticket_usd=args.ticket)
        note = _caveat(card["scored"])
        if note:
            _say("%s:%s" % (card["horizon"], note))
        if card["unreachable"]:
            _say("  %s: %d of %d went unquotable (scored -100%%)"
                 % (card["horizon"], card["unreachable"], card["marked"]))
        if card["unpriced"]:
            _say("  %s: %d marked but net-unpriceable (no cost model -- "
                 "bonding curve or no liquidity object)"
                 % (card["horizon"], card["unpriced"]))

    pending = sum(1 for c in rows if c.get("status") != "complete")
    if pending:
        _say()
        _say("  %d callout(s) still accruing marks." % pending)


def _callers(rows: list[dict], args) -> None:
    table = ledger.caller_table(rows, args.horizon or 24.0)
    _say()
    _say("BY CALLER -- @ %gh, net of cost, best median first"
         % (args.horizon or 24.0))
    _say("A caller reliably net-negative here is one whose subscribers are the "
         "exit.")
    _say()
    hdr = "%-28s %7s %7s %8s %10s %12s" % ("CALLER", "SCORED", "WINS", "HIT",
                                           "MED NET", "TOTAL P&L")
    _say(hdr)
    _say("-" * len(hdr))
    if not table:
        _say("(no callouts recorded yet)")
        return
    for r in table:
        _say("%-28s %7d %7d %8s %10s %12s" % (
            str(r["caller"])[:28],
            r["scored"],
            r["wins"],
            "n/a" if r["hit_rate_pct"] is None else "%.0f%%" % r["hit_rate_pct"],
            _pct(r["median_net_pct"]),
            _usd(r["total_pnl_usd"]),
        ))
    thin = [r for r in table if r["scored"] < MIN_MEANINGFUL_N]
    if thin:
        _say()
        _say("  %d of %d callers have n<%d -- that part of the table is "
             "ranking noise." % (len(thin), len(table), MIN_MEANINGFUL_N))


def _list(rows: list[dict], args) -> None:
    key = ledger._horizon_key(args.horizon or 24.0)
    rows = sorted(rows, key=lambda c: c.get("called_at_ms") or 0, reverse=True)
    _say()
    _say("RECENT CALLOUTS (newest first) -- outcome shown @ %s" % key)
    _say()
    hdr = "%-12s %-11s %-9s %-16s %10s %10s" % ("TOKEN", "WHEN", "TIER",
                                                "SOURCE", "GROSS", "NET")
    _say(hdr)
    _say("-" * len(hdr))
    for c in rows[:args.limit]:
        m = (c.get("marks") or {}).get(key) or {}
        _say("%-12s %-11s %-9s %-16s %10s %10s" % (
            str(c.get("symbol", "?"))[:12],
            _age_str(c.get("called_at_ms")),
            str(c.get("tier", "?"))[:9],
            str(c.get("caller") or c.get("source") or "?")[:16],
            _pct(m.get("ret_pct")),
            _pct(m.get("net_ret_pct")),
        ))


def _telegram_digest(rows: list[dict], args) -> int:
    try:
        from dotenv import load_dotenv
        load_dotenv(ROOT / ".env")
    except ImportError:
        pass
    from src.notifications import TelegramNotifier, _esc

    token, chat = os.getenv("TELEGRAM_BOT_TOKEN", ""), os.getenv("TELEGRAM_CHAT_ID", "")
    if not token or not chat:
        _say("ERROR: TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set.")
        return 1

    h = args.horizon or 24.0
    card = ledger.scorecard(rows, h, ticket_usd=args.ticket)
    lines = [
        "<b>Callout scorecard @ %s</b>" % card["horizon"],
        "Called: <b>%d</b>   Scored: <b>%d</b>" % (card["called"], card["scored"]),
    ]
    if card["scored"]:
        lines += [
            "Hit rate: <b>%s</b>  (%d W / %d L)" % (
                "n/a" if card["hit_rate_pct"] is None else "%.0f%%" % card["hit_rate_pct"],
                card["wins"], card["losses"]),
            "Median net: <b>%s</b>   gross %s" % (
                _pct(card["median_net_pct"]), _pct(card["median_gross_pct"])),
            "Worst: <b>%s</b>" % _pct(card["worst_net_pct"]),
            "",
            "If you had taken every one at %s:" % _usd(
                args.ticket or (rows[0].get("ticket_usd") if rows else None), 0),
            "<b>%s</b>" % _usd(card["total_pnl_usd"]),
        ]
        if card["unreachable"]:
            lines.append("%d went unquotable (scored -100%%)" % card["unreachable"])
        if card["scored"] < MIN_MEANINGFUL_N:
            lines += ["", "<i>n=%d is below the %d-sample floor. This is a "
                          "story, not a statistic -- do not size off it.</i>"
                      % (card["scored"], MIN_MEANINGFUL_N)]
    else:
        lines.append("<i>Nothing has reached this horizon yet.</i>")

    if args.callers:
        table = [r for r in ledger.caller_table(rows, h) if r["scored"]]
        if table:
            lines += ["", "<b>By caller:</b>"]
            for r in table[:10]:
                lines.append("  %s  n=%d  med %s  %s" % (
                    _esc(str(r["caller"])[:20]), r["scored"],
                    _pct(r["median_net_pct"]), _usd(r["total_pnl_usd"], 0)))

    notifier = TelegramNotifier(token, chat, enabled=True, async_safe=False)
    ok = notifier.send_message("\n".join(lines))
    _say("Telegram digest %s." % ("sent" if ok else "FAILED"))
    return 0 if ok else 1


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="callout_scorecard",
        description="Forward-scored results for every meme callout on the book. "
                    "Net of cost; unquotable counts as -100%.",
    )
    p.add_argument("--horizon", type=float, default=None, metavar="H",
                   help="single horizon to report (1, 4, 24, 72). Default: all")
    p.add_argument("--source", default=None,
                   help="filter by source prefix, e.g. 'radar' or 'tg:'")
    p.add_argument("--tier", default=None, choices=("EARLY", "SURVIVED", "early", "survived"),
                   help="filter by tier")
    p.add_argument("--ticket", type=float, default=None, metavar="USD",
                   help="re-price the dollar total at this ticket size "
                        "(default: whatever each callout was screened at)")
    p.add_argument("--callers", action="store_true",
                   help="also show the per-caller table")
    p.add_argument("--list", action="store_true", dest="do_list",
                   help="list individual callouts newest first")
    p.add_argument("--limit", type=int, default=25, metavar="N",
                   help="rows for --list (default: 25)")
    p.add_argument("--telegram", action="store_true",
                   help="send the digest to Telegram instead of printing")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    doc = ledger.load_ledger()
    rows = _filter([c for c in doc.get("callouts", []) if isinstance(c, dict)], args)

    if not rows:
        _say()
        _say("No callouts on the book yet%s."
             % (" matching that filter" if (args.source or args.tier) else ""))
        _say("Run scripts/meme_radar.py (and/or scripts/telegram_ingest.py) "
             "first; results appear here once alerts reach their horizons.")
        return 0

    if args.telegram:
        return _telegram_digest(rows, args)

    _report(rows, args)
    if args.callers:
        _callers(rows, args)
    if args.do_list:
        _list(rows, args)
    _say()
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

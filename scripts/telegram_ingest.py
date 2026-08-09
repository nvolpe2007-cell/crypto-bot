#!/usr/bin/env python3
"""Telegram ingest — read the alpha channels, score the callers.

WHAT THIS IS
------------
The inbound half of the meme stack. It reads messages from Telegram channels you
are already a member of, extracts every contract address posted, screens each
one through the same `screener_risk` gates the radar uses, and writes it to the
same `callout_ledger` tagged with WHO called it.

The point is not to follow the calls. The point is to answer, with a real
sample and real cost accounting:

    "Which of these channels is actually early, and which one is using me as
     exit liquidity?"

A channel whose calls are reliably net-negative at 24h is not unlucky. It is
one where, by the time the message reaches you, the move has already happened
and the people who posted it need somebody to sell to. That is the single most
valuable output here, and you get it from `callout_scorecard.py --callers`.

WHY THIS NEEDS YOUR OWN CREDENTIALS
-----------------------------------
The bot token in .env can only SEND. Reading channel history requires a user
session, which means Telethon and an api_id/api_hash from https://my.telegram.org
(Login > API development tools). That is a one-time interactive login you must
run yourself -- it prompts for your phone, an SMS code, and possibly your 2FA
password, and none of those should ever pass through an agent:

    python scripts/telegram_ingest.py --login

That writes a session file to data/tg_ingest.session. Guard it like a password:
anyone holding it can read your Telegram as you. It is gitignored.

    TELEGRAM_API_ID=...        # from my.telegram.org
    TELEGRAM_API_HASH=...      # from my.telegram.org
    TELEGRAM_INGEST_CHANNELS=chan_one,chan_two   # usernames or t.me links

READ-ONLY, BY CONSTRUCTION
--------------------------
This script only ever calls `iter_messages`. It never sends, joins, leaves,
reacts, or marks anything read. It cannot post to a channel and it cannot move
funds. The only writes are the ledger and its own dedup state.

A NOTE ON WHAT IT SCREENS
-------------------------
Extracting an address from a message is not endorsement of the message. The
screen is run for exactly the same reason as in the radar: to establish what a
position would cost to enter and exit, and to reject the ones that are
untradeable before price is even a consideration. Most calls will REJECT. Those
are still recorded -- a channel that mostly calls untradeable tokens is telling
you something important about the channel.

USAGE
-----
    python scripts/telegram_ingest.py --login              # one-time, interactive
    python scripts/telegram_ingest.py --list-channels      # what you can read
    python scripts/telegram_ingest.py --lookback-h 6 --dry-run
    python scripts/telegram_ingest.py --lookback-h 1 --ticket 100
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import callout_ledger as ledger  # noqa: E402
from scripts import screener_risk, screener_sources  # noqa: E402

SESSION_PATH = ROOT / "data" / "tg_ingest"
STATE_FILE = ROOT / "data" / "tg_ingest_state.json"

# ── address extraction ────────────────────────────────────────────────────────
# Deliberately conservative. A false positive costs an HTTP round trip and a
# "could not resolve" line; a false negative just misses one call. Anything
# ambiguous is left for the URL patterns below, which are unambiguous.

# EVM: 0x + 40 hex. No practical false-positive risk.
_EVM_RE = re.compile(r"\b0x[a-fA-F0-9]{40}\b")

# Solana: base58, 32-44 chars. Base58 excludes 0, O, I and l. This WILL
# occasionally match a long word or a base58 signature, which is why every hit
# is verified against DexScreener before it becomes a callout.
_SOL_RE = re.compile(r"\b[1-9A-HJ-NP-Za-km-z]{32,44}\b")

# Explicit links are the reliable path -- chain and address both stated.
_DEXSCREENER_RE = re.compile(
    r"dexscreener\.com/([a-z]+)/([1-9A-HJ-NP-Za-km-z]{32,44}|0x[a-fA-F0-9]{40})",
    re.I)
_BIRDEYE_RE = re.compile(
    r"birdeye\.so/token/([1-9A-HJ-NP-Za-km-z]{32,44})", re.I)
_PUMPFUN_RE = re.compile(
    r"pump\.fun/(?:coin/)?([1-9A-HJ-NP-Za-km-z]{32,44})", re.I)

# Words that are base58-legal and long enough to trip _SOL_RE. Cheap guard
# against burning an HTTP call on chat.
_SOL_STOPWORDS = {
    "solanasolanasolanasolanasolana",
}


def extract_refs(text: str) -> list[str]:
    """Every plausible "chain:address" ref in one message, order preserved.

    URL forms win because they carry the chain explicitly. Bare addresses are
    inferred: 0x-prefixed goes to ethereum (the resolver will find the token on
    whichever EVM chain actually lists it), everything else to solana.
    """
    if not text:
        return []
    refs: list[str] = []
    seen: set[str] = set()
    # Addresses whose chain a URL stated outright. A later bare-address match on
    # the same string must not re-add it under a guessed chain -- that would
    # screen the same token twice, once against a chain it does not live on.
    claimed: set[str] = set()

    def add(chain: str, addr: str, *, authoritative: bool) -> None:
        if not authoritative and addr.lower() in claimed:
            return
        if authoritative:
            claimed.add(addr.lower())
        ref = "%s:%s" % (chain.lower(), addr)
        if ref.lower() not in seen:
            seen.add(ref.lower())
            refs.append(ref)

    for chain, addr in _DEXSCREENER_RE.findall(text):
        add(chain, addr, authoritative=True)
    for addr in _BIRDEYE_RE.findall(text):
        add("solana", addr, authoritative=True)
    for addr in _PUMPFUN_RE.findall(text):
        add("solana", addr, authoritative=True)
    for addr in _EVM_RE.findall(text):
        add("ethereum", addr, authoritative=False)
    for addr in _SOL_RE.findall(text):
        if addr.lower() in _SOL_STOPWORDS or addr.startswith("0x"):
            continue
        add("solana", addr, authoritative=False)
    return refs


# ── state ─────────────────────────────────────────────────────────────────────


def _say(line: str = "") -> None:
    print(line.encode("ascii", "replace").decode("ascii"))


def _load_state() -> dict:
    if not STATE_FILE.exists():
        return {"channels": {}}
    try:
        with STATE_FILE.open("r", encoding="utf-8") as fh:
            doc = json.load(fh)
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        return {"channels": {}}
    return doc if isinstance(doc, dict) and isinstance(doc.get("channels"), dict) \
        else {"channels": {}}


def _save_state(doc: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2, sort_keys=True, default=str)
    os.replace(tmp, STATE_FILE)


# ── telethon ──────────────────────────────────────────────────────────────────


def _creds() -> tuple[int, str]:
    try:
        from dotenv import load_dotenv
        load_dotenv(ROOT / ".env")
    except ImportError:
        pass
    api_id = os.getenv("TELEGRAM_API_ID", "").strip()
    api_hash = os.getenv("TELEGRAM_API_HASH", "").strip()
    if not api_id or not api_hash:
        raise SystemExit(
            "TELEGRAM_API_ID / TELEGRAM_API_HASH are not set.\n"
            "Get them from https://my.telegram.org (Login > API development "
            "tools) and add them to .env. These are NOT the bot token -- the "
            "bot token can only send, and reading channels needs a user "
            "session."
        )
    try:
        return int(api_id), api_hash
    except ValueError:
        raise SystemExit("TELEGRAM_API_ID must be an integer.")


def _client():
    try:
        from telethon import TelegramClient
    except ImportError:
        raise SystemExit(
            "telethon is not installed. Run:\n"
            "    pip install telethon\n"
            "It is the only way to READ Telegram channels; the bot token in "
            ".env can only send."
        )
    api_id, api_hash = _creds()
    SESSION_PATH.parent.mkdir(parents=True, exist_ok=True)
    return TelegramClient(str(SESSION_PATH), api_id, api_hash)


def do_login() -> int:
    """One-time interactive login. Must be run by a human at a real terminal."""
    client = _client()
    _say("Starting Telegram login. You will be asked for your phone number, "
         "then a code sent to your Telegram app, and your 2FA password if you "
         "have one set.")
    _say("Session will be written to %s -- treat that file like a password."
         % SESSION_PATH.with_suffix(".session"))
    with client:
        me = client.get_me()
        _say("")
        _say("Logged in as: %s (@%s)" % (
            getattr(me, "first_name", "?") or "?",
            getattr(me, "username", None) or "no username"))
    return 0


def do_list_channels(limit: int) -> int:
    client = _client()
    with client:
        _say("")
        _say("%-36s %-14s %s" % ("NAME", "USERNAME", "TYPE"))
        _say("-" * 70)
        n = 0
        for dialog in client.iter_dialogs():
            if not (dialog.is_channel or dialog.is_group):
                continue
            ent = dialog.entity
            _say("%-36s %-14s %s" % (
                str(dialog.name or "?")[:36],
                str(getattr(ent, "username", None) or "-")[:14],
                "channel" if dialog.is_channel else "group"))
            n += 1
            if n >= limit:
                break
        _say("")
        _say("Add the ones you want to TELEGRAM_INGEST_CHANNELS in .env, "
             "comma-separated (use the USERNAME column).")
    return 0


def _channels(args) -> list[str]:
    if args.channel:
        return [c.strip() for c in args.channel if c.strip()]
    raw = os.getenv("TELEGRAM_INGEST_CHANNELS", "")
    out = []
    for part in raw.replace("\n", ",").split(","):
        part = part.strip()
        if not part:
            continue
        part = part.rsplit("/", 1)[-1].lstrip("@")  # accept t.me/foo and @foo
        out.append(part)
    return out


# ── main ingest ───────────────────────────────────────────────────────────────


def ingest(args) -> int:
    channels = _channels(args)
    if not channels:
        _say("No channels configured. Set TELEGRAM_INGEST_CHANNELS in .env or "
             "pass --channel NAME (repeatable).")
        _say("Run --list-channels to see what you can read.")
        return 2

    state = _load_state()
    doc = ledger.load_ledger()
    now_ms = int(time.time() * 1000)
    cutoff_ms = now_ms - float(args.lookback_h) * 3_600_000.0

    client = _client()
    harvested: list[tuple[str, str, int]] = []   # (channel, ref, msg_ms)

    with client:
        for chan in channels:
            node = state.setdefault("channels", {}).setdefault(chan, {})
            last_id = int(node.get("last_msg_id") or 0)
            seen_here = 0
            max_id = last_id
            try:
                for msg in client.iter_messages(chan, limit=args.max_messages):
                    mid = int(getattr(msg, "id", 0) or 0)
                    if mid <= last_id:
                        break
                    max_id = max(max_id, mid)
                    dt = getattr(msg, "date", None)
                    msg_ms = int(dt.timestamp() * 1000) if dt else now_ms
                    if msg_ms < cutoff_ms:
                        break
                    text = (getattr(msg, "message", "") or "")
                    for ref in extract_refs(text):
                        harvested.append((chan, ref, msg_ms))
                        seen_here += 1
            except Exception as exc:
                _say("  %s: could not read (%s: %s)"
                     % (chan, type(exc).__name__, exc))
                continue
            node["last_msg_id"] = max_id
            node["last_run_ms"] = now_ms
            _say("%-24s %d address(es) in the last %gh"
                 % (chan, seen_here, args.lookback_h))

    if not harvested:
        _save_state(state)
        _say("")
        _say("Nothing new to screen.")
        return 0

    # Screen each distinct ref once, but credit every channel that called it --
    # that is precisely the who-was-first comparison the ledger exists for.
    order: list[str] = []
    callers: dict[str, list[tuple[str, int]]] = {}
    for chan, ref, msg_ms in harvested:
        k = ref.lower()
        if k not in callers:
            callers[k] = []
            order.append(ref)
        callers[k].append((chan, msg_ms))

    _say("")
    _say("Resolving %d distinct address(es)..." % len(order))
    try:
        records = screener_sources.fetch_token_records(order[:args.max_tokens])
    except Exception as exc:
        _say("ERROR: resolver failed (%s: %s)" % (type(exc).__name__, exc))
        _save_state(state)
        return 1

    by_key = {"%s:%s" % (str(getattr(r, "chain", "")).lower(),
                         str(getattr(r, "token_address", "")).lower()): r
              for r in (records or [])}

    gates = dict(screener_risk.DEFAULT_GATES)
    if args.early_window is not None:
        gates["AGE_MIN_HOURS"] = float(args.early_window)
    if args.min_liquidity is not None:
        gates["LIQ_MIN_USD"] = float(args.min_liquidity)

    _say("")
    hdr = "%-12s %-10s %-22s %8s %s" % ("TOKEN", "CHAIN", "CALLER", "VERDICT", "WHY")
    _say(hdr)
    _say("-" * 96)

    added = unresolved = 0
    for ref in order[:args.max_tokens]:
        rec = by_key.get(ref.lower())
        if rec is None:
            unresolved += 1
            if args.verbose:
                _say("%-12s %-10s %-22s %8s %s"
                     % ("?", ref.split(":")[0][:10],
                        callers[ref.lower()][0][0][:22], "-",
                        "not a token DexScreener knows (likely a false-positive "
                        "base58 match)"))
            continue

        try:
            screen = screener_risk.score(rec, ticket_usd=float(args.ticket),
                                         gates=gates)
        except Exception as exc:
            _say("%-12s %-10s %-22s %8s %s"
                 % (str(getattr(rec, "symbol", "?"))[:12],
                    str(getattr(rec, "chain", "?"))[:10],
                    callers[ref.lower()][0][0][:22], "ERROR",
                    "%s" % type(exc).__name__))
            continue

        age = ((screen.get("metrics") or {}).get("age_hours"))
        tier = "EARLY" if (age is not None and age < 48.0) else "SURVIVED"

        for chan, msg_ms in callers[ref.lower()]:
            _say("%-12s %-10s %-22s %8s %s" % (
                str(getattr(rec, "symbol", "?"))[:12],
                str(getattr(rec, "chain", "?"))[:10],
                chan[:22],
                screen.get("verdict", "?"),
                ("; ".join(screen.get("reasons") or []) or "cleared the gates")[:44],
            ))
            if args.dry_run:
                continue
            entry = ledger.add_callout(
                doc, record=rec, screen=screen, source="tg:%s" % chan,
                tier=tier, ticket_usd=float(args.ticket), caller=chan,
                trigger="called in %s" % chan,
                # Mark from when the CALL was posted, not when cron happened to
                # read it. Otherwise a channel looks better the later you poll.
                called_at_ms=msg_ms,
            )
            if entry is not None:
                added += 1

    if not args.dry_run:
        ledger.save_ledger(doc)
    _save_state(state)

    _say("")
    _say("%d new callout(s) recorded, %d address(es) did not resolve.%s"
         % (added, unresolved, "  [DRY RUN -- nothing written]" if args.dry_run else ""))
    _say("Scored results land in: python scripts/callout_scorecard.py --callers")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="telegram_ingest",
        description="Read alpha channels you are in, screen every contract "
                    "address posted, and score which callers are actually "
                    "early. Read-only; never posts.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--login", action="store_true",
                   help="one-time interactive login (run this yourself)")
    p.add_argument("--list-channels", action="store_true", dest="list_channels",
                   help="show the channels/groups this account can read")
    p.add_argument("--channel", action="append", default=None, metavar="NAME",
                   help="channel username to read; repeatable. Overrides "
                        "TELEGRAM_INGEST_CHANNELS")
    p.add_argument("--lookback-h", type=float, default=6.0, metavar="H",
                   dest="lookback_h",
                   help="how far back to read on this run (default: 6)")
    p.add_argument("--max-messages", type=int, default=300, metavar="N",
                   dest="max_messages",
                   help="message cap per channel per run (default: 300)")
    p.add_argument("--max-tokens", type=int, default=40, metavar="N",
                   dest="max_tokens",
                   help="cap on addresses resolved per run (default: 40)")
    p.add_argument("--ticket", type=float, default=100.0, metavar="USD",
                   help="position size costs are priced against (default: 100)")
    p.add_argument("--early-window", type=float, default=None, metavar="H",
                   dest="early_window",
                   help="lower the 48h rug-window age gate to H hours. Most "
                        "channel calls are inside it; without this they REJECT "
                        "on age, which is itself an honest finding.")
    p.add_argument("--min-liquidity", type=float, default=None, metavar="USD",
                   dest="min_liquidity", help="override the liquidity floor")
    p.add_argument("--dry-run", action="store_true", dest="dry_run",
                   help="screen and print, write nothing to the ledger")
    p.add_argument("--verbose", "-v", action="store_true",
                   help="also show addresses that did not resolve")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.login:
        return do_login()
    if args.list_channels:
        return do_list_channels(200)
    if args.ticket <= 0:
        _say("ERROR: --ticket must be a positive dollar amount.")
        return 2
    return ingest(args)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        _say("Interrupted.")
        sys.exit(130)
    except SystemExit:
        raise
    except Exception as exc:
        _say("ERROR: unexpected failure (%s: %s)" % (type(exc).__name__, exc))
        sys.exit(1)

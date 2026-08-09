#!/usr/bin/env python3
"""Meme radar — early callouts to Telegram, with a ledger behind every one.

WHAT THIS IS
------------
A cron tick (every few minutes) over the DexScreener new/boosted token feeds. It
does three things the manual `meme_screener.py` CLI cannot:

  1. REMEMBERS. It keeps a rolling snapshot per token across runs, so it can see
     CHANGE -- liquidity being added, trade count accelerating, flow flipping to
     the buy side. A single snapshot cannot express any of that, and change is
     the entire "early" signal. A token is never alerted on its first sighting;
     there is nothing to compare it to yet.
  2. GATES. Every candidate goes through the existing screener_risk gates at the
     caller's real ticket size, so nothing gets called out that costs more to
     round-trip than it could plausibly return.
  3. RECORDS. Every alert is written to the callout ledger and marked forward at
     1h/4h/24h/72h, so within two weeks there is a hit rate instead of a memory
     of the good ones.

WHAT "EARLY" HONESTLY MEANS HERE
--------------------------------
The screener's age gate is 48h, because most rugs and sniper dumps resolve
inside the first two days. This tool does NOT quietly lower that. There are two
tiers, and the tier is printed in every alert:

  SURVIVED  age >= --min-age-hours (default 48). Cleared the rug window. This
            is "early" in the sense of early to a token that has at least
            demonstrated it is not an instant exit-scam -- not early to a launch.
  EARLY     age below the 48h floor, only ever produced when you explicitly pass
            --early-window H. This is inside the rug/snipe window by definition.
            Alerts carry a warning line saying exactly that.

Running with --early-window is a decision to trade inside the window where the
base rate is worst. The tool will do it, and it will tell you every time.

WHAT IT CANNOT DO
-----------------
* It cannot trade. These tokens live on Solana/Base/ETH AMMs; the crypto-bot
  trades Kraken spot and Bybit-signalled majors. Every callout is a manual trade
  in your own wallet. executable=False, in the spirit of the arb arm.
* It cannot read a contract. Honeypots, live mint authority, freeze authority,
  and unlocked LP are all invisible here -- see the footer in meme_screener.py.
* It cannot detect a rug that has not happened yet. Every gate is backward
  looking.
* It does not rank by upside. The delta triggers say "something changed", not
  "this will go up". The base rate for new tokens going to zero is very high and
  nothing in here identifies the exceptions.

SAFETY
------
Read-only with respect to money: HTTP GETs to DexScreener, one Telegram POST per
alert, two JSON files written (radar state + callout ledger). No wallet, no
keys, no signing, no order placement.

USAGE
-----
    python scripts/meme_radar.py --ticket 100 --dry-run
    python scripts/meme_radar.py --ticket 100                  # SURVIVED only
    python scripts/meme_radar.py --ticket 100 --early-window 6 # + EARLY tier
    python scripts/meme_radar.py --marks-only                  # just update marks

Cron: see deploy/meme_radar_cron.txt
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import callout_ledger as ledger  # noqa: E402
from scripts import screener_risk, screener_sources  # noqa: E402

STATE_FILE = ROOT / "data" / "meme_radar_state.json"

# How many polls of history to keep per token. At a 5-minute cron that is ~2h of
# observation, which is enough to measure acceleration without the state file
# growing without bound.
MAX_POLLS = 24

# A token is dropped from state after this long without being seen in the feed.
STATE_TTL_H = 72.0

# ── delta triggers ────────────────────────────────────────────────────────────
# These fire the alert. They are deliberately about CHANGE, and deliberately
# conjunctive with the risk gates: a token must both be tradeable AND be doing
# something new. Neither alone is worth a notification.

# Liquidity added since the oldest retained poll, as a fraction. Real money
# entering the pool is the least fakeable of the available signals -- inflating
# it costs the deployer actual capital that stays at risk in the pool.
LIQ_GROWTH_MIN = 0.25          # +25% pool depth

# Trade-count acceleration: recent-half rate vs older-half rate.
TXN_ACCEL_MIN = 1.60           # 60% faster

# Minimum observation span before any delta is trusted. Two polls 30 seconds
# apart measure noise.
MIN_SPAN_MIN = 12.0

# Buy-side share of recent flow. Below this the "acceleration" is people leaving.
BUY_SHARE_MIN = 0.55


def _now_ms() -> int:
    return int(time.time() * 1000)


def _say(line: str = "") -> None:
    """Print, guaranteed encodable on a cp1252 console (matches meme_screener)."""
    print(line.encode("ascii", "replace").decode("ascii"))


def _f(value) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    if v != v or v in (float("inf"), float("-inf")):
        return None
    return v


# ── state ─────────────────────────────────────────────────────────────────────


def _load_state() -> dict:
    if not STATE_FILE.exists():
        return {"tokens": {}}
    try:
        with STATE_FILE.open("r", encoding="utf-8") as fh:
            doc = json.load(fh)
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        return {"tokens": {}}
    if not isinstance(doc, dict) or not isinstance(doc.get("tokens"), dict):
        return {"tokens": {}}
    return doc


def _save_state(doc: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2, sort_keys=True, default=str)
    os.replace(tmp, STATE_FILE)


def _prune(state: dict, now_ms: int) -> int:
    cutoff = now_ms - STATE_TTL_H * 3_600_000.0
    tokens = state.get("tokens", {})
    stale = [k for k, v in tokens.items()
             if not isinstance(v, dict) or (v.get("last_seen_ms") or 0) < cutoff]
    for k in stale:
        tokens.pop(k, None)
    return len(stale)


def _key(rec) -> str:
    return "%s:%s" % (str(getattr(rec, "chain", "?")).lower(),
                      str(getattr(rec, "token_address", "?")).lower())


def _observe(state: dict, rec, now_ms: int) -> dict:
    """Append this run's snapshot to the token's rolling history."""
    tokens = state.setdefault("tokens", {})
    k = _key(rec)
    node = tokens.get(k)
    if not isinstance(node, dict):
        node = {"first_seen_ms": now_ms, "alerted_ms": None, "polls": []}
        tokens[k] = node
    node["last_seen_ms"] = now_ms
    node["symbol"] = str(getattr(rec, "symbol", "?"))
    node.setdefault("polls", []).append({
        "ms": now_ms,
        "liq": _f(getattr(rec, "liquidity_usd", None)),
        "vol24": _f(getattr(rec, "volume_h24_usd", None)),
        "buys": getattr(rec, "buys_h24", None),
        "sells": getattr(rec, "sells_h24", None),
        "price": _f(getattr(rec, "price_usd", None)),
    })
    node["polls"] = node["polls"][-MAX_POLLS:]
    return node


# ── the delta signal ──────────────────────────────────────────────────────────


def _txn_count(poll: dict) -> float | None:
    b, s = poll.get("buys"), poll.get("sells")
    if b is None and s is None:
        return None
    return float(max(b or 0, 0) + max(s or 0, 0))


def evaluate_delta(node: dict) -> dict:
    """Measure what changed across the retained polls.

    Returns {fired: bool, trigger: str, span_min, liq_growth, txn_accel,
             buy_share, why: [str]} -- `why` explains a non-fire too, so a quiet
    radar can be debugged without guessing.
    """
    out = {"fired": False, "trigger": "", "span_min": 0.0, "liq_growth": None,
           "txn_accel": None, "buy_share": None, "why": []}

    polls = [p for p in (node.get("polls") or []) if isinstance(p, dict)]
    if len(polls) < 2:
        out["why"].append("only %d poll(s): nothing to compare yet" % len(polls))
        return out

    span_min = (polls[-1]["ms"] - polls[0]["ms"]) / 60_000.0
    out["span_min"] = span_min
    if span_min < MIN_SPAN_MIN:
        out["why"].append("observed %.1f min < %.0f min minimum span"
                          % (span_min, MIN_SPAN_MIN))
        return out

    # --- liquidity growth over the whole window -----------------------------
    liq0, liq1 = _f(polls[0].get("liq")), _f(polls[-1].get("liq"))
    if liq0 and liq0 > 0 and liq1 is not None:
        out["liq_growth"] = liq1 / liq0 - 1.0

    # --- trade acceleration: recent half vs older half ----------------------
    # DexScreener reports a rolling 24h count, so the DIFFERENCE between two
    # snapshots is the trades that happened between them. Comparing the rate in
    # the recent half of the window against the older half is what turns a
    # rolling aggregate into an acceleration measurement.
    mid = len(polls) // 2
    old, new = polls[:mid + 1], polls[mid:]

    def _rate(seg: list[dict]) -> float | None:
        if len(seg) < 2:
            return None
        a, b = _txn_count(seg[0]), _txn_count(seg[-1])
        if a is None or b is None:
            return None
        minutes = (seg[-1]["ms"] - seg[0]["ms"]) / 60_000.0
        if minutes <= 0:
            return None
        return max(b - a, 0.0) / minutes

    r_old, r_new = _rate(old), _rate(new)
    if r_old is not None and r_new is not None and r_old > 0:
        out["txn_accel"] = r_new / r_old

    # --- buy share of the flow added during the window ----------------------
    b0, b1 = polls[0].get("buys"), polls[-1].get("buys")
    s0, s1 = polls[0].get("sells"), polls[-1].get("sells")
    if None not in (b0, b1, s0, s1):
        d_buys = max(float(b1) - float(b0), 0.0)
        d_sells = max(float(s1) - float(s0), 0.0)
        if d_buys + d_sells > 0:
            out["buy_share"] = d_buys / (d_buys + d_sells)

    # --- decide -------------------------------------------------------------
    liq_ok = out["liq_growth"] is not None and out["liq_growth"] >= LIQ_GROWTH_MIN
    txn_ok = out["txn_accel"] is not None and out["txn_accel"] >= TXN_ACCEL_MIN
    buy_ok = out["buy_share"] is None or out["buy_share"] >= BUY_SHARE_MIN

    if not (liq_ok or txn_ok):
        out["why"].append(
            "no delta: liq growth %s (need +%.0f%%), txn accel %s (need %.2fx)"
            % ("n/a" if out["liq_growth"] is None else "%+.0f%%" % (out["liq_growth"] * 100),
               LIQ_GROWTH_MIN * 100,
               "n/a" if out["txn_accel"] is None else "%.2fx" % out["txn_accel"],
               TXN_ACCEL_MIN))
        return out

    if not buy_ok:
        out["why"].append(
            "flow is net SELL: buys are %.0f%% of new trades (need %.0f%%) -- "
            "the acceleration is people leaving"
            % (out["buy_share"] * 100, BUY_SHARE_MIN * 100))
        return out

    parts = []
    if liq_ok:
        parts.append("liquidity +%.0f%% in %.0f min" % (out["liq_growth"] * 100, span_min))
    if txn_ok:
        parts.append("trade rate %.1fx" % out["txn_accel"])
    out["trigger"] = ", ".join(parts)
    out["fired"] = True
    return out


# ── delta v2: unique buyers instead of trade counts ───────────────────────────
#
# v1 above measures TRADE COUNT acceleration. That number is trivially inflated:
# one wallet trading against itself produces unlimited "acceleration" at the cost
# of gas. Measured on the cohort 2026-08-09, the p90 pool ran 5.7 trades per
# unique wallet and 3.5% ran over 10x -- so the failure mode is not theoretical.
#
# v2 replaces it with growth in the count of DISTINCT BUYING WALLETS. Faking that
# requires funding and operating n separate wallets rather than looping one, which
# is a real cost that scales with the size of the lie.
#
# v1 is deliberately left intact and unmodified: scripts/meme_paper.py runs a
# pre-registered experiment against it (RESEARCH_2026-08-09_meme_filter_test.md),
# and silently redefining the thing under test would invalidate the very test
# meant to judge this change. Both run side by side until that read-out.

BUYER_GROWTH_MIN = 0.40     # +40% more distinct buyers across the window
MIN_BUYERS_ABS = 8          # below this, "growth" is 2 wallets becoming 3


def evaluate_delta_buyers(node: dict) -> dict:
    """Delta on UNIQUE BUYER growth. Same contract as evaluate_delta().

    Requires unique-wallet counts in the poll history (`buyers`/`sellers`), which
    DexScreener does not publish -- the radar enriches candidates from
    GeckoTerminal before calling this. When those fields are absent it reports
    that plainly and does not fire, rather than silently falling back to the
    trade-count logic it exists to replace.
    """
    out = {"fired": False, "trigger": "", "span_min": 0.0, "liq_growth": None,
           "buyer_growth": None, "buyers_now": None, "buy_share": None,
           "why": []}

    polls = [p for p in (node.get("polls") or []) if isinstance(p, dict)]
    if len(polls) < 2:
        out["why"].append("only %d poll(s): nothing to compare yet" % len(polls))
        return out

    span_min = (polls[-1]["ms"] - polls[0]["ms"]) / 60_000.0
    out["span_min"] = span_min
    if span_min < MIN_SPAN_MIN:
        out["why"].append("observed %.1f min < %.0f min minimum span"
                          % (span_min, MIN_SPAN_MIN))
        return out

    with_buyers = [p for p in polls if p.get("buyers") is not None]
    if len(with_buyers) < 2:
        out["why"].append("no unique-wallet history (%d/%d polls enriched)"
                          % (len(with_buyers), len(polls)))
        return out

    b0 = _f(with_buyers[0].get("buyers"))
    b1 = _f(with_buyers[-1].get("buyers"))
    out["buyers_now"] = b1
    if b0 is not None and b1 is not None and b0 > 0:
        out["buyer_growth"] = b1 / b0 - 1.0

    liq0, liq1 = _f(polls[0].get("liq")), _f(polls[-1].get("liq"))
    if liq0 and liq0 > 0 and liq1 is not None:
        out["liq_growth"] = liq1 / liq0 - 1.0

    s0 = _f(with_buyers[0].get("sellers")) or 0.0
    s1 = _f(with_buyers[-1].get("sellers")) or 0.0
    d_buy = max((b1 or 0.0) - (b0 or 0.0), 0.0)
    d_sell = max(s1 - s0, 0.0)
    if d_buy + d_sell > 0:
        out["buy_share"] = d_buy / (d_buy + d_sell)

    # An absolute floor as well as a rate. Going from 2 distinct buyers to 3 is
    # +50% and means nothing; the rate alone would fire constantly on dust.
    if b1 is not None and b1 < MIN_BUYERS_ABS:
        out["why"].append("only %.0f distinct buyers (need %d) -- too few for a "
                          "growth rate to mean anything" % (b1, MIN_BUYERS_ABS))
        return out

    buyers_ok = out["buyer_growth"] is not None and \
        out["buyer_growth"] >= BUYER_GROWTH_MIN
    liq_ok = out["liq_growth"] is not None and out["liq_growth"] >= LIQ_GROWTH_MIN
    share_ok = out["buy_share"] is None or out["buy_share"] >= BUY_SHARE_MIN

    if not (buyers_ok or liq_ok):
        out["why"].append(
            "no delta: distinct buyers %s (need +%.0f%%), liquidity %s (need +%.0f%%)"
            % ("n/a" if out["buyer_growth"] is None
               else "%+.0f%%" % (out["buyer_growth"] * 100), BUYER_GROWTH_MIN * 100,
               "n/a" if out["liq_growth"] is None
               else "%+.0f%%" % (out["liq_growth"] * 100), LIQ_GROWTH_MIN * 100))
        return out

    if not share_ok:
        out["why"].append(
            "net SELLER flow: only %.0f%% of new distinct wallets were buyers "
            "(need %.0f%%)" % (out["buy_share"] * 100, BUY_SHARE_MIN * 100))
        return out

    parts = []
    if buyers_ok:
        parts.append("distinct buyers +%.0f%% to %.0f in %.0f min"
                     % (out["buyer_growth"] * 100, b1, span_min))
    if liq_ok:
        parts.append("liquidity +%.0f%%" % (out["liq_growth"] * 100))
    out["trigger"] = ", ".join(parts)
    out["fired"] = True
    return out


# ── GeckoTerminal enrichment (unique wallets) ─────────────────────────────────

_GT_MULTI = "https://api.geckoterminal.com/api/v2/networks/%s/pools/multi/%s"
_GT_BATCH = 30

# DexScreener chainId -> GeckoTerminal network id. The two services do NOT use
# the same identifiers, and the endpoint is network-scoped: querying a BSC pool
# under /networks/solana/ returns an empty result set, not an error.
#
# Getting this wrong is silent and severe. The first version of this function
# hardcoded "solana", so every non-Solana candidate failed enrichment, never
# accumulated unique-wallet history, and could never fire v2 -- which would have
# meant the radar quietly filtering on "is this token on Solana" rather than on
# anything about the token. Measured on the boosted feed 2026-08-09: gate-passers
# were 1 Solana + 1 BSC, so half the candidates were affected.
_GT_NETWORK = {
    "solana": "solana",
    "ethereum": "eth",
    "bsc": "bsc",
    "base": "base",
    "polygon": "polygon_pos",
    "arbitrum": "arbitrum",
    "avalanche": "avax",
    "optimism": "optimism",
    "blast": "blast",
    "ton": "ton",
    "tron": "tron",
    "sui": "sui-network",
    "abstract": "abstract",
    "berachain": "berachain",
    "hyperevm": "hyperevm",
}


def fetch_unique_wallets(candidates: list) -> dict:
    """{pair_address: {"buyers": n, "sellers": n}} from GeckoTerminal.

    `candidates` is a list of (chain, pair_address) pairs. DexScreener publishes
    trade counts but not distinct-wallet counts, and its `pairAddress` is the
    same AMM pool address GeckoTerminal keys on (verified 2026-08-09) -- but the
    lookup is network-scoped, so the chain has to travel with the address.

    Only gate-passing candidates are enriched (typically a couple per tick), so
    this costs about one call per chain represented, not per token. Failures
    return nothing for the affected pools: no unique-wallet data means v2 does
    not fire, which is the correct conservative outcome and deliberately NOT a
    fallback to v1 -- falling back would reinstate the trade-count logic v2
    exists to replace, at exactly the moment we cannot verify it.
    """
    out: dict = {}
    by_net: dict[str, list] = {}
    unmapped: set = set()
    for chain, addr in candidates:
        if not addr:
            continue
        net = _GT_NETWORK.get(str(chain or "").lower())
        if net is None:
            unmapped.add(str(chain or "?"))
            continue
        by_net.setdefault(net, []).append(str(addr))

    if unmapped:
        _say("  no GeckoTerminal network mapping for: %s -- those candidates "
             "cannot be enriched and will not fire v2"
             % ", ".join(sorted(unmapped)))

    for net, addrs in by_net.items():
        addrs = list(dict.fromkeys(addrs))
        for i in range(0, len(addrs), _GT_BATCH):
            batch = addrs[i:i + _GT_BATCH]
            try:
                req = urllib.request.Request(
                    _GT_MULTI % (net, ",".join(batch)),
                    headers={"User-Agent": "crypto-bot-radar/1.0",
                             "Accept": "application/json"})
                with urllib.request.urlopen(req, timeout=20) as fh:
                    payload = json.load(fh)
            except Exception as exc:
                _say("  unique-wallet enrichment failed on %s (%s) -- v2 will "
                     "not fire for those this tick" % (net, type(exc).__name__))
                continue
            for item in (payload.get("data") or []):
                attrs = item.get("attributes") or {}
                addr = attrs.get("address")
                h24 = ((attrs.get("transactions") or {}).get("h24") or {})
                if addr and ("buyers" in h24 or "sellers" in h24):
                    # Keyed lowercase. DexScreener returns EVM addresses
                    # checksummed (0x630B9C...) and GeckoTerminal returns them
                    # lowercased (0x630b9c...), so an exact-match lookup silently
                    # misses EVERY EVM pool even when the fetch succeeded.
                    # Solana addresses are base58 and case-significant, but
                    # lowercasing both sides of the comparison is still safe:
                    # the same string maps to the same key on both sides.
                    out[str(addr).lower()] = {"buyers": h24.get("buyers"),
                                              "sellers": h24.get("sellers")}
    return out


# ── telegram ──────────────────────────────────────────────────────────────────


def _notifier():
    """Synchronous notifier, like trade_close_notifier: a one-shot cron process
    must actually wait for delivery before it exits, so the bot's async/queued
    notifier is the wrong one here."""
    try:
        from dotenv import load_dotenv
        load_dotenv(ROOT / ".env")
    except ImportError:
        pass
    from src.notifications import TelegramNotifier
    token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    chat = os.getenv("TELEGRAM_CHAT_ID", "")
    if not token or not chat:
        return None
    return TelegramNotifier(token, chat, enabled=True, async_safe=False)


def _usd(value, dp: int = 0) -> str:
    v = _f(value)
    if v is None:
        return "n/a"
    return "$" + format(v, ",.%df" % dp)


def _age(hours) -> str:
    h = _f(hours)
    if h is None:
        return "n/a"
    if h >= 240:
        return "%.0fd" % (h / 24.0)
    return "%.1fh" % h


def build_alert(rec, screen: dict, delta: dict, tier: str, ticket: float) -> str:
    """The Telegram message. Dollars first -- the round-trip cost and the move
    needed to break even are the two numbers that decide whether the callout is
    actionable at this ticket size, so they go above the fold."""
    from src.notifications import _esc

    cost = screen.get("cost") or {}
    metrics = screen.get("metrics") or {}
    sym = _esc(getattr(rec, "symbol", "?"))
    chain = _esc(getattr(rec, "chain", "?"))
    dex = _esc(getattr(rec, "dex", "?"))

    icon = "[EARLY]" if tier == "EARLY" else "[RADAR]"
    lines = [
        "%s <b>%s</b>  <i>%s / %s</i>" % (icon, sym, chain, dex),
        "Changed: %s" % _esc(delta.get("trigger") or "n/a"),
        "",
        "Liquidity <b>%s</b>   Age <b>%s</b>   FDV %s" % (
            _usd(getattr(rec, "liquidity_usd", None)),
            _age(metrics.get("age_hours")),
            _usd(getattr(rec, "fdv_usd", None)),
        ),
        "On a <b>%s</b> ticket: costs <b>%s</b> round trip," % (
            _usd(ticket), _usd(cost.get("total_usd"), 2)),
        "needs <b>%s</b> just to break even." % (
            "n/a" if _f(cost.get("breakeven_move_pct")) is None
            else "%.1f%%" % _f(cost.get("breakeven_move_pct"))),
    ]

    if tier == "EARLY":
        lines += [
            "",
            "&#9888; <b>Inside the 48h rug/snipe window.</b> This pair has not "
            "survived the period when most rugs and sniper dumps resolve.",
        ]

    flags = [str(f) for f in (screen.get("flags") or [])]
    if flags:
        lines.append("")
        lines.append("<b>Carrying %d flag(s):</b>" % len(flags))
        for f in flags[:5]:
            lines.append("  - %s" % _esc(f))

    lines += [
        "",
        _esc(getattr(rec, "url", "") or ""),
        "",
        "<i>Not a buy signal. Cleared liquidity/cost gates only -- no contract, "
        "LP-lock, or holder-concentration check was done. The bot cannot trade "
        "this; it is a manual wallet trade.</i>",
    ]
    return "\n".join(l for l in lines if l is not None)


# ── forward marks ─────────────────────────────────────────────────────────────


def update_marks(doc: dict, verbose: bool = False) -> tuple[int, int]:
    """Take any due forward marks, and close out any horizon whose window passed.

    Returns (marks_taken, marks_expired).
    """
    now = _now_ms()
    open_rows = [c for c in doc.get("callouts", [])
                 if isinstance(c, dict) and c.get("status") != "complete"]
    if not open_rows:
        return (0, 0)

    # Expire first: it needs no network and must happen even if the fetch fails.
    expired = 0
    for entry in open_rows:
        for h in ledger.expire_stale(entry, now):
            ledger.record_mark(entry, h, price_usd=None, at_ms=now, reachable=False)
            expired += 1
            if verbose:
                _say("  expired %s @ %gh -- no price at the horizon (scored -100%%)"
                     % (entry.get("symbol"), h))

    need = [(e, ledger.due_horizons(e, now)) for e in open_rows]
    need = [(e, hs) for e, hs in need if hs]
    if not need:
        return (0, expired)

    refs = []
    for entry, _ in need:
        chain, addr = entry.get("chain"), entry.get("token_address")
        if chain and addr:
            refs.append("%s:%s" % (chain, addr))

    fresh: dict[str, object] = {}
    if refs:
        try:
            for rec in screener_sources.fetch_token_records(list(dict.fromkeys(refs))):
                fresh[_key(rec)] = rec
        except Exception as exc:
            if verbose:
                _say("  mark fetch failed (%s: %s) -- horizons stay open until "
                     "their grace window closes" % (type(exc).__name__, exc))

    taken = 0
    for entry, horizons in need:
        k = "%s:%s" % (str(entry.get("chain", "")).lower(),
                       str(entry.get("token_address", "")).lower())
        rec = fresh.get(k)
        for h in horizons:
            if rec is None:
                # Leave it open -- expire_stale will close it out if it is still
                # unreachable once the grace window passes. A single failed HTTP
                # call must not be recorded as a token going to zero.
                continue
            m = ledger.record_mark(
                entry, h,
                price_usd=getattr(rec, "price_usd", None),
                liquidity_usd=getattr(rec, "liquidity_usd", None),
                at_ms=now,
            )
            taken += 1
            if verbose:
                net = m.get("net_ret_pct")
                _say("  marked %s @ %gh: %s gross, %s net"
                     % (entry.get("symbol"), h,
                        "n/a" if m.get("ret_pct") is None else "%+.1f%%" % m["ret_pct"],
                        "n/a" if net is None else "%+.1f%%" % net))
    return (taken, expired)


# ── main scan ─────────────────────────────────────────────────────────────────


def scan(args) -> int:
    now = _now_ms()
    state = _load_state()
    doc = ledger.load_ledger()

    # 1. forward marks on everything already called
    taken, expired = update_marks(doc, verbose=args.verbose)
    if taken or expired:
        _say("marks: %d taken, %d expired" % (taken, expired))

    if args.marks_only:
        ledger.save_ledger(doc)
        return 0

    # 2. fetch the candidate population
    try:
        if args.source == "boosted":
            refs = screener_sources.discover_boosted(limit=args.limit)
            records = screener_sources.fetch_token_records(refs, boosted_refs=set(refs))
        else:
            refs = screener_sources.discover_new(limit=args.limit)
            records = screener_sources.fetch_token_records(refs)
    except Exception as exc:
        _say("ERROR: data source failed (%s: %s). Nothing scanned."
             % (type(exc).__name__, exc))
        ledger.save_ledger(doc)
        return 1

    records = records or []
    wanted = {c.strip().lower() for c in (args.chain or []) if c and c.strip()}
    if wanted:
        records = [r for r in records
                   if str(getattr(r, "chain", "")).lower() in wanted]

    # 3. observe, gate, and decide
    early_floor = _f(args.early_window)
    age_gate = early_floor if early_floor is not None else float(args.min_age_hours)
    gates = dict(screener_risk.DEFAULT_GATES)
    gates["AGE_MIN_HOURS"] = age_gate
    if args.min_liquidity is not None:
        gates["LIQ_MIN_USD"] = float(args.min_liquidity)

    notifier = None if args.dry_run else _notifier()
    if not args.dry_run and notifier is None:
        _say("WARN: TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set -- "
             "callouts will be recorded to the ledger but not sent.")

    scanned = passed = fired = sent = 0
    # Pass 1 -- observe and screen everything, collecting the gate-passers.
    # Enrichment is batched, so it cannot happen inline: one GeckoTerminal call
    # covers up to 30 candidates, versus one call each if done in the loop.
    candidates: list = []
    for rec in records:
        scanned += 1
        node = _observe(state, rec, now)

        if node.get("alerted_ms"):
            continue

        try:
            screen = screener_risk.score(rec, ticket_usd=float(args.ticket), gates=gates)
        except Exception as exc:
            if args.verbose:
                _say("  %s: scoring error (%s)" % (getattr(rec, "symbol", "?"),
                                                   type(exc).__name__))
            continue

        if screen.get("verdict") != "PASS":
            if args.verbose:
                _say("  %-12s REJECT  %s" % (getattr(rec, "symbol", "?"),
                                             "; ".join(screen.get("reasons") or [])[:100]))
            continue
        passed += 1
        candidates.append((rec, screen, node))

    # Enrich only the survivors with unique-wallet counts, then patch this tick's
    # poll so the history accumulates across runs.
    if candidates:
        wallets = fetch_unique_wallets(
            [(str(getattr(r, "chain", "") or ""),
              str(getattr(r, "pair_address", "") or "")) for r, _, _ in candidates])
        for rec, _, node in candidates:
            w = wallets.get(str(getattr(rec, "pair_address", "") or "").lower())
            if w and node.get("polls"):
                node["polls"][-1]["buyers"] = w.get("buyers")
                node["polls"][-1]["sellers"] = w.get("sellers")
        if args.verbose:
            _say("  enriched %d/%d candidate(s) with unique-wallet counts"
                 % (len(wallets), len(candidates)))

    # Pass 2 -- the delta decision, on unique buyers.
    for rec, screen, node in candidates:
        delta = evaluate_delta_buyers(node)
        if not delta["fired"]:
            if args.verbose:
                _say("  %-12s PASS but quiet: %s"
                     % (getattr(rec, "symbol", "?"), "; ".join(delta["why"])))
            continue
        fired += 1

        age = _f((screen.get("metrics") or {}).get("age_hours"))
        tier = "EARLY" if (age is not None and age < float(args.min_age_hours)) \
            else "SURVIVED"

        msg = build_alert(rec, screen, delta, tier, float(args.ticket))
        _say("")
        _say("CALLOUT %s %s (%s) -- %s"
             % (tier, getattr(rec, "symbol", "?"), getattr(rec, "chain", "?"),
                delta["trigger"]))

        entry = ledger.add_callout(
            doc, record=rec, screen=screen, source="radar", tier=tier,
            ticket_usd=float(args.ticket), trigger=delta["trigger"],
            called_at_ms=now,
        )
        if entry is None and args.verbose:
            _say("  (already in the ledger -- not re-alerting)")

        if args.dry_run:
            _say(msg)
        elif notifier is not None and entry is not None:
            if notifier.send_message(msg):
                sent += 1
            else:
                _say("  WARN: Telegram send failed for %s"
                     % getattr(rec, "symbol", "?"))

        node["alerted_ms"] = now

    pruned = _prune(state, now)
    _save_state(state)
    ledger.save_ledger(doc)

    _say("")
    _say("scanned %d  passed gates %d  delta fired %d  sent %d  (state pruned %d)"
         % (scanned, passed, fired, sent, pruned))
    if scanned and not passed:
        _say("Nothing cleared the gates. That is the normal outcome, not a fault.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="meme_radar",
        description=(
            "Early meme-coin callouts to Telegram, gated on tradeability and "
            "recorded to a forward-scored ledger. Not investment advice; not a "
            "buy list; the bot cannot execute any of these."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--ticket", type=float, default=100.0, metavar="USD",
                   help="position size all costs are priced against (default: 100)")
    p.add_argument("--source", choices=("new", "boosted"), default="new",
                   help="token population to poll (default: new)")
    p.add_argument("--limit", type=int, default=30, metavar="N",
                   help="max tokens per poll (default: 30)")
    p.add_argument("--chain", action="append", default=None, metavar="CHAIN",
                   help="only scan this chain; repeatable")
    p.add_argument("--min-age-hours", type=float, default=48.0, metavar="H",
                   dest="min_age_hours",
                   help="rug-window floor for the SURVIVED tier (default: 48)")
    p.add_argument("--early-window", type=float, default=None, metavar="H",
                   dest="early_window",
                   help="ALSO alert on pairs younger than the 48h rug window, "
                        "down to H hours old. These are tagged EARLY and carry "
                        "an explicit warning. Off by default.")
    p.add_argument("--min-liquidity", type=float, default=None, metavar="USD",
                   dest="min_liquidity", help="override the liquidity floor")
    p.add_argument("--marks-only", action="store_true", dest="marks_only",
                   help="only update forward marks; do not scan for new callouts")
    p.add_argument("--dry-run", action="store_true", dest="dry_run",
                   help="print alerts instead of sending them")
    p.add_argument("--verbose", "-v", action="store_true",
                   help="explain every rejection and every quiet token")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.ticket <= 0:
        _say("ERROR: --ticket must be a positive dollar amount.")
        return 2
    if args.early_window is not None and args.early_window >= args.min_age_hours:
        _say("ERROR: --early-window %.1fh is not below the %.1fh rug-window "
             "floor, so it would do nothing. Use a smaller value."
             % (args.early_window, args.min_age_hours))
        return 2
    return scan(args)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        _say("Interrupted.")
        sys.exit(130)
    except Exception as exc:
        _say("ERROR: unexpected failure (%s: %s)" % (type(exc).__name__, exc))
        sys.exit(1)

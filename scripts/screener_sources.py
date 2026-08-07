"""Data layer for the meme-coin RISK SCREENER.

WHAT THIS IS
------------
A read-only fetch layer over the public DexScreener API. It pulls objective,
observable facts about newly-listed DEX tokens -- liquidity, 24h volume, FDV,
market cap, pair age, buy/sell counts, and how many pools the token trades in --
and normalises them into `TokenRecord` objects for downstream rug-risk scoring.

No API key. No wallet. No private keys. No order placement. Nothing in this
module can move funds; it only issues HTTP GETs.

WHAT THIS IS *NOT*
------------------
* It is NOT a buy signal, alpha source, or recommendation engine. Nothing here
  ranks tokens by upside, and nothing here should ever be made to. The only
  legitimate use is downside/risk characterisation: "how easily could this rug,
  and how thin is the exit?"
* The numbers are NOT trustworthy in the way exchange data is. DexScreener
  aggregates self-reported on-chain indexer data. On brand-new tokens, wash
  trading (volume inflated by the deployer trading against themselves), fake or
  instantly-removable liquidity, and spoofed buy/sell counts are the norm rather
  than the exception. Treat every field as a claim, not a fact.
* `boosted=True` means somebody PAID DexScreener to promote the listing. That is
  a marketing spend by an anonymous deployer -- it is a RISK signal, not an
  endorsement, and must never be scored as a positive.
* High liquidity does not mean withdrawable liquidity, and an unlocked or
  unburned LP can be pulled in a single transaction. This module cannot see LP
  lock status, mint authority, or holder concentration.

Stdlib only, by design -- the repo keeps tools like this dependency-free.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Iterable

__all__ = [
    "TokenRecord",
    "discover_new",
    "discover_boosted",
    "fetch_token_records",
    "search",
]

# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------

API_BASE = "https://api.dexscreener.com"

PROFILES_URL = f"{API_BASE}/token-profiles/latest/v1"
BOOSTS_URL = f"{API_BASE}/token-boosts/latest/v1"
TOKENS_URL = f"{API_BASE}/latest/dex/tokens/{{address}}"
SEARCH_URL = f"{API_BASE}/latest/dex/search"

USER_AGENT = "crypto-bot-screener/1.0 (read-only risk screener; stdlib urllib)"
TIMEOUT_S = 10.0
RETRIES = 2
POLITE_SLEEP_S = 0.2


# --------------------------------------------------------------------------
# Public record type
# --------------------------------------------------------------------------


@dataclass
class TokenRecord:
    """One token, represented by its deepest-liquidity pool.

    Every numeric field is an *unverified claim* sourced from DexScreener.
    Missing/garbage upstream values degrade to 0.0 or None rather than raising.
    """

    symbol: str
    name: str
    chain: str
    dex: str
    token_address: str
    pair_address: str
    url: str
    liquidity_usd: float = 0.0
    volume_h24_usd: float = 0.0
    fdv_usd: float | None = None
    market_cap_usd: float | None = None
    pair_created_ms: int | None = None
    age_hours: float | None = None
    price_usd: float | None = None
    price_change_h24: float | None = None
    buys_h24: int = 0
    sells_h24: int = 0
    pair_count: int = 0
    boosted: bool = False


# --------------------------------------------------------------------------
# Defensive coercion helpers
#
# DexScreener returns numbers as ints, floats, numeric strings, nulls, or omits
# the key entirely -- and occasionally nests a dict where a scalar was expected.
# Nothing below is allowed to raise.
# --------------------------------------------------------------------------


def _as_float(value: Any, default: float | None = None) -> float | None:
    if value is None or isinstance(value, bool):
        return default
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    if out != out or out in (float("inf"), float("-inf")):  # NaN / inf
        return default
    return out


def _as_int(value: Any, default: int = 0) -> int:
    out = _as_float(value, None)
    if out is None:
        return default
    try:
        return int(out)
    except (TypeError, ValueError, OverflowError):
        return default


def _as_str(value: Any, default: str = "") -> str:
    if value is None:
        return default
    if isinstance(value, str):
        return value
    try:
        return str(value)
    except Exception:  # pragma: no cover - str() on anything sane won't fail
        return default


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _dig(obj: Any, *keys: str) -> Any:
    """Walk nested dicts, returning None the moment the path breaks."""
    cur = obj
    for key in keys:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
    return cur


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------


def _http_get_json(url: str) -> Any | None:
    """GET + parse JSON. Returns None on persistent failure -- never raises."""
    last_err: Exception | None = None
    for attempt in range(RETRIES):
        if attempt:
            time.sleep(POLITE_SLEEP_S * (attempt + 1))
        try:
            req = urllib.request.Request(
                url,
                headers={
                    "User-Agent": USER_AGENT,
                    "Accept": "application/json",
                },
                method="GET",
            )
            with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
                raw = resp.read()
            if not raw:
                return None
            return json.loads(raw.decode("utf-8", errors="replace"))
        except urllib.error.HTTPError as exc:
            last_err = exc
            # 4xx other than rate-limit won't fix themselves; stop early.
            if exc.code not in (408, 429) and 400 <= exc.code < 500:
                break
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
            last_err = exc
        except Exception as exc:  # belt and braces: caller must never crash
            last_err = exc
            break
    if last_err is not None:
        _warn(f"request failed: {url} -> {type(last_err).__name__}: {last_err}")
    return None


def _warn(message: str) -> None:
    """Single choke point for diagnostics. ASCII only (cp1252 consoles)."""
    print(f"[screener_sources] WARN {message}".encode("ascii", "replace").decode("ascii"))


# --------------------------------------------------------------------------
# Refs  ("chain:token_address")
# --------------------------------------------------------------------------


def _make_ref(chain: Any, address: Any) -> str | None:
    chain_s = _as_str(chain).strip()
    addr_s = _as_str(address).strip()
    if not chain_s or not addr_s:
        return None
    return f"{chain_s}:{addr_s}"


def _split_ref(ref: str) -> tuple[str, str] | None:
    if not isinstance(ref, str) or ":" not in ref:
        return None
    chain, _, address = ref.partition(":")
    chain, address = chain.strip(), address.strip()
    if not chain or not address:
        return None
    return chain, address


def _refs_from_listing(url: str, limit: int) -> list[str]:
    """Shared shape for token-profiles and token-boosts: a list of records
    carrying `chainId` + `tokenAddress`. De-duplicated, order preserved."""
    if limit <= 0:
        return []
    payload = _http_get_json(url)
    # Both endpoints return a bare list, but tolerate a wrapped object.
    if isinstance(payload, dict):
        for key in ("data", "tokens", "results"):
            if isinstance(payload.get(key), list):
                payload = payload[key]
                break
    if not isinstance(payload, list):
        return []

    refs: list[str] = []
    seen: set[str] = set()
    for item in payload:
        if not isinstance(item, dict):
            continue
        ref = _make_ref(item.get("chainId"), item.get("tokenAddress"))
        if ref is None or ref in seen:
            continue
        seen.add(ref)
        refs.append(ref)
        if len(refs) >= limit:
            break
    return refs


def discover_new(limit: int = 30) -> list[str]:
    """Newest token profiles. Returns "chain:token_address" refs.

    Being newly profiled says nothing about quality -- these are simply the
    most recent listings, which is exactly the population with the highest
    base-rate of rugs.
    """
    return _refs_from_listing(PROFILES_URL, limit)


def discover_boosted(limit: int = 30) -> list[str]:
    """Tokens whose listing was PAID-promoted. Returns refs.

    Paying for promotion is a marketing expense incurred by an anonymous
    deployer on a brand-new token. Score it as risk, never as validation.
    """
    return _refs_from_listing(BOOSTS_URL, limit)


# --------------------------------------------------------------------------
# Pair -> TokenRecord
# --------------------------------------------------------------------------


def _pair_liquidity(pair: Any) -> float:
    return _as_float(_dig(pair, "liquidity", "usd"), 0.0) or 0.0


def _best_pair(pairs: Iterable[Any]) -> dict[str, Any] | None:
    """Representative pool = deepest USD liquidity.

    A token whose liquidity is smeared across many shallow pools, or that lives
    in exactly one pool, is a materially different risk profile -- `pair_count`
    on the record preserves that, so this only picks the *representative*.
    """
    best: dict[str, Any] | None = None
    best_liq = -1.0
    for pair in pairs:
        if not isinstance(pair, dict):
            continue
        liq = _pair_liquidity(pair)
        if liq > best_liq:
            best_liq = liq
            best = pair
    return best


def _record_from_pair(
    pair: dict[str, Any],
    *,
    pair_count: int,
    boosted: bool = False,
    token_address: str = "",
    now_ms: int | None = None,
) -> TokenRecord | None:
    """Normalise one pair dict. Returns None only if it is unusable."""
    try:
        base = _as_dict(pair.get("baseToken"))

        address = _as_str(base.get("address")) or _as_str(token_address)
        if not address:
            return None

        created_raw = _as_float(pair.get("pairCreatedAt"), None)
        created_ms: int | None = None
        age_hours: float | None = None
        if created_raw is not None and created_raw > 0:
            created_ms = int(created_raw)
            if now_ms is None:
                now_ms = int(time.time() * 1000)
            age_hours = (now_ms - created_ms) / 3_600_000.0

        txns_h24 = _as_dict(_dig(pair, "txns", "h24"))

        return TokenRecord(
            symbol=_as_str(base.get("symbol"), "?"),
            name=_as_str(base.get("name"), "?"),
            chain=_as_str(pair.get("chainId"), "?"),
            dex=_as_str(pair.get("dexId"), "?"),
            token_address=address,
            pair_address=_as_str(pair.get("pairAddress")),
            url=_as_str(pair.get("url")),
            liquidity_usd=_pair_liquidity(pair),
            volume_h24_usd=_as_float(_dig(pair, "volume", "h24"), 0.0) or 0.0,
            fdv_usd=_as_float(pair.get("fdv"), None),
            market_cap_usd=_as_float(pair.get("marketCap"), None),
            pair_created_ms=created_ms,
            age_hours=age_hours,
            price_usd=_as_float(pair.get("priceUsd"), None),
            price_change_h24=_as_float(_dig(pair, "priceChange", "h24"), None),
            buys_h24=_as_int(txns_h24.get("buys"), 0),
            sells_h24=_as_int(txns_h24.get("sells"), 0),
            pair_count=max(int(pair_count), 0),
            boosted=bool(boosted),
        )
    except Exception as exc:  # a malformed record must never kill the sweep
        _warn(f"skipped malformed pair: {type(exc).__name__}: {exc}")
        return None


def _pairs_payload(payload: Any) -> list[Any]:
    if isinstance(payload, dict):
        pairs = payload.get("pairs")
        if isinstance(pairs, list):
            return pairs
    if isinstance(payload, list):
        return payload
    return []


# --------------------------------------------------------------------------
# Public fetchers
# --------------------------------------------------------------------------


def fetch_token_records(
    refs: list[str],
    boosted_refs: set[str] | None = None,
) -> list[TokenRecord]:
    """Resolve "chain:token_address" refs into TokenRecords.

    One HTTP call per token, with a polite pause between them. Refs that fail
    to resolve are skipped with a warning; the rest are still returned.
    """
    if not refs:
        return []
    boosted_set = boosted_refs or set()

    records: list[TokenRecord] = []
    seen_addresses: set[str] = set()

    for index, ref in enumerate(refs):
        parsed = _split_ref(ref)
        if parsed is None:
            _warn(f"ignoring bad ref: {ref!r}")
            continue
        chain, address = parsed
        key = f"{chain}:{address}".lower()
        if key in seen_addresses:
            continue
        seen_addresses.add(key)

        if index:
            time.sleep(POLITE_SLEEP_S)

        payload = _http_get_json(TOKENS_URL.format(address=urllib.parse.quote(address, safe="")))
        pairs = _pairs_payload(payload)

        # The tokens endpoint can return pools from other chains for an address
        # that collides across chains -- keep only the requested chain if any
        # such pool exists, otherwise fall back to everything returned.
        on_chain = [
            p for p in pairs
            if isinstance(p, dict) and _as_str(p.get("chainId")).lower() == chain.lower()
        ]
        candidates = on_chain or [p for p in pairs if isinstance(p, dict)]
        if not candidates:
            _warn(f"no pairs for {ref}")
            continue

        best = _best_pair(candidates)
        if best is None:
            continue

        record = _record_from_pair(
            best,
            pair_count=len(candidates),
            boosted=(ref in boosted_set) or (key in {r.lower() for r in boosted_set}),
            token_address=address,
        )
        if record is not None:
            records.append(record)

    return records


def search(query: str, limit: int = 30) -> list[TokenRecord]:
    """Free-text search, collapsed to one record per token.

    Pools are grouped by (chain, base token address); each group contributes its
    deepest-liquidity pool, with `pair_count` set to the group size. Results are
    ordered by liquidity descending -- that is a *data quality* ordering (deeper
    pools are marginally harder to fake), never a ranking of desirability.
    """
    query = _as_str(query).strip()
    if not query or limit <= 0:
        return []

    url = f"{SEARCH_URL}?{urllib.parse.urlencode({'q': query})}"
    pairs = _pairs_payload(_http_get_json(url))
    if not pairs:
        return []

    groups: dict[str, list[dict[str, Any]]] = {}
    order: list[str] = []
    for pair in pairs:
        if not isinstance(pair, dict):
            continue
        chain = _as_str(pair.get("chainId")).lower()
        address = _as_str(_dig(pair, "baseToken", "address")).lower()
        if not address:
            continue
        key = f"{chain}:{address}"
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(pair)

    now_ms = int(time.time() * 1000)
    records: list[TokenRecord] = []
    for key in order:
        group = groups[key]
        best = _best_pair(group)
        if best is None:
            continue
        record = _record_from_pair(best, pair_count=len(group), now_ms=now_ms)
        if record is not None:
            records.append(record)

    records.sort(key=lambda r: r.liquidity_usd, reverse=True)
    return records[:limit]


# --------------------------------------------------------------------------
# Smoke test  (ASCII-only output: this repo is run from a cp1252 console)
# --------------------------------------------------------------------------


def _fmt_usd(value: float | None) -> str:
    if value is None:
        return "n/a"
    if value >= 1_000_000:
        return f"${value / 1_000_000:,.2f}M"
    if value >= 1_000:
        return f"${value / 1_000:,.1f}k"
    return f"${value:,.2f}"


def _fmt_age(hours: float | None) -> str:
    if hours is None:
        return "n/a"
    if hours < 48:
        return f"{hours:.1f}h"
    return f"{hours / 24:.1f}d"


def _print_record(rec: TokenRecord) -> None:
    flags = []
    if rec.boosted:
        flags.append("PAID-BOOST")
    if rec.pair_count == 1:
        flags.append("SINGLE-POOL")
    if rec.liquidity_usd > 0 and rec.volume_h24_usd / rec.liquidity_usd > 10:
        flags.append("VOL>>LIQ")
    flag_s = (" [" + ",".join(flags) + "]") if flags else ""

    print(f"  {rec.symbol:<12} {rec.chain}/{rec.dex}{flag_s}")
    print(f"      name={rec.name[:48]}")
    print(
        f"      liq={_fmt_usd(rec.liquidity_usd)}  vol24h={_fmt_usd(rec.volume_h24_usd)}"
        f"  fdv={_fmt_usd(rec.fdv_usd)}  mcap={_fmt_usd(rec.market_cap_usd)}"
    )
    print(
        f"      age={_fmt_age(rec.age_hours)}  pools={rec.pair_count}"
        f"  buys={rec.buys_h24} sells={rec.sells_h24}"
        f"  chg24h={'n/a' if rec.price_change_h24 is None else f'{rec.price_change_h24:+.1f}%'}"
    )
    print(f"      {rec.url or '(no url)'}")


def _smoke() -> None:
    print("screener_sources smoke test")
    print("RISK DATA ONLY -- not a buy signal. Figures are self-reported by")
    print("indexers; wash volume and fake liquidity are common on new tokens.")
    print("-" * 72)

    refs = discover_new(limit=5)
    print(f"discover_new(5) -> {len(refs)} refs")
    for ref in refs:
        print(f"  {ref}")

    boosted = set(discover_boosted(limit=30))
    print(f"discover_boosted(30) -> {len(boosted)} refs")

    print("-" * 72)
    records = fetch_token_records(refs, boosted_refs=boosted)
    print(f"fetch_token_records -> {len(records)} records")
    for rec in records:
        _print_record(rec)

    print("-" * 72)
    hits = search("SOL", limit=3)
    print(f"search('SOL', 3) -> {len(hits)} records")
    for rec in hits:
        _print_record(rec)

    print("-" * 72)
    print("done.")


if __name__ == "__main__":
    _smoke()

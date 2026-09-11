"""Thin, read-only wrapper over Deribit's public REST API.

Two endpoints, no API key (these are public market-data endpoints, no
authentication required):

  get_book_summary_by_currency -- one call returns OI + mark IV + underlying
    price for EVERY active option on the currency in one shot (~1000
    instruments for BTC). This is why the module doesn't fetch per-instrument
    greeks: it would be ~1000 calls for data this one call already implies.

  get_historical_volatility -- Deribit's own realized-volatility index,
    hourly, annualized %. Used as the "historical volatility" side of the
    HV-vs-IV comparison; this is Deribit's computed series, not independently
    recomputed here.
"""
from __future__ import annotations

import time
import urllib.error
import urllib.request
import json
from dataclasses import dataclass

BASE_URL = "https://www.deribit.com/api/v2/public"


@dataclass(frozen=True)
class OptionQuote:
    instrument_name: str
    strike: float
    option_type: str  # "call" | "put"
    expiration_timestamp_ms: int
    open_interest: float
    mark_iv_pct: float  # annualized, e.g. 68.82 means 68.82%
    underlying_price: float


def _get(path: str, params: dict, timeout: float = 10.0, retries: int = 3) -> dict:
    qs = "&".join(f"{k}={v}" for k, v in params.items())
    url = f"{BASE_URL}/{path}?{qs}"
    last_err: Exception | None = None
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(url, timeout=timeout) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
            if "result" not in payload:
                raise RuntimeError(f"Deribit {path}: no 'result' in response: {payload}")
            return payload["result"]
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            last_err = exc
            if attempt < retries - 1:
                time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"Deribit {path} failed after {retries} attempts: {last_err}")


def fetch_option_chain(currency: str = "BTC") -> list[OptionQuote]:
    """All active options for `currency`, parsed from the instrument_name
    (Deribit encodes strike/type in it: 'BTC-25SEP26-125000-P') since
    book-summary doesn't repeat those fields structurally."""
    rows = _get("get_book_summary_by_currency", {"currency": currency, "kind": "option"})
    quotes: list[OptionQuote] = []
    for row in rows:
        oi = row.get("open_interest")
        iv = row.get("mark_iv")
        spot = row.get("underlying_price")
        if oi is None or iv is None or spot is None or oi <= 0:
            continue  # no open interest -> no dealer exposure to attribute
        try:
            _, expiry_str, strike_str, opt_type = row["instrument_name"].split("-")
        except ValueError:
            continue  # combo/exotic instrument name shape -- skip, not a vanilla option
        quotes.append(
            OptionQuote(
                instrument_name=row["instrument_name"],
                strike=float(strike_str),
                option_type="call" if opt_type == "C" else "put",
                expiration_timestamp_ms=_parse_deribit_expiry(expiry_str),
                open_interest=float(oi),
                mark_iv_pct=float(iv),
                underlying_price=float(spot),
            )
        )
    return quotes


def _parse_deribit_expiry(expiry_str: str) -> int:
    """'25SEP26' -> ms timestamp at 08:00 UTC (Deribit's standard expiry time)."""
    import calendar
    from datetime import datetime, timezone

    dt = datetime.strptime(expiry_str, "%d%b%y").replace(
        hour=8, minute=0, second=0, tzinfo=timezone.utc
    )
    return calendar.timegm(dt.timetuple()) * 1000


def fetch_historical_volatility(currency: str = "BTC") -> list[tuple[int, float]]:
    """[(timestamp_ms, annualized_vol_pct), ...], most recent last."""
    rows = _get("get_historical_volatility", {"currency": currency})
    return [(int(ts), float(v)) for ts, v in rows]


def latest_historical_volatility(currency: str = "BTC") -> float | None:
    series = fetch_historical_volatility(currency)
    return series[-1][1] if series else None

import re
from datetime import datetime, timezone

from scripts.gex_to_backquant import build_text
from src.gex.deribit_client import OptionQuote

AS_OF = datetime(2026, 9, 28, 5, 0, tzinfo=timezone.utc)
DAY = 24 * 3600 * 1000
NOW_MS = int(AS_OF.timestamp() * 1000)


def _q(strike, kind, oi, days):
    return OptionQuote(f"BTC-X-{strike}-{kind[0].upper()}", strike, kind, NOW_MS + int(days * DAY), oi, 50.0, 100_000.0)


CHAIN = [
    _q(110_000, "call", 900, 30), _q(105_000, "call", 300, 30),
    _q(90_000, "put", 800, 30), _q(95_000, "put", 200, 30),
    _q(101_000, "call", 50, 0.5), _q(99_000, "put", 60, 0.5),
    _q(100_000, "call", 100, 2), _q(100_000, "put", 100, 2),
]


def pine_parse_dollar(txt, key):
    """Python mirror of the indicator's parse_dollar(): first `key`, then first '$' number."""
    i = txt.find(key)
    if i < 0:
        return None
    sub = txt[i + len(key):]
    m = re.match(r"[^$]*\$([\d,.]+)", sub)
    return float(m.group(1).replace(",", "")) if m else None


def test_every_key_the_indicator_reads_is_present_and_numeric():
    txt = build_text(CHAIN, AS_OF, "BTC")
    for key in ("HVL:", "Call Resistance:", "Put Support:", "0DTE HVL:", "0DTE Call:",
                "0DTE Put:", "Max Pain:", "Expected Move:"):
        assert pine_parse_dollar(txt, key) is not None, key


def test_all_expiry_hvl_is_found_before_the_0dte_line_that_contains_it():
    txt = build_text(CHAIN, AS_OF, "BTC")
    assert txt.find("HVL:") < txt.find("0DTE HVL:")
    assert txt.find("HVL:") != txt.find("0DTE HVL:") + len("0DTE ")


def test_walls_have_the_expected_sign():
    txt = build_text(CHAIN, AS_OF, "BTC")
    assert pine_parse_dollar(txt, "Call Resistance:") == 110_000  # biggest call OI
    assert pine_parse_dollar(txt, "Put Support:") == 90_000       # biggest put OI


def test_top10_blocks_are_terminated_for_the_line_scanner():
    txt = build_text(CHAIN, AS_OF, "BTC")
    for header in ("All-Expiry GEX Top 10", "0DTE GEX Top 10"):
        block = txt[txt.find(header):].split("\n")
        assert any("━" in ln for ln in block[1:13])

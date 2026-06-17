"""Tests for the dependency-free candlestick PNG renderer (src/chart_render.py)."""

import base64
import struct

import src.chart_render as cr


def _bars(n=160, start=100.0):
    """Synthetic ascending-ish OHLC daily bars."""
    bars = []
    px = start
    for i in range(n):
        o = px
        c = px * (1.01 if i % 3 else 0.995)
        h = max(o, c) * 1.01
        l = min(o, c) * 0.99
        bars.append({"t": 1_000_000 + i * 86_400, "o": o, "h": h, "l": l, "c": c})
        px = c
    return bars


def _png_dims(b64: str):
    """Decode a base64 PNG header and return (width, height), asserting it's a PNG."""
    raw = base64.b64decode(b64)
    assert raw[:8] == b"\x89PNG\r\n\x1a\n"          # PNG signature
    assert raw[12:16] == b"IHDR"
    w, h = struct.unpack(">II", raw[16:24])
    return w, h


def test_renders_valid_png_of_expected_size():
    out = cr.render_candles(_bars(), title="BTC")
    assert isinstance(out, str) and out
    assert _png_dims(out) == (cr.W, cr.H)


def test_handles_fewer_than_max_bars():
    out = cr.render_candles(_bars(40), title="ETH")          # < MAX_BARS but >= 10
    assert out and _png_dims(out) == (cr.W, cr.H)


def test_returns_none_on_too_few_bars():
    assert cr.render_candles(_bars(5)) is None               # below the 10-bar floor


def test_returns_none_on_missing_ohlc_keys():
    closes_only = [{"t": i, "c": 100.0 + i} for i in range(50)]
    assert cr.render_candles(closes_only) is None            # no o/h/l → fail-safe


def test_returns_none_on_degenerate_flat_series():
    flat = [{"t": i, "o": 100.0, "h": 100.0, "l": 100.0, "c": 100.0} for i in range(50)]
    # zero price range → cannot scale; must fail safe, not divide by zero
    assert cr.render_candles(flat) is None


def test_never_raises_on_garbage():
    assert cr.render_candles([{"o": "x", "h": None, "l": 1, "c": 2}] * 20) is None

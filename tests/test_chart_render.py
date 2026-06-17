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


# ── custom SMA periods + weekly resampling + multi-timeframe ──────────────────

def test_render_with_custom_sma_periods():
    out = cr.render_candles(_bars(120), title="BTC", sma_periods=(13, 26, 52))
    assert out and _png_dims(out) == (cr.W, cr.H)


def test_resample_weekly_buckets_seven_days():
    daily = _bars(70)                                   # 70 daily → 10 weekly buckets
    wk = cr.resample_weekly(daily)
    assert len(wk) == 10
    # first weekly bar: open=first day's open, close=7th day's close, high=max of 7
    assert wk[0]["o"] == daily[0]["o"]
    assert wk[0]["c"] == daily[6]["c"]
    assert wk[0]["h"] == max(b["h"] for b in daily[:7])
    assert wk[0]["l"] == min(b["l"] for b in daily[:7])


def test_resample_weekly_handles_partial_last_week():
    wk = cr.resample_weekly(_bars(73))                  # 73 → 10 full + 1 partial = 11
    assert len(wk) == 11
    assert wk[-1]["c"] == _bars(73)[-1]["c"]            # last close carried


def test_multi_timeframe_returns_labeled_weekly_and_daily():
    out = cr.render_multi_timeframe(_bars(400), title="BTC")
    assert len(out) == 2
    labels = [lbl for lbl, _ in out]
    assert "WEEKLY" in labels[0] and "DAILY" in labels[1]   # weekly first (dominant trend)
    for _, b64 in out:
        assert _png_dims(b64) == (cr.W, cr.H)


def test_multi_timeframe_failsafe_on_thin_data():
    # too few bars for even a weekly chart → empty list, never raises
    assert cr.render_multi_timeframe(_bars(8), title="BTC") == []

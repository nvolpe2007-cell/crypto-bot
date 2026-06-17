"""
Dependency-free candlestick chart renderer → base64 PNG, for the AI brain's eyes.

The brain reads exact levels from the JSON snapshot; this gives it the *shape* —
trend structure, consolidations, support/resistance, higher-highs/lower-lows — so
it can reason over chart PATTERNS the way a discretionary trader would. We render
candlesticks + SMA50/100/200 overlays onto a numpy canvas and encode a PNG with
stdlib zlib (no matplotlib/Pillow — numpy is already a core dep, so the VPS deploy
stays lean). Everything is wrapped fail-safe: any error returns None and the brain
simply runs text-only, exactly as before.
"""
from __future__ import annotations

import base64
import logging
import struct
import zlib
from typing import List, Dict, Optional, Sequence

import numpy as np

logger = logging.getLogger(__name__)

W, H = 760, 420                 # canvas size
PAD_L, PAD_R, PAD_T, PAD_B = 8, 8, 8, 8
MAX_BARS = 140                  # ~4-5 months of daily candles: enough structure to read

# colors (R, G, B)
BG = (255, 255, 255)
UP = (0, 158, 84)
DOWN = (214, 48, 49)
SMA_COLORS = [(30, 90, 220), (230, 145, 0), (150, 30, 185)]   # short, mid, long
DAILY_SMAS = (50, 100, 200)         # daily chart: ~2-7mo / 3-7mo / long-term
WEEKLY_SMAS = (13, 26, 52)          # weekly chart: ~quarter / half / full year
GRID = (232, 232, 232)


def _png_bytes(arr: np.ndarray) -> bytes:
    """Encode an (H, W, 3) uint8 array as a PNG (truecolor, 8-bit) using stdlib zlib."""
    h, w, _ = arr.shape
    rows = bytearray()
    for y in range(h):
        rows.append(0)                          # filter type 0 (None) per scanline
        rows.extend(arr[y].tobytes())

    def chunk(typ: bytes, data: bytes) -> bytes:
        crc = zlib.crc32(typ + data) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + typ + data + struct.pack(">I", crc)

    sig = b"\x89PNG\r\n\x1a\n"
    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)   # bit depth 8, color type 2 (RGB)
    idat = zlib.compress(bytes(rows), 9)
    return sig + chunk(b"IHDR", ihdr) + chunk(b"IDAT", idat) + chunk(b"IEND", b"")


def _vline(arr, x, y0, y1, color):
    if x < 0 or x >= arr.shape[1]:
        return
    y0, y1 = sorted((int(y0), int(y1)))
    y0 = max(0, y0); y1 = min(arr.shape[0] - 1, y1)
    if y1 >= y0:
        arr[y0:y1 + 1, x] = color


def _hline(arr, y, x0, x1, color):
    if y < 0 or y >= arr.shape[0]:
        return
    x0, x1 = sorted((int(x0), int(x1)))
    x0 = max(0, x0); x1 = min(arr.shape[1] - 1, x1)
    if x1 >= x0:
        arr[y, x0:x1 + 1] = color


def _rect(arr, x0, x1, y0, y1, color):
    x0, x1 = sorted((int(x0), int(x1)))
    y0, y1 = sorted((int(y0), int(y1)))
    x0 = max(0, x0); x1 = min(arr.shape[1] - 1, x1)
    y0 = max(0, y0); y1 = min(arr.shape[0] - 1, y1)
    if x1 >= x0 and y1 >= y0:
        arr[y0:y1 + 1, x0:x1 + 1] = color


def _polyline(arr, xs: Sequence[int], ys: Sequence[float], color):
    """Draw a polyline through (x, y) points, skipping NaN y (warm-up region)."""
    prev = None
    for x, y in zip(xs, ys):
        if y is None or (isinstance(y, float) and np.isnan(y)):
            prev = None
            continue
        p = (int(x), int(y))
        if prev is not None:
            _line(arr, prev[0], prev[1], p[0], p[1], color)
        prev = p


def _line(arr, x0, y0, x1, y1, color):
    """Bresenham line."""
    x0, y0, x1, y1 = int(x0), int(y0), int(x1), int(y1)
    dx, dy = abs(x1 - x0), -abs(y1 - y0)
    sx = 1 if x0 < x1 else -1
    sy = 1 if y0 < y1 else -1
    err = dx + dy
    h, w = arr.shape[0], arr.shape[1]
    while True:
        if 0 <= x0 < w and 0 <= y0 < h:
            arr[y0, x0] = color
        if x0 == x1 and y0 == y1:
            break
        e2 = 2 * err
        if e2 >= dy:
            err += dy; x0 += sx
        if e2 <= dx:
            err += dx; y0 += sy


def _sma(closes: np.ndarray, n: int) -> np.ndarray:
    out = np.full(len(closes), np.nan)
    if len(closes) >= n:
        c = np.cumsum(np.insert(closes, 0, 0.0))
        out[n - 1:] = (c[n:] - c[:-n]) / n
    return out


def resample_weekly(daily_bars: List[Dict]) -> List[Dict]:
    """Aggregate daily OHLC bars into weekly bars (7-day buckets, oldest→newest).
    open=first, high=max, low=min, close=last of each bucket. Needs 'o','h','l','c'."""
    bars = [b for b in daily_bars if all(k in b for k in ("o", "h", "l", "c"))]
    weekly = []
    for i in range(0, len(bars), 7):
        chunk = bars[i:i + 7]
        if not chunk:
            continue
        weekly.append({
            "t": chunk[0].get("t", i),
            "o": float(chunk[0]["o"]),
            "h": max(float(b["h"]) for b in chunk),
            "l": min(float(b["l"]) for b in chunk),
            "c": float(chunk[-1]["c"]),
        })
    return weekly


def render_multi_timeframe(daily_bars: List[Dict], title: str = "") -> List[tuple]:
    """Render a (weekly, daily) pair of labeled charts for one symbol — the way a
    discretionary trader checks the dominant trend (weekly) then the entry (daily).
    Returns a list of (label, base64) for whichever timeframes rendered (each is
    independently fail-safe; an empty list means neither could be drawn)."""
    out = []
    weekly = render_candles(resample_weekly(daily_bars), title=f"{title} weekly",
                            sma_periods=WEEKLY_SMAS)
    if weekly:
        out.append((f"{title} WEEKLY (~2yr; SMA13/26/52w — the dominant trend)", weekly))
    daily = render_candles(daily_bars, title=f"{title} daily", sma_periods=DAILY_SMAS)
    if daily:
        out.append((f"{title} DAILY (~140d; SMA50/100/200d — recent action & entry)", daily))
    return out


def render_candles(bars: List[Dict], title: str = "",
                   sma_periods: Sequence[int] = DAILY_SMAS) -> Optional[str]:
    """Render up to the last MAX_BARS OHLC bars as a base64 PNG candlestick chart
    with SMA overlays. `bars` items need 'o','h','l','c' (floats); `sma_periods` are
    drawn in SMA_COLORS order (short→long). Returns a base64 string (no data: prefix)
    or None on any failure (brain falls back to text)."""
    try:
        bars = [b for b in bars if all(k in b for k in ("o", "h", "l", "c"))]
        if len(bars) < 10:
            return None
        full_close = np.array([float(b["c"]) for b in bars])
        # SMAs computed on the FULL history, then sliced to the visible window so the
        # longest line is correct even when we only show the last MAX_BARS candles.
        sma_full = {n: _sma(full_close, n) for n in sma_periods}
        view = bars[-MAX_BARS:]
        o = np.array([float(b["o"]) for b in view])
        hi = np.array([float(b["h"]) for b in view])
        lo = np.array([float(b["l"]) for b in view])
        cl = np.array([float(b["c"]) for b in view])
        sma_view = {n: sma_full[n][-len(view):] for n in sma_periods}

        # price range spans candles AND any visible SMA point
        ymax = float(hi.max()); ymin = float(lo.min())
        for n in sma_periods:
            s = sma_view[n]
            s = s[~np.isnan(s)]
            if len(s):
                ymax = max(ymax, float(s.max())); ymin = min(ymin, float(s.min()))
        if not np.isfinite(ymax) or not np.isfinite(ymin) or ymax <= ymin:
            return None
        pad = (ymax - ymin) * 0.04
        ymax += pad; ymin -= pad

        arr = np.full((H, W, 3), BG, dtype=np.uint8)
        x0, x1 = PAD_L, W - PAD_R
        ytop, ybot = PAD_T, H - PAD_B
        n = len(view)
        slot = (x1 - x0) / n
        body_w = max(1, int(slot * 0.6))

        def py(price: float) -> int:
            return int(ytop + (ymax - price) / (ymax - ymin) * (ybot - ytop))

        # light horizontal gridlines (quartiles) for visual scale
        for f in (0.25, 0.5, 0.75):
            _hline(arr, py(ymin + (ymax - ymin) * f), x0, x1, GRID)

        # SMA overlays (drawn under candles), longest first so the shortest sits on top
        xs = [int(x0 + (i + 0.5) * slot) for i in range(n)]
        for idx in reversed(range(len(sma_periods))):
            nn = sma_periods[idx]
            color = SMA_COLORS[idx] if idx < len(SMA_COLORS) else SMA_COLORS[-1]
            ys = [py(v) if not np.isnan(v) else None for v in sma_view[nn]]
            _polyline(arr, xs, ys, color)

        # candles
        for i in range(n):
            cx = int(x0 + (i + 0.5) * slot)
            up = cl[i] >= o[i]
            color = UP if up else DOWN
            _vline(arr, cx, py(hi[i]), py(lo[i]), color)            # wick
            yo, yc = py(o[i]), py(cl[i])
            if abs(yo - yc) < 1:                                     # doji → 1px body
                _hline(arr, yo, cx - body_w // 2, cx + body_w // 2, color)
            else:
                _rect(arr, cx - body_w // 2, cx + body_w // 2, yo, yc, color)

        return base64.b64encode(_png_bytes(arr)).decode("ascii")
    except Exception as e:                       # never break the brain over a chart
        logger.warning("[chart_render] render failed for %s: %s", title, e)
        return None

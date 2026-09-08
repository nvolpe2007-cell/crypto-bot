"""
Chart rendering for ORB setup alerts — a candlestick PNG with the opening
range, entry, stop, and target marked, for the Telegram photo caption.

matplotlib is a LAZY import (only orb_alerts.py's alerting path needs it; the
core backtest/metrics/strategy modules stay dependency-light per the repo's
existing pattern). `pip install matplotlib` to enable.
"""
from __future__ import annotations

import io

import pandas as pd


def render_orb_chart(symbol: str, bars: pd.DataFrame, or_high: float, or_low: float,
                     entry: float, stop: float, target: float, side: str = "long"
                     ) -> bytes:
    """Render today's intraday bars as candlesticks with OR/entry/stop/target
    lines. Returns PNG bytes. Raises ImportError if matplotlib isn't installed
    (caller decides how to handle that — see orb_alerts.py)."""
    import matplotlib
    matplotlib.use("Agg")  # headless — no display needed on a server
    import matplotlib.pyplot as plt
    import matplotlib.dates as mdates

    fig, ax = plt.subplots(figsize=(9, 5), dpi=140)
    xs = mdates.date2num(bars.index.to_pydatetime())
    width = (xs[1] - xs[0]) * 0.6 if len(xs) > 1 else 0.0006
    up = bars["close"] >= bars["open"]
    for i, (x, row) in enumerate(zip(xs, bars.itertuples())):
        color = "#22a06b" if up.iloc[i] else "#d1453b"
        ax.vlines(x, row.low, row.high, color=color, linewidth=1)
        body_lo, body_hi = min(row.open, row.close), max(row.open, row.close)
        ax.add_patch(plt.Rectangle((x - width / 2, body_lo), width,
                                   max(body_hi - body_lo, 1e-6), color=color))

    ax.axhline(or_high, color="#888", linestyle="--", linewidth=1, label=f"OR high {or_high:.2f}")
    ax.axhline(or_low, color="#888", linestyle=":", linewidth=1, label=f"OR low {or_low:.2f}")
    ax.axhline(entry, color="#1a73e8", linewidth=1.4, label=f"entry {entry:.2f}")
    ax.axhline(stop, color="#d1453b", linewidth=1.4, label=f"stop {stop:.2f}")
    ax.axhline(target, color="#22a06b", linewidth=1.4, label=f"target {target:.2f}")

    ax.set_title(f"{symbol} — ORB {side.upper()} setup")
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))
    fig.autofmt_xdate()
    ax.legend(loc="upper left", fontsize=8, framealpha=0.9)
    ax.set_ylabel("price")
    fig.tight_layout()

    buf = io.BytesIO()
    fig.savefig(buf, format="png")
    plt.close(fig)
    return buf.getvalue()

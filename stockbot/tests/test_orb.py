"""ORB strategy unit tests — crafted single-day scenarios (no look-ahead, gap-aware,
EOD-flat, cost-charged)."""
import pandas as pd

from stockbot.strategy import ORBConfig, opening_range, simulate_day

CFG = ORBConfig(or_minutes=15, direction="long", target_r=2.0, cost_bps_per_side=2.0)


def _day(bars):
    """bars: list of (timestr, open, high, low, close)."""
    idx = pd.DatetimeIndex([pd.Timestamp(f"2026-01-05 {t}") for t, *_ in bars])
    data = [(o, h, l, c, 1000.0) for _, o, h, l, c in bars]
    return pd.DataFrame(data, columns=["open", "high", "low", "close", "volume"], index=idx)


def test_opening_range_is_first_15min():
    day = _day([
        ("09:30", 100, 101, 99, 100),
        ("09:35", 100, 101, 99.5, 100.5),
        ("09:40", 100.5, 101, 100, 100.8),   # last OR bar (minute-of-day < 9:45)
        ("09:45", 100.8, 103, 100.5, 102),   # outside OR — must not widen the range
    ])
    hi, lo = opening_range(day, CFG)
    assert hi == 101.0 and lo == 99.0


def test_long_breakout_hits_target():
    day = _day([
        ("09:30", 100, 101, 99, 100),
        ("09:35", 100, 101, 99.5, 100.5),
        ("09:40", 100.5, 101, 100, 100.8),   # OR high=101, low=99 → risk=2, target=105
        ("09:45", 100.8, 102, 100.5, 101.8),  # breakout: entry at 101
        ("09:50", 101.8, 106, 101, 105.5),    # high≥105 → target exit
    ])
    t = simulate_day(day, CFG, "TST")
    assert t is not None and t.side == "long"
    assert t.entry_px == 101.0 and t.exit_px == 105.0 and t.reason == "target"
    # net = gross − round-trip cost (2bps/side → 0.0004)
    assert abs(t.gross_ret - (105 - 101) / 101) < 1e-6   # gross_ret stored to 6dp
    assert abs(t.net_ret - (t.gross_ret - 0.0004)) < 1e-6


def test_no_breakout_no_trade():
    day = _day([
        ("09:30", 100, 101, 99, 100),
        ("09:35", 100, 100.8, 99.5, 100.2),
        ("09:40", 100.2, 100.9, 100, 100.5),  # OR high=101
        ("09:45", 100.5, 100.9, 100, 100.3),  # never exceeds 101 → no entry
        ("09:50", 100.3, 100.7, 99.8, 100.1),
    ])
    assert simulate_day(day, CFG, "TST") is None


def test_eod_flat_when_neither_stop_nor_target():
    day = _day([
        ("09:30", 100, 101, 99, 100),
        ("09:35", 100, 101, 99.5, 100.5),
        ("09:40", 100.5, 101, 100, 100.8),    # target=105, stop=99
        ("09:45", 100.8, 101.5, 100.5, 101.2),  # entry 101
        ("09:50", 101.2, 102, 100.5, 101.5),
        ("15:55", 101.5, 102, 101, 101.7),    # last bar, no stop/target → EOD close
    ])
    t = simulate_day(day, CFG, "TST")
    assert t.reason == "eod" and t.exit_px == 101.7


def test_gap_through_stop_fills_at_open():
    day = _day([
        ("09:30", 100, 101, 99, 100),
        ("09:35", 100, 101, 99.5, 100.5),
        ("09:40", 100.5, 101, 100, 100.8),    # stop=99
        ("09:45", 100.8, 101.5, 100.5, 101.2),  # entry 101
        ("09:50", 98, 98.2, 97, 97.5),        # gaps below stop → fills at open 98 (<99)
    ])
    t = simulate_day(day, CFG, "TST")
    assert t.reason == "stop" and t.exit_px == 98.0      # worse than the 99 stop


def test_no_entry_after_cutoff():
    day = _day([
        ("09:30", 100, 101, 99, 100),
        ("09:35", 100, 101, 99.5, 100.5),
        ("09:40", 100.5, 101, 100, 100.8),
        ("15:35", 100.8, 105, 100, 104),      # breakout, but after 15:30 cutoff
    ])
    assert simulate_day(day, CFG, "TST") is None


def test_short_side_when_enabled():
    cfg = ORBConfig(or_minutes=15, direction="short", target_r=2.0)
    day = _day([
        ("09:30", 100, 101, 99, 100),
        ("09:35", 100, 100.5, 99, 99.5),
        ("09:40", 99.5, 100, 99, 99.2),       # OR low=99
        ("09:45", 99.2, 99.5, 98, 98.3),      # breaks below 99 → short entry at 99
        ("09:50", 98.3, 98.5, 94, 95),        # low≤95 (target=99-2*2) → target
    ])
    t = simulate_day(day, cfg, "TST")
    assert t is not None and t.side == "short" and t.reason == "target"
    assert t.gross_ret > 0      # a winning short is positive return

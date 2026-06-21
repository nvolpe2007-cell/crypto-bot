"""Alpaca paper runner tests — decision logic + orchestration, via a FAKE client
(no SDK, no keys, no network)."""
from datetime import datetime, time
from zoneinfo import ZoneInfo

import pandas as pd

from stockbot.alpaca import Account
from stockbot.live_paper import decide, run_step, ET
from stockbot.strategy import ORBConfig

CFG = ORBConfig(or_minutes=15, direction="long", target_r=2.0)


def _bars(rows):
    """rows: (HH:MM, o,h,l,c) on 2026-01-05, tz-naive."""
    idx = pd.DatetimeIndex([pd.Timestamp(f"2026-01-05 {t}") for t, *_ in rows])
    return pd.DataFrame([(o, h, l, c, 1000.0) for _, o, h, l, c in rows],
                        columns=["open", "high", "low", "close", "volume"], index=idx)


_BREAKOUT = _bars([
    ("09:30", 100, 101, 99, 100),
    ("09:35", 100, 101, 99.5, 100.5),
    ("09:40", 100.5, 101, 100, 100.8),     # OR high=101, low=99
    ("09:45", 100.8, 102, 100.5, 101.8),   # breaks above 101
])


def test_decide_breakout_makes_long_intent():
    it = decide(_BREAKOUT, CFG, time(9, 45), notional=2000)
    assert it is not None and it.side == "long"
    assert it.stop == 99.0 and it.target == 101.0 + 2 * (101.0 - 99.0)  # entry101 → 105
    assert it.qty == int(2000 // 101.0)


def test_decide_none_before_or_window_closes():
    assert decide(_BREAKOUT, CFG, time(9, 35), notional=2000) is None


def test_decide_none_after_cutoff():
    assert decide(_BREAKOUT, CFG, time(15, 45), notional=2000) is None


def test_decide_none_when_notional_too_small():
    assert decide(_BREAKOUT, CFG, time(9, 45), notional=50) is None   # <1 share


def test_decide_requires_target():
    cfg = ORBConfig(or_minutes=15, target_r=None)
    assert decide(_BREAKOUT, CFG.__class__(or_minutes=15, target_r=None),
                  time(9, 45), 2000) is None


# ── fake Alpaca client ────────────────────────────────────────────────────────

class _Fake:
    def __init__(self, bars, position=None, equity=100000.0):
        self._bars = bars
        self._position = position
        self._equity = equity
        self.orders = []
        self.closed = False

    def available(self):
        return True

    def account(self):
        return Account(self._equity, self._equity, self._equity * 2)

    def get_position(self, symbol):
        return self._position

    def recent_bars(self, symbol, timeframe="5Min", limit=120):
        return self._bars

    def submit_bracket(self, symbol, qty, side, take_profit, stop_loss):
        self.orders.append((symbol, qty, side, take_profit, stop_loss))
        return "ord_1"

    def close_all(self):
        self.closed = True
        return True


def _now(hhmm):
    h, m = hhmm
    return datetime(2026, 1, 5, h, m, tzinfo=ET)


def test_run_step_submits_on_breakout_and_is_idempotent():
    fake = _Fake(_BREAKOUT)
    state = {"traded": {}}
    r1 = run_step(fake, ["SPY"], CFG, state, _now((9, 45)), notional=2000)
    assert r1["status"] == "ok" and len(fake.orders) == 1
    assert fake.orders[0][0] == "SPY" and fake.orders[0][2] == "long"
    # same day, run again → no second order (one trade/symbol/day)
    run_step(fake, ["SPY"], CFG, state, _now((9, 50)), notional=2000)
    assert len(fake.orders) == 1


def test_run_step_skips_when_already_in_position():
    from stockbot.alpaca import Position
    fake = _Fake(_BREAKOUT, position=Position("SPY", 10, 101.0, 0.0, "long"))
    r = run_step(fake, ["SPY"], CFG, {"traded": {}}, _now((9, 45)), notional=2000)
    assert fake.orders == [] and r["status"] == "ok"


def test_run_step_eod_flattens_once():
    fake = _Fake(_BREAKOUT)
    state = {"traded": {}}
    r = run_step(fake, ["SPY"], CFG, state, _now((16, 0)), notional=2000)
    assert fake.closed is True and r["status"] == "eod" and "eod_flat" in r["actions"]
    # second EOD tick same day → no repeat
    fake.closed = False
    run_step(fake, ["SPY"], CFG, state, _now((16, 5)), notional=2000)
    assert fake.closed is False


def test_run_step_unavailable_client_noops():
    class Dead:
        def available(self):
            return False
    r = run_step(Dead(), ["SPY"], CFG, {"traded": {}}, _now((9, 45)), 2000)
    assert r["status"] == "unavailable"

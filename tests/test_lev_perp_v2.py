"""lev_perp_v2_paper: ATR chandelier exit engine (the only thing that differs
from v1 — entries/filters/sizing are imported from lev_perp_paper and covered
by its tests)."""
import pytest

import lev_perp_v2_paper as v2
import lev_perp_paper as lp


def _bars(prices, spread=1.0):
    return [{"t": i, "h": p + spread, "l": p - spread, "c": p, "v": 100.0}
            for i, p in enumerate(prices)]


def _pos(side, entry, atr=2.0):
    return {"symbol": "BTC", "side": side, "entry": entry, "entry_ts": "0",
            "margin_usd": 333.33, "leverage": 3.0, "notional_usd": 1000.0,
            "peak": entry, "atr": atr,
            "trail": entry - side * v2.ATR_MULT * atr,
            "liq": entry * (1 - side * (1.0 - lp.MAINT) / 3.0)}


class TestATR:
    def test_atr_simple_mean_true_range(self):
        bars = _bars([100.0] * (v2.ATR_N + 1), spread=1.5)
        assert v2._atr(bars) == pytest.approx(3.0)  # flat closes -> TR = h-l

    def test_atr_needs_history(self):
        assert v2._atr(_bars([100.0] * 3)) is None


class TestRatchet:
    def test_long_peak_and_trail_advance(self):
        pos = _pos(1, 100.0, atr=2.0)  # trail starts 96
        v2._ratchet(pos, {"h": 110.0, "l": 105.0, "c": 108.0})
        assert pos["peak"] == 110.0
        assert pos["trail"] == pytest.approx(110.0 - v2.ATR_MULT * 2.0)

    def test_trail_never_loosens(self):
        pos = _pos(1, 100.0, atr=2.0)
        v2._ratchet(pos, {"h": 110.0, "l": 105.0, "c": 108.0})
        t = pos["trail"]
        v2._ratchet(pos, {"h": 104.0, "l": 101.0, "c": 102.0})  # pullback bar
        assert pos["trail"] == t and pos["peak"] == 110.0

    def test_short_ratchets_down(self):
        pos = _pos(-1, 100.0, atr=2.0)  # trail starts 104
        v2._ratchet(pos, {"h": 95.0, "l": 90.0, "c": 92.0})
        assert pos["peak"] == 90.0
        assert pos["trail"] == pytest.approx(90.0 + v2.ATR_MULT * 2.0)


class TestExit:
    def test_long_stopped_at_trail(self):
        pos = _pos(1, 100.0, atr=2.0)  # trail 96
        assert v2._check_exit(pos, {"h": 101.0, "l": 95.5, "c": 97.0}) == (96.0, "trail_stop")

    def test_short_stopped_at_trail(self):
        pos = _pos(-1, 100.0, atr=2.0)  # trail 104
        out = v2._check_exit(pos, {"h": 104.5, "l": 99.0, "c": 103.0})
        assert out == (104.0, "trail_stop")

    def test_liquidation_beats_trail(self):
        pos = _pos(-1, 100.0, atr=20.0)  # trail 140, beyond liq ~131.7
        out = v2._check_exit(pos, {"h": 150.0, "l": 99.0, "c": 100.0})
        assert out[1] == "liquidation"

    def test_no_exit_inside_trail(self):
        pos = _pos(1, 100.0, atr=2.0)
        assert v2._check_exit(pos, {"h": 103.0, "l": 97.0, "c": 102.0}) is None

    def test_bar_cannot_arm_its_own_trail(self):
        # a huge favorable spike then reversal in ONE bar must NOT exit at the
        # spiked trail — exit checks run against prior-bar state only
        pos = _pos(1, 100.0, atr=2.0)  # trail 96
        bar = {"h": 120.0, "l": 97.0, "c": 98.0}
        assert v2._check_exit(pos, bar) is None   # 97 > 96: no exit at old trail
        v2._ratchet(pos, bar)                     # only now does the peak advance
        assert pos["peak"] == 120.0


class TestWinnersRunLosersCut:
    """The A/B thesis in miniature: a trending sequence exits far beyond +5%
    (where v1 would have capped), an adverse one cuts near ~2 ATR."""
    def test_trend_exit_beyond_v1_tp(self):
        state = {"positions": {}, "closed": [], "equity": 1000.0,
                 "starting_equity": 1000.0}
        prices = [100.0] * (v2.ATR_N + 1) + [104, 108, 112, 116, 120, 111]
        bars = _bars(prices, spread=1.0)
        v2._open(state, "BTC", 1, 100.0, "0", bars[:v2.ATR_N + 1],
                 [b["c"] for b in bars[:v2.ATR_N + 1]])
        pos = state["positions"]["BTC"]
        for bar in bars[v2.ATR_N + 1:]:
            ex = v2._check_exit(pos, bar)
            if ex:
                exit_price = ex[0]
                break
            v2._ratchet(pos, bar)
        else:
            pytest.fail("never exited")
        assert exit_price > 100.0 * (1 + lp.TP_PRICE_FRAC)  # rode past v1's +5% cap

    def test_adverse_entry_cut_early(self):
        pos = _pos(1, 100.0, atr=2.0)  # trail 96 -> loss capped ~-4% price
        out = v2._check_exit(pos, {"h": 100.0, "l": 93.0, "c": 94.0})
        assert out == (96.0, "trail_stop")
        assert (out[0] - 100.0) / 100.0 > -0.05  # tighter than v1's -5% fixed stop

"""Risk controls added to lev_perp_paper 2026-07-01: hard stop, vol-targeted
leverage, correlation-capped margin, news halt. See the module docstring's
RISK CONTROLS section for the spec."""
import json
import time

import pytest

import lev_perp_paper as lp


def _mkpos(side, entry, stop=None, lev=3.0):
    pos = {"symbol": "BTC", "side": side, "entry": entry, "entry_ts": "0",
           "margin_usd": 333.33, "leverage": lev,
           "notional_usd": 333.33 * lev,
           "tp": entry * (1 + side * lp.TP_PRICE_FRAC),
           "liq": entry * (1 - side * (1.0 - lp.MAINT) / lev)}
    if stop is not None:
        pos["stop"] = stop
    return pos


class TestHardStop:
    def test_short_stopped_on_adverse_move(self):
        pos = _mkpos(-1, 100.0, stop=105.0)
        out = lp._check_exit(pos, {"h": 106.0, "l": 99.0, "c": 105.5})
        assert out == (105.0, "stop_loss")

    def test_long_stopped_on_adverse_move(self):
        pos = _mkpos(1, 100.0, stop=95.0)
        out = lp._check_exit(pos, {"h": 101.0, "l": 94.0, "c": 96.0})
        assert out == (95.0, "stop_loss")

    def test_stop_synthesized_for_legacy_position(self):
        # positions opened before this change carry no "stop" key
        pos = _mkpos(-1, 100.0)
        assert "stop" not in pos
        out = lp._check_exit(pos, {"h": 100.0 * (1 + lp.SL_PRICE_FRAC) + 0.01,
                                   "l": 99.0, "c": 100.0})
        assert out is not None and out[1] == "stop_loss"

    def test_liquidation_beats_stop_intrabar(self):
        pos = _mkpos(-1, 100.0, stop=105.0)
        liq = pos["liq"]
        out = lp._check_exit(pos, {"h": liq + 1, "l": 99.0, "c": 100.0})
        assert out == (liq, "liquidation")

    def test_stop_beats_tp_intrabar(self):
        # bar touches both -> conservative: adverse first
        pos = _mkpos(-1, 100.0, stop=105.0)
        out = lp._check_exit(pos, {"h": 106.0, "l": 90.0, "c": 100.0})
        assert out[1] == "stop_loss"

    def test_no_exit_inside_band(self):
        pos = _mkpos(-1, 100.0, stop=105.0)
        assert lp._check_exit(pos, {"h": 101.0, "l": 97.0, "c": 99.0}) is None


class TestVolTargetedLeverage:
    def test_calm_market_hits_cap(self):
        closes = [100.0 * (1.001 ** i) for i in range(lp.VOL_N + 1)]  # ~0.1%/day vol -> cap
        assert lp._effective_leverage(closes) == lp.LEVERAGE

    def test_volatile_market_sizes_down(self):
        closes = [100.0]
        for i in range(lp.VOL_N + 5):  # alternate +-4%/day -> ~4% vol
            closes.append(closes[-1] * (1.04 if i % 2 else 0.96))
        lev = lp._effective_leverage(closes)
        assert lev < lp.LEVERAGE
        vol = lp._realized_vol(closes, lp.VOL_N)
        assert lev == pytest.approx(max(0.1, lp.VOL_TARGET / vol))

    def test_insufficient_history_falls_back_to_cap(self):
        assert lp._effective_leverage([100.0, 101.0]) == lp.LEVERAGE

    def test_open_stores_effective_leverage_and_matching_liq(self):
        state = {"positions": {}}
        closes = [100.0]
        for i in range(lp.VOL_N + 5):
            closes.append(closes[-1] * (1.04 if i % 2 else 0.96))
        lp._open(state, "BTC", -1, 100.0, "0", closes)
        pos = state["positions"]["BTC"]
        assert pos["leverage"] < lp.LEVERAGE
        liq_frac = (1.0 - lp.MAINT) / pos["leverage"]
        assert pos["liq"] == pytest.approx(100.0 * (1 + liq_frac), rel=1e-2)
        if lp.SL_PRICE_FRAC > 0:
            assert pos["stop"] == pytest.approx(100.0 * (1 + lp.SL_PRICE_FRAC))


class TestCorrelationCap:
    def test_first_position_gets_full_margin(self):
        assert lp._entry_margin({"positions": {}}, -1) == lp.MARGIN

    def test_same_direction_margin_split(self):
        state = {"positions": {"ETH": _mkpos(-1, 1500.0, stop=1575.0)}}
        assert lp._entry_margin(state, -1) == pytest.approx(lp.MARGIN / 2, abs=0.01)

    def test_opposite_direction_not_penalized(self):
        state = {"positions": {"ETH": _mkpos(1, 1500.0, stop=1425.0)}}
        assert lp._entry_margin(state, -1) == lp.MARGIN


class TestNewsHalt:
    def test_no_file_means_no_halt(self, tmp_path, monkeypatch):
        monkeypatch.setattr(lp, "NEWS_HALT_FILE", tmp_path / "absent.json")
        assert lp._news_halted() is False

    def test_future_until_halts(self, tmp_path, monkeypatch):
        f = tmp_path / "news_halt.json"
        f.write_text(json.dumps({"until": time.time() + 3600}))
        monkeypatch.setattr(lp, "NEWS_HALT_FILE", f)
        assert lp._news_halted() is True

    def test_expired_until_clears(self, tmp_path, monkeypatch):
        f = tmp_path / "news_halt.json"
        f.write_text(json.dumps({"until": time.time() - 60}))
        monkeypatch.setattr(lp, "NEWS_HALT_FILE", f)
        assert lp._news_halted() is False

    def test_malformed_file_fails_open(self, tmp_path, monkeypatch):
        f = tmp_path / "news_halt.json"
        f.write_text("not json")
        monkeypatch.setattr(lp, "NEWS_HALT_FILE", f)
        assert lp._news_halted() is False

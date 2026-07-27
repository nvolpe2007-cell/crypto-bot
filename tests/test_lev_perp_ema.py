"""lev_perp_ema_paper: EMA(21/55)-cross directional signal (the only thing
that differs from v2 — exit engine is imported from lev_perp_v2_paper and
covered by its tests; entry filters other than trend-age are imported from
lev_perp_paper and covered by its tests)."""
import pytest

import lev_perp_ema_paper as ema
import lev_perp_paper as lp


class TestEMA:
    def test_ema_needs_history(self):
        assert ema._ema([100.0] * 5, 21) is None

    def test_ema_seeds_with_sma(self):
        prices = [100.0] * 21
        series = ema._ema(prices, 21)
        assert series == pytest.approx([100.0])

    def test_ema_tracks_a_rising_series(self):
        prices = [100.0] * 21 + [110.0] * 10
        series = ema._ema(prices, 21)
        assert series[-1] > series[0]
        assert series[-1] < 110.0  # EMA lags the new level


class TestEMACrossSide:
    def test_none_during_warmup(self):
        assert ema._ema_cross_side([100.0] * 10) is None

    def test_uptrend_gives_long_side(self):
        # fast EMA(21) must pull above slow EMA(55) after a sustained rally
        prices = [100.0] * 55 + [100.0 + i * 2 for i in range(1, 40)]
        assert ema._ema_cross_side(prices) == 1

    def test_downtrend_gives_short_side(self):
        prices = [100.0] * 55 + [100.0 - i * 2 for i in range(1, 40)]
        assert ema._ema_cross_side(prices) == -1


class TestTrendAgeEMA:
    def test_zero_during_warmup(self):
        assert ema._trend_age_ema([100.0] * 10) == 0

    def test_age_grows_with_a_persistent_cross(self):
        prices = [100.0] * 55 + [100.0 + i * 2 for i in range(1, 40)]
        age_early = ema._trend_age_ema(prices[:70])
        age_late = ema._trend_age_ema(prices)
        assert age_late > age_early > 0


class TestEntryFilterEMA:
    def _bars(self, prices, spread=1.0, vol=100.0):
        return [{"t": i, "h": p + spread, "l": p - spread, "c": p, "v": vol}
                for i, p in enumerate(prices)]

    def test_rejects_on_rsi_extreme(self):
        # a straight-line rally trips RSI>=45 before anything else is checked
        prices = [100.0 + i for i in range(80)]
        bars = self._bars(prices)
        reasons = []
        ok = ema._entry_filter_ema(bars, prices, reasons)
        assert not ok
        assert any("RSI" in r for r in reasons)

    def test_rejects_on_short_trend_age(self, monkeypatch):
        # force every other gate to pass so only the trend-age check can fire
        prices = [100.0 + i for i in range(80)]
        bars = self._bars(prices)
        monkeypatch.setattr(lp, "_rsi", lambda closes, n: 30.0)
        monkeypatch.setattr(lp, "_vol_ratio", lambda bars, lookback: 5.0)
        monkeypatch.setattr(lp, "_adx", lambda bars, n: 50.0)  # outside the dead-zone
        monkeypatch.setattr(ema, "_trend_age_ema", lambda closes: lp.MIN_TREND_AGE - 1)
        reasons = []
        ok = ema._entry_filter_ema(bars, prices, reasons)
        assert not ok
        assert any("trend_age" in r for r in reasons)


def test_state_file_env_override(monkeypatch, tmp_path):
    override = tmp_path / "custom_ema_state.json"
    monkeypatch.setenv("LEV_PERP_EMA_STATE_FILE", str(override))
    import importlib
    importlib.reload(ema)
    assert ema.STATE_FILE == override
    importlib.reload(ema)  # restore default for other tests in the session

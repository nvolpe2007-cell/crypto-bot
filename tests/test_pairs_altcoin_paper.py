"""Tests for pairs_altcoin_paper.py — the validated-altcoin market-neutral pairs
forward arm. Core z-score/cost/exit mechanics are pairs_paper.py's own (imported,
not reimplemented) and covered by tests/test_pairs_paper.py; these tests cover
what's actually NEW here: the explicit pair universe, state-file isolation from
the production pairs arm, and the process()/kill-switch wiring around it.

No network: feed synthetic close series and prices directly.
"""
import pytest

import pairs_altcoin_paper as pac
import pairs_paper as pp


def _state():
    return {"positions": {}, "closed": [], "last_bar_t": {}, "equity_curve": [],
            "starting_equity": 1000.0, "equity": 1000.0, "equity_mtm": 1000.0,
            "halted": False}


class TestPairUniverse:
    def test_pairs_are_the_four_validated_ones(self):
        assert set(pac.PAIRS) == {("AVAX", "ETH"), ("BCH", "ETH"),
                                   ("ETH", "XRP"), ("LINK", "SOL")}

    def test_not_a_combinatorial_expansion(self):
        # every coin touched by PAIRS
        coins = {c for pair in pac.PAIRS for c in pair}
        # combinations(coins, 2) would be 15 pairs; we deliberately trade only 4
        from itertools import combinations
        all_combos = set(tuple(sorted(c)) for c in combinations(coins, 2))
        assert len(all_combos) > len(pac.PAIRS)
        assert set(pac.PAIRS).issubset(all_combos)

    def test_kraken_pairs_cover_every_symbol_in_every_pair(self):
        needed = {c for pair in pac.PAIRS for c in pair}
        assert needed.issubset(pac.KRAKEN_PAIRS.keys())


class TestStateIsolation:
    def test_default_state_file_differs_from_production_pairs_arm(self):
        assert pac.STATE_FILE != pp.STATE_FILE
        assert "altcoin" in str(pac.STATE_FILE)

    def test_state_file_env_override(self, monkeypatch, tmp_path):
        override = tmp_path / "custom_altcoin_state.json"
        monkeypatch.setenv("PAIRS_ALTCOIN_STATE_FILE", str(override))
        import importlib
        reloaded = importlib.reload(pac)
        assert reloaded.STATE_FILE == override
        importlib.reload(pac)  # restore default for other tests


class TestProcess:
    def test_opens_on_entry_z_for_a_validated_pair(self, monkeypatch):
        monkeypatch.setattr(pp, "LEG_NOTIONAL", 150.0)
        monkeypatch.setattr(pp, "LOOKBACK", 10)
        st = _state()
        # build a spread series for LINK/SOL that swings to a large z at the end
        closes_link = {t: 100.0 for t in range(20)}
        closes_sol = {t: 100.0 for t in range(20)}
        closes_link[19] = 140.0  # LINK rich vs SOL -> z > 0 -> short LINK, long SOL
        prices = {"LINK": closes_link[19], "SOL": closes_sol[19],
                  "ETH": 100.0, "AVAX": 100.0, "BCH": 100.0, "XRP": 100.0}
        acted = pac.process(st, {"LINK": closes_link, "SOL": closes_sol}, prices, now=None)
        assert acted >= 1
        assert "LINK-SOL" in st["positions"]
        pos = st["positions"]["LINK-SOL"]
        assert pos["short_sym"] == "LINK" and pos["long_sym"] == "SOL"

    def test_idempotent_same_bar(self, monkeypatch):
        monkeypatch.setattr(pp, "LEG_NOTIONAL", 150.0)
        monkeypatch.setattr(pp, "LOOKBACK", 10)
        st = _state()
        closes_link = {t: 100.0 for t in range(20)}
        closes_sol = {t: 100.0 for t in range(20)}
        closes_link[19] = 140.0
        prices = {"LINK": 140.0, "SOL": 100.0, "ETH": 100.0, "AVAX": 100.0,
                  "BCH": 100.0, "XRP": 100.0}
        closes = {"LINK": closes_link, "SOL": closes_sol}
        pac.process(st, closes, prices, now=None)
        acted_again = pac.process(st, closes, prices, now=None)
        assert acted_again == 0  # same bar already processed

    def test_missing_symbol_skips_pair_without_crashing(self):
        st = _state()
        # only ETH present -- every pair needs a second symbol that's absent
        acted = pac.process(st, {"ETH": {0: 100.0}}, {"ETH": 100.0}, now=None)
        assert acted == 0


class TestRegimeSizing:
    def test_disabled_returns_full_size(self, monkeypatch):
        monkeypatch.setattr(pac, "REGIME_SIZING_ENABLED", False)
        assert pac.regime_size_mult() == 1.0

    def test_fails_safe_to_full_size_on_fetch_error(self, monkeypatch):
        monkeypatch.setattr(pac, "REGIME_SIZING_ENABLED", True)
        import sys
        # simulate ccxt raising during the fetch loop
        class BoomKraken:
            def __init__(self, *a, **k): pass
            def fetch_ohlcv(self, *a, **k): raise RuntimeError("network down")
        fake_ccxt = type(sys)("ccxt")
        fake_ccxt.kraken = BoomKraken
        monkeypatch.setitem(sys.modules, "ccxt", fake_ccxt)
        assert pac.regime_size_mult() == 1.0

    def test_open_applies_size_mult_and_restores_global_after(self, monkeypatch):
        monkeypatch.setattr(pp, "LEG_NOTIONAL", 150.0)
        monkeypatch.setattr(pp, "LOOKBACK", 10)
        st = _state()
        closes_link = {t: 100.0 for t in range(20)}
        closes_sol = {t: 100.0 for t in range(20)}
        closes_link[19] = 140.0
        prices = {"LINK": 140.0, "SOL": 100.0, "ETH": 100.0, "AVAX": 100.0,
                  "BCH": 100.0, "XRP": 100.0}
        pac.process(st, {"LINK": closes_link, "SOL": closes_sol}, prices, now=None, size_mult=0.5)
        pos = st["positions"]["LINK-SOL"]
        assert pos["leg_notional"] == pytest.approx(75.0)   # 150 * 0.5
        assert pp.LEG_NOTIONAL == 150.0                       # global restored, not left mutated

    def test_full_size_mult_matches_unscaled_open(self, monkeypatch):
        monkeypatch.setattr(pp, "LEG_NOTIONAL", 150.0)
        monkeypatch.setattr(pp, "LOOKBACK", 10)
        st = _state()
        closes_link = {t: 100.0 for t in range(20)}
        closes_sol = {t: 100.0 for t in range(20)}
        closes_link[19] = 140.0
        prices = {"LINK": 140.0, "SOL": 100.0, "ETH": 100.0, "AVAX": 100.0,
                  "BCH": 100.0, "XRP": 100.0}
        pac.process(st, {"LINK": closes_link, "SOL": closes_sol}, prices, now=None, size_mult=1.0)
        assert st["positions"]["LINK-SOL"]["leg_notional"] == pytest.approx(150.0)


class TestKillSwitch:
    def test_kill_switch_blocks_new_open(self, monkeypatch):
        monkeypatch.setattr(pp, "LEG_NOTIONAL", 150.0)
        monkeypatch.setattr(pp, "LOOKBACK", 10)
        monkeypatch.setattr(pac, "_is_killed", lambda: True)
        st = _state()
        closes_link = {t: 100.0 for t in range(20)}
        closes_sol = {t: 100.0 for t in range(20)}
        closes_link[19] = 140.0
        prices = {"LINK": 140.0, "SOL": 100.0, "ETH": 100.0, "AVAX": 100.0,
                  "BCH": 100.0, "XRP": 100.0}
        acted = pac.process(st, {"LINK": closes_link, "SOL": closes_sol}, prices, now=None)
        assert acted == 0
        assert st["positions"] == {}

    def test_halted_state_blocks_new_open(self, monkeypatch):
        monkeypatch.setattr(pp, "LEG_NOTIONAL", 150.0)
        monkeypatch.setattr(pp, "LOOKBACK", 10)
        st = _state()
        st["halted"] = True
        closes_link = {t: 100.0 for t in range(20)}
        closes_sol = {t: 100.0 for t in range(20)}
        closes_link[19] = 140.0
        prices = {"LINK": 140.0, "SOL": 100.0, "ETH": 100.0, "AVAX": 100.0,
                  "BCH": 100.0, "XRP": 100.0}
        acted = pac.process(st, {"LINK": closes_link, "SOL": closes_sol}, prices, now=None)
        assert acted == 0

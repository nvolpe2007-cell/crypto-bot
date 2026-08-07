"""LEV_PERP_SYMBOLS universe widening (2026-08-07): the arm's default 3-coin
universe (BTC/ETH/SOL) must stay byte-for-byte unchanged for production runs
that don't set LEV_PERP_SYMBOLS -- ALLOC_FRAC/MARGIN sizing changing silently
for the live v1 arm or its agg twins would be a real, hard-to-notice bug. An
explicit override can additionally reach the wider superset (adds ADA/XRP/
DOT/AVAX/LINK) per RESEARCH_2026-07-26_lev_perp_v1_frequency.md."""
import importlib
import os

import pytest


@pytest.fixture()
def lp(monkeypatch):
    monkeypatch.delenv("LEV_PERP_SYMBOLS", raising=False)
    import lev_perp_paper
    return importlib.reload(lev_perp_paper)


class TestDefaultUnchanged:
    def test_default_universe_is_still_3_coin(self, lp):
        assert lp.KRAKEN_PAIRS == {"BTC": "XBTUSD", "ETH": "ETHUSD", "SOL": "SOLUSD"}

    def test_default_alloc_frac_is_still_one_third(self, lp):
        assert lp.ALLOC_FRAC == pytest.approx(1 / 3)

    def test_default_margin_unchanged(self, lp):
        assert lp.MARGIN == pytest.approx(round(lp.STARTING_EQUITY / 3, 2))


class TestWidenedOverride:
    def test_symbols_env_can_reach_the_wider_superset(self, monkeypatch):
        monkeypatch.setenv("LEV_PERP_SYMBOLS", "BTC,ETH,SOL,ADA,XRP,DOT,AVAX,LINK")
        import lev_perp_paper
        mod = importlib.reload(lev_perp_paper)
        assert set(mod.KRAKEN_PAIRS) == {"BTC", "ETH", "SOL", "ADA", "XRP", "DOT", "AVAX", "LINK"}
        importlib.reload(lev_perp_paper)  # restore default for later tests

    def test_alloc_frac_splits_across_active_set_not_the_full_dict(self, monkeypatch):
        monkeypatch.setenv("LEV_PERP_SYMBOLS", "BTC,ETH,SOL,ADA,XRP,DOT,AVAX,LINK")
        import lev_perp_paper
        mod = importlib.reload(lev_perp_paper)
        assert mod.ALLOC_FRAC == pytest.approx(1 / 8)
        assert mod.MARGIN == pytest.approx(round(mod.STARTING_EQUITY / 8, 2))
        importlib.reload(lev_perp_paper)

    def test_partial_override_still_uses_active_count_not_all_count(self, monkeypatch):
        # subset of the wider universe -- ALLOC_FRAC must reflect the SUBSET size
        monkeypatch.setenv("LEV_PERP_SYMBOLS", "BTC,ADA,LINK")
        import lev_perp_paper
        mod = importlib.reload(lev_perp_paper)
        assert set(mod.KRAKEN_PAIRS) == {"BTC", "ADA", "LINK"}
        assert mod.ALLOC_FRAC == pytest.approx(1 / 3)
        importlib.reload(lev_perp_paper)

    def test_wide_superset_is_a_strict_extension_of_the_default(self, lp):
        assert set(lp.KRAKEN_PAIRS_ALL).issubset(set(lp.KRAKEN_PAIRS_WIDE))
        assert len(lp.KRAKEN_PAIRS_WIDE) == len(lp.KRAKEN_PAIRS_ALL) + 5

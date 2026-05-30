"""Unit tests for src/expectancy_gate.py — per-path gross-expectancy size cap."""
from types import SimpleNamespace

from src.expectancy_gate import ExpectancyGate, PROBE_USD


def _rec(path="main", pnl=0.0, fees=0.0, slippage=0.0, version=2):
    return SimpleNamespace(entry_path=path, pnl=pnl, fees_paid=fees,
                           slippage_cost=slippage, prob_model_version=version)


def _journal(records):
    return SimpleNamespace(records=records)


def _gate(min_trades=30):
    return ExpectancyGate(min_trades=min_trades, probe_usd=PROBE_USD, refit_every=1, min_model_version=2)


class TestNoData:
    def test_unknown_path_is_probe_capped(self):
        g = _gate()
        g.update(_journal([]), force=True)
        assert g.cap_for("main") == PROBE_USD

    def test_path_with_no_records_capped(self):
        g = _gate()
        g.update(_journal([_rec(path="mr", pnl=1.0)]), force=True)
        # 'fast-track' has no data → probe
        assert g.cap_for("fast-track") == PROBE_USD


class TestProofThreshold:
    def test_below_min_trades_capped_even_if_positive(self):
        g = _gate(min_trades=30)
        recs = [_rec(pnl=1.0, fees=0.1) for _ in range(29)]   # gross +1.1 each, but n<30
        g.update(_journal(recs), force=True)
        assert g.cap_for("main") == PROBE_USD

    def test_proven_when_enough_and_gross_positive(self):
        g = _gate(min_trades=30)
        recs = [_rec(pnl=1.0, fees=0.1) for _ in range(30)]   # gross +1.1, n=30
        g.update(_journal(recs), force=True)
        assert g.cap_for("main") is None        # uncapped

    def test_capped_when_gross_negative_despite_sample(self):
        g = _gate(min_trades=30)
        # net positive only via... no: pnl negative, fees positive → gross negative
        recs = [_rec(pnl=-0.5, fees=0.1) for _ in range(40)]  # gross -0.4
        g.update(_journal(recs), force=True)
        assert g.cap_for("main") == PROBE_USD

    def test_gross_uses_precost_pnl(self):
        """A path that's net-negative but gross-positive still counts as having
        an edge (gross > 0) once the sample is large enough."""
        g = _gate(min_trades=10)
        # pnl net -0.05 (loss after cost) but gross = pnl + fees = -0.05 + 0.10 = +0.05
        recs = [_rec(pnl=-0.05, fees=0.10) for _ in range(10)]
        g.update(_journal(recs), force=True)
        assert g.cap_for("main") is None


class TestVersionFilter:
    def test_legacy_records_excluded(self):
        g = _gate(min_trades=30)
        # 40 legacy (v0) winners would prove the path IF counted — they must not be.
        recs = [_rec(pnl=1.0, version=0) for _ in range(40)]
        g.update(_journal(recs), force=True)
        assert g.cap_for("main") == PROBE_USD   # no v2 data → still probe

    def test_per_path_independence(self):
        g = _gate(min_trades=10)
        recs = ([_rec(path="main", pnl=1.0, fees=0.1) for _ in range(10)] +   # proven
                [_rec(path="mr", pnl=-1.0, fees=0.1) for _ in range(10)])       # capped
        g.update(_journal(recs), force=True)
        assert g.cap_for("main") is None
        assert g.cap_for("mr") == PROBE_USD


class TestRefitGating:
    def test_update_skips_until_growth(self):
        g = ExpectancyGate(min_trades=5, refit_every=10, min_model_version=2)
        recs = [_rec(pnl=1.0) for _ in range(5)]
        assert g.update(_journal(recs), force=True) is True
        # only +3 new records, below refit_every=10 → no recompute
        assert g.update(_journal(recs + [_rec(pnl=1.0) for _ in range(3)])) is False

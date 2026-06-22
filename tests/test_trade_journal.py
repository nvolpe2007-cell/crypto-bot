"""
Unit tests for src/trade_journal.py

Covers:
- TradeRecord.to_dict: all fields serialised correctly
- TradeRecord.features: correct keys, all-float values, direction encoding,
  lead_lag_aligned encoding, numeric fields finite
- TradeJournal (file-isolated via monkeypatch):
    - starts empty when no journal file exists
    - add() appends record to in-memory list
    - wins() / losses() filter on the 'won' field
    - stats() returns {total:0} for an empty journal
    - stats() counts wins, losses, and win_rate correctly
    - save() + reload via a fresh instance round-trips all records
    - old records missing new fields get _DEFAULTS applied on load
    - build_record() maps a Trade + signal correctly to a TradeRecord
"""

import json
import os
import pytest
from datetime import datetime, timezone
from unittest.mock import MagicMock

import src.trade_journal as _tj_module
from src.trade_journal import TradeJournal, TradeRecord


# ── helpers ───────────────────────────────────────────────────────────────────

def _minimal_record(**overrides) -> TradeRecord:
    """Return a TradeRecord with all required fields filled in."""
    base = dict(
        trade_id="BTC_1700000000",
        symbol="BTC/USD",
        opened_at="2024-01-01T00:00:00+00:00",
        closed_at="2024-01-01T00:05:00+00:00",
        rsi=55.0,
        adx=28.0,
        volume_ratio=1.2,
        regime="TRENDING_UP",
        atr_pct=0.5,
        ema100_gap=0.3,
        ema200_gap=0.8,
        hour_utc=12,
        day_of_week=1,
        pnl=5.0,
        pnl_pct=0.5,
        won=True,
        reason="TAKE_PROFIT",
    )
    base.update(overrides)
    return TradeRecord(**base)


def _make_trade(entry_price=50_000.0, exit_price=51_000.0, pnl=10.0,
                pnl_pct=1.0, size=0.002, side="sell"):
    """Return a minimal Trade-like object."""
    t = MagicMock()
    t.entry_price = entry_price
    t.exit_price  = exit_price
    t.pnl         = pnl
    t.pnl_pct     = pnl_pct
    t.size        = size
    t.side        = side
    t.entry_time  = datetime(2024, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
    t.exit_time   = datetime(2024, 1, 1, 0, 5, 0, tzinfo=timezone.utc)
    return t


def _make_signal(rsi=55.0, adx=28.0, atr=500.0, close=50_000.0,
                 regime="TRENDING_UP", volume_ratio=1.2):
    """Return a minimal signal-like object with the fields build_record() reads."""
    s = MagicMock()
    s.rsi          = rsi
    s.adx          = adx
    s.atr          = atr
    s.close        = close
    s.regime       = regime
    s.volume_ratio = volume_ratio
    # ScientificSignal does NOT have ema100 / ema200
    del s.ema100
    del s.ema200
    return s


@pytest.fixture()
def journal(tmp_path, monkeypatch):
    """Return a TradeJournal backed by a temp file, reset between tests."""
    fake_path = str(tmp_path / "journal.json")
    monkeypatch.setattr(_tj_module, "JOURNAL_FILE", fake_path)
    return TradeJournal()


# ── TradeRecord.to_dict ───────────────────────────────────────────────────────

class TestTradeRecordToDict:
    def test_returns_dict(self):
        assert isinstance(_minimal_record().to_dict(), dict)

    def test_required_keys_present(self):
        d = _minimal_record().to_dict()
        for key in ("trade_id", "symbol", "opened_at", "closed_at",
                    "rsi", "adx", "volume_ratio", "regime",
                    "pnl", "pnl_pct", "won", "reason"):
            assert key in d, f"missing key: {key}"

    def test_extended_keys_present(self):
        d = _minimal_record().to_dict()
        for key in ("ofi", "lead_lag_strength", "lead_lag_aligned",
                    "confidence", "ofi_score", "lead_lag_score",
                    "regime_score", "funding_rate", "direction"):
            assert key in d, f"missing extended key: {key}"

    def test_postmortem_keys_present(self):
        d = _minimal_record().to_dict()
        for key in ("mfe_pct", "mae_pct", "time_in_trade_sec",
                    "regime_at_exit", "rsi_at_exit", "adx_at_exit", "exit_price"):
            assert key in d, f"missing post-mortem key: {key}"

    def test_values_match_fields(self):
        r = _minimal_record(pnl=12.5, symbol="ETH/USD", won=False)
        d = r.to_dict()
        assert d["pnl"] == 12.5
        assert d["symbol"] == "ETH/USD"
        assert d["won"] is False

    def test_default_direction_is_buy(self):
        assert _minimal_record().to_dict()["direction"] == "buy"

    def test_custom_direction_preserved(self):
        r = _minimal_record(direction="short")
        assert r.to_dict()["direction"] == "short"


# ── TradeRecord.features ──────────────────────────────────────────────────────

_EXPECTED_FEATURE_KEYS = {
    "rsi", "adx", "volume_ratio", "atr_pct", "ema100_gap", "ema200_gap",
    "hour_utc", "day_of_week", "ofi", "lead_lag_strength", "lead_lag_aligned",
    "regime_confidence", "funding_rate", "ofi_score", "lead_lag_score",
    "regime_score", "confidence", "is_buy",
}


class TestTradeRecordFeatures:
    def test_returns_dict(self):
        assert isinstance(_minimal_record().features(), dict)

    def test_expected_keys_present(self):
        features = _minimal_record().features()
        for key in _EXPECTED_FEATURE_KEYS:
            assert key in features, f"missing feature key: {key}"

    def test_no_unexpected_keys(self):
        features = _minimal_record().features()
        assert set(features.keys()) == _EXPECTED_FEATURE_KEYS

    def test_all_values_are_float(self):
        features = _minimal_record().features()
        for key, val in features.items():
            assert isinstance(val, float), f"features['{key}'] is {type(val)}, expected float"

    def test_all_values_finite(self):
        import math
        features = _minimal_record().features()
        for key, val in features.items():
            assert math.isfinite(val), f"features['{key}'] = {val} is not finite"

    def test_direction_buy_encodes_as_1(self):
        r = _minimal_record(direction="buy")
        assert r.features()["is_buy"] == 1.0

    def test_direction_short_encodes_as_0(self):
        r = _minimal_record(direction="short")
        assert r.features()["is_buy"] == 0.0

    def test_direction_sell_encodes_as_0(self):
        r = _minimal_record(direction="sell")
        assert r.features()["is_buy"] == 0.0

    def test_lead_lag_aligned_true_encodes_as_1(self):
        r = _minimal_record(lead_lag_aligned=True)
        assert r.features()["lead_lag_aligned"] == 1.0

    def test_lead_lag_aligned_false_encodes_as_0(self):
        r = _minimal_record(lead_lag_aligned=False)
        assert r.features()["lead_lag_aligned"] == 0.0

    def test_rsi_matches_record(self):
        r = _minimal_record(rsi=42.0)
        assert r.features()["rsi"] == 42.0

    def test_confidence_matches_record(self):
        r = _minimal_record(confidence=85.0)
        assert r.features()["confidence"] == 85.0

    def test_hour_utc_is_float(self):
        r = _minimal_record(hour_utc=15)
        assert r.features()["hour_utc"] == 15.0

    def test_day_of_week_is_float(self):
        r = _minimal_record(day_of_week=3)
        assert r.features()["day_of_week"] == 3.0


# ── TradeJournal — empty state ────────────────────────────────────────────────

class TestTradeJournalEmpty:
    def test_starts_empty_when_no_file(self, journal):
        assert journal.records == []

    def test_wins_is_empty_list(self, journal):
        assert journal.wins() == []

    def test_losses_is_empty_list(self, journal):
        assert journal.losses() == []

    def test_stats_returns_total_zero(self, journal):
        assert journal.stats() == {"total": 0}


# ── TradeJournal.add ──────────────────────────────────────────────────────────

class TestTradeJournalAdd:
    def test_add_appends_record(self, journal):
        r = _minimal_record()
        journal.add(r)
        assert len(journal.records) == 1

    def test_add_multiple_records(self, journal):
        for i in range(5):
            journal.add(_minimal_record(trade_id=f"id_{i}"))
        assert len(journal.records) == 5

    def test_add_persists_to_file(self, journal, tmp_path, monkeypatch):
        """After add(), the file on disk must contain the record."""
        journal.add(_minimal_record(trade_id="persisted"))
        file_path = _tj_module.JOURNAL_FILE
        assert os.path.exists(file_path)
        with open(file_path) as f:
            data = json.load(f)
        assert any(r.get("trade_id") == "persisted" for r in data)


# ── TradeJournal.wins / losses ────────────────────────────────────────────────

class TestTradeJournalWinsLosses:
    def test_wins_only_returns_won_records(self, journal):
        journal.add(_minimal_record(won=True))
        journal.add(_minimal_record(won=False))
        assert all(r.won for r in journal.wins())
        assert len(journal.wins()) == 1

    def test_losses_only_returns_lost_records(self, journal):
        journal.add(_minimal_record(won=True))
        journal.add(_minimal_record(won=False))
        assert all(not r.won for r in journal.losses())
        assert len(journal.losses()) == 1

    def test_all_wins(self, journal):
        for _ in range(3):
            journal.add(_minimal_record(won=True))
        assert len(journal.wins()) == 3
        assert len(journal.losses()) == 0

    def test_all_losses(self, journal):
        for _ in range(3):
            journal.add(_minimal_record(won=False))
        assert len(journal.wins()) == 0
        assert len(journal.losses()) == 3


# ── TradeJournal.stats ────────────────────────────────────────────────────────

class TestTradeJournalStats:
    def test_stats_total_count(self, journal):
        journal.add(_minimal_record(won=True))
        journal.add(_minimal_record(won=False))
        assert journal.stats()["total"] == 2

    def test_stats_wins_count(self, journal):
        journal.add(_minimal_record(won=True))
        journal.add(_minimal_record(won=True))
        journal.add(_minimal_record(won=False))
        assert journal.stats()["wins"] == 2

    def test_stats_losses_count(self, journal):
        journal.add(_minimal_record(won=True))
        journal.add(_minimal_record(won=False))
        journal.add(_minimal_record(won=False))
        assert journal.stats()["losses"] == 2

    def test_stats_win_rate_100_pct(self, journal):
        for _ in range(4):
            journal.add(_minimal_record(won=True))
        assert journal.stats()["win_rate"] == 100.0

    def test_stats_win_rate_0_pct(self, journal):
        for _ in range(4):
            journal.add(_minimal_record(won=False))
        assert journal.stats()["win_rate"] == 0.0

    def test_stats_win_rate_50_pct(self, journal):
        journal.add(_minimal_record(won=True))
        journal.add(_minimal_record(won=False))
        assert journal.stats()["win_rate"] == 50.0

    def test_stats_win_rate_rounded_to_1dp(self, journal):
        # 1 win out of 3 → 33.3%
        journal.add(_minimal_record(won=True))
        journal.add(_minimal_record(won=False))
        journal.add(_minimal_record(won=False))
        assert journal.stats()["win_rate"] == pytest.approx(33.3, abs=0.05)

    def test_stats_wins_plus_losses_equals_total(self, journal):
        for i in range(7):
            journal.add(_minimal_record(won=(i % 2 == 0)))
        s = journal.stats()
        assert s["wins"] + s["losses"] == s["total"]


# ── TradeJournal save / reload ────────────────────────────────────────────────

class TestTradeJournalPersistence:
    def test_round_trip_preserves_record_count(self, tmp_path, monkeypatch):
        path = str(tmp_path / "journal.json")
        monkeypatch.setattr(_tj_module, "JOURNAL_FILE", path)

        j1 = TradeJournal()
        j1.add(_minimal_record(trade_id="r1", pnl=5.0, won=True))
        j1.add(_minimal_record(trade_id="r2", pnl=-3.0, won=False))

        j2 = TradeJournal()   # re-reads from same path
        assert len(j2.records) == 2

    def test_round_trip_preserves_pnl(self, tmp_path, monkeypatch):
        path = str(tmp_path / "journal.json")
        monkeypatch.setattr(_tj_module, "JOURNAL_FILE", path)

        j1 = TradeJournal()
        j1.add(_minimal_record(trade_id="rx", pnl=7.77))

        j2 = TradeJournal()
        assert j2.records[0].pnl == pytest.approx(7.77)

    def test_round_trip_preserves_won_flag(self, tmp_path, monkeypatch):
        path = str(tmp_path / "journal.json")
        monkeypatch.setattr(_tj_module, "JOURNAL_FILE", path)

        j1 = TradeJournal()
        j1.add(_minimal_record(won=True))
        j1.add(_minimal_record(won=False))

        j2 = TradeJournal()
        assert j2.records[0].won is True
        assert j2.records[1].won is False

    def test_round_trip_preserves_direction(self, tmp_path, monkeypatch):
        path = str(tmp_path / "journal.json")
        monkeypatch.setattr(_tj_module, "JOURNAL_FILE", path)

        j1 = TradeJournal()
        j1.add(_minimal_record(direction="short"))

        j2 = TradeJournal()
        assert j2.records[0].direction == "short"


# ── TradeJournal.reload ───────────────────────────────────────────────────────

class TestTradeJournalReload:
    """reload() lets a long-lived reader (e.g. StrategyAdvisor) pick up trades
    written to disk by a separate TradeJournal instance (the trading loop's)."""

    def test_reload_picks_up_records_written_by_another_instance(self, tmp_path, monkeypatch):
        path = str(tmp_path / "journal.json")
        monkeypatch.setattr(_tj_module, "JOURNAL_FILE", path)

        writer = TradeJournal()
        reader = TradeJournal()
        assert reader.records == []

        writer.add(_minimal_record(trade_id="r1", pnl=5.0, won=True))
        reader.reload()
        assert len(reader.records) == 1
        assert reader.records[0].trade_id == "r1"

    def test_reload_again_picks_up_subsequent_writes(self, tmp_path, monkeypatch):
        path = str(tmp_path / "journal.json")
        monkeypatch.setattr(_tj_module, "JOURNAL_FILE", path)

        writer = TradeJournal()
        reader = TradeJournal()

        writer.add(_minimal_record(trade_id="r1"))
        reader.reload()
        assert len(reader.records) == 1

        writer.add(_minimal_record(trade_id="r2"))
        reader.reload()
        assert len(reader.records) == 2
        assert [r.trade_id for r in reader.records] == ["r1", "r2"]

    def test_reload_on_missing_file_keeps_existing_records(self, tmp_path, monkeypatch):
        path = str(tmp_path / "journal.json")
        monkeypatch.setattr(_tj_module, "JOURNAL_FILE", path)

        j = TradeJournal()
        j.add(_minimal_record(trade_id="kept"))
        assert os.path.exists(path)

        os.remove(path)
        j.reload()
        assert len(j.records) == 1
        assert j.records[0].trade_id == "kept"

    def test_reload_on_corrupted_file_keeps_existing_records(self, tmp_path, monkeypatch):
        path = str(tmp_path / "journal.json")
        monkeypatch.setattr(_tj_module, "JOURNAL_FILE", path)

        j = TradeJournal()
        j.add(_minimal_record(trade_id="kept"))

        with open(path, "w") as f:
            f.write("{not valid json")

        j.reload()
        assert len(j.records) == 1
        assert j.records[0].trade_id == "kept"


# ── TradeJournal._DEFAULTS backward-compat ───────────────────────────────────

class TestTradeJournalDefaults:
    """Old records on disk that are missing new fields get _DEFAULTS applied."""

    def test_old_record_missing_ofi_gets_default(self, tmp_path, monkeypatch):
        path = str(tmp_path / "journal.json")
        monkeypatch.setattr(_tj_module, "JOURNAL_FILE", path)
        os.makedirs(os.path.dirname(path), exist_ok=True)

        # Write a minimal record without the 'ofi' field
        old_record = {
            "trade_id": "old_1", "symbol": "BTC/USD",
            "opened_at": "2023-01-01T00:00:00", "closed_at": "2023-01-01T00:05:00",
            "rsi": 50.0, "adx": 20.0, "volume_ratio": 1.0, "regime": "RANGING",
            "atr_pct": 0.3, "ema100_gap": 0.0, "ema200_gap": 0.0,
            "hour_utc": 10, "day_of_week": 2,
            "pnl": 1.0, "pnl_pct": 0.1, "won": True, "reason": "SIGNAL",
        }
        with open(path, "w") as f:
            json.dump([old_record], f)

        j = TradeJournal()
        assert j.records[0].ofi == 0.0            # default applied

    def test_old_record_missing_confidence_gets_default(self, tmp_path, monkeypatch):
        path = str(tmp_path / "journal.json")
        monkeypatch.setattr(_tj_module, "JOURNAL_FILE", path)
        os.makedirs(os.path.dirname(path), exist_ok=True)

        old_record = {
            "trade_id": "old_2", "symbol": "ETH/USD",
            "opened_at": "2023-01-01T00:00:00", "closed_at": "2023-01-01T00:05:00",
            "rsi": 55.0, "adx": 25.0, "volume_ratio": 1.1, "regime": "TRENDING_UP",
            "atr_pct": 0.4, "ema100_gap": 0.0, "ema200_gap": 0.0,
            "hour_utc": 14, "day_of_week": 3,
            "pnl": 2.0, "pnl_pct": 0.2, "won": True, "reason": "TAKE_PROFIT",
        }
        with open(path, "w") as f:
            json.dump([old_record], f)

        j = TradeJournal()
        assert j.records[0].confidence == 0.0     # default applied
        assert j.records[0].direction  == "buy"   # default applied

    def test_old_record_missing_postmortem_fields_gets_defaults(self, tmp_path, monkeypatch):
        path = str(tmp_path / "journal.json")
        monkeypatch.setattr(_tj_module, "JOURNAL_FILE", path)
        os.makedirs(os.path.dirname(path), exist_ok=True)

        old_record = {
            "trade_id": "old_3", "symbol": "SOL/USD",
            "opened_at": "2023-06-01T00:00:00", "closed_at": "2023-06-01T00:05:00",
            "rsi": 60.0, "adx": 30.0, "volume_ratio": 1.3, "regime": "RANGING",
            "atr_pct": 0.6, "ema100_gap": 0.0, "ema200_gap": 0.0,
            "hour_utc": 8, "day_of_week": 0,
            "pnl": -1.0, "pnl_pct": -0.1, "won": False, "reason": "STOP_LOSS",
        }
        with open(path, "w") as f:
            json.dump([old_record], f)

        j = TradeJournal()
        r = j.records[0]
        assert r.mfe_pct           == 0.0
        assert r.mae_pct           == 0.0
        assert r.time_in_trade_sec == 0.0
        assert r.regime_at_exit    == ""
        assert r.exit_price        == 0.0


# ── TradeJournal.build_record ─────────────────────────────────────────────────

class TestTradeJournalBuildRecord:
    def test_returns_trade_record(self, journal):
        trade  = _make_trade()
        signal = _make_signal()
        result = journal.build_record(trade, "BTC/USD", "TAKE_PROFIT", signal)
        assert isinstance(result, TradeRecord)

    def test_symbol_set_correctly(self, journal):
        trade  = _make_trade()
        signal = _make_signal()
        r = journal.build_record(trade, "ETH/USD", "SIGNAL", signal)
        assert r.symbol == "ETH/USD"

    def test_reason_set_correctly(self, journal):
        trade  = _make_trade()
        signal = _make_signal()
        r = journal.build_record(trade, "BTC/USD", "STOP_LOSS", signal)
        assert r.reason == "STOP_LOSS"

    def test_pnl_rounded_to_4dp(self, journal):
        trade  = _make_trade(pnl=3.123456789)
        signal = _make_signal()
        r = journal.build_record(trade, "BTC/USD", "SIGNAL", signal)
        assert r.pnl == pytest.approx(3.1235, abs=1e-4)

    def test_pnl_pct_rounded_to_2dp(self, journal):
        trade  = _make_trade(pnl_pct=1.23456)
        signal = _make_signal()
        r = journal.build_record(trade, "BTC/USD", "SIGNAL", signal)
        assert r.pnl_pct == pytest.approx(1.23, abs=1e-2)

    def test_won_true_when_pnl_positive(self, journal):
        trade  = _make_trade(pnl=5.0)
        signal = _make_signal()
        r = journal.build_record(trade, "BTC/USD", "TAKE_PROFIT", signal)
        assert r.won is True

    def test_won_false_when_pnl_negative(self, journal):
        trade  = _make_trade(pnl=-3.0)
        signal = _make_signal()
        r = journal.build_record(trade, "BTC/USD", "STOP_LOSS", signal)
        assert r.won is False

    def test_won_false_when_pnl_zero(self, journal):
        trade  = _make_trade(pnl=0.0)
        signal = _make_signal()
        r = journal.build_record(trade, "BTC/USD", "SIGNAL", signal)
        assert r.won is False

    def test_rsi_from_signal(self, journal):
        trade  = _make_trade()
        signal = _make_signal(rsi=42.0)
        r = journal.build_record(trade, "BTC/USD", "SIGNAL", signal)
        assert r.rsi == 42.0

    def test_adx_from_signal(self, journal):
        trade  = _make_trade()
        signal = _make_signal(adx=35.0)
        r = journal.build_record(trade, "BTC/USD", "SIGNAL", signal)
        assert r.adx == 35.0

    def test_regime_from_signal(self, journal):
        trade  = _make_trade()
        signal = _make_signal(regime="VOLATILE")
        r = journal.build_record(trade, "BTC/USD", "SIGNAL", signal)
        assert r.regime == "VOLATILE"

    def test_atr_pct_computed_as_percentage_of_close(self, journal):
        trade  = _make_trade()
        # atr=500 at close=50000 → atr_pct = 500/50000*100 = 1.0
        signal = _make_signal(atr=500.0, close=50_000.0)
        r = journal.build_record(trade, "BTC/USD", "SIGNAL", signal)
        assert r.atr_pct == pytest.approx(1.0, rel=1e-4)

    def test_no_signal_uses_defaults(self, journal):
        trade = _make_trade()
        r = journal.build_record(trade, "BTC/USD", "SIGNAL", signal=None)
        assert r.rsi  == 50.0
        assert r.adx  == 20.0

    def test_ema_gap_zero_when_signal_lacks_ema100(self, journal):
        """ScientificSignal has no ema100/ema200 — gaps must default to 0.0."""
        trade  = _make_trade()
        signal = _make_signal()
        r = journal.build_record(trade, "BTC/USD", "SIGNAL", signal)
        assert r.ema100_gap == 0.0
        assert r.ema200_gap == 0.0

    def test_trade_id_contains_symbol(self, journal):
        trade  = _make_trade()
        signal = _make_signal()
        r = journal.build_record(trade, "BTC/USD", "SIGNAL", signal)
        assert "BTC" in r.trade_id or "BTC/USD" in r.trade_id

    def test_opened_at_is_string(self, journal):
        trade  = _make_trade()
        signal = _make_signal()
        r = journal.build_record(trade, "BTC/USD", "SIGNAL", signal)
        assert isinstance(r.opened_at, str)

    def test_closed_at_is_string(self, journal):
        trade  = _make_trade()
        signal = _make_signal()
        r = journal.build_record(trade, "BTC/USD", "SIGNAL", signal)
        assert isinstance(r.closed_at, str)

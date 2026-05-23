"""
Tests for position save/load round-trip (_save_open_positions / _load_open_positions).

Verifies that all critical position fields survive a restart cycle, including:
- Trailing stop state (tier, trail_style, trail_stop_price, intended_hold_min)
- Perp-specific state (is_perp, leverage, margin_locked, funding_accrued, last_funding_ts)
- Probability gate context (prob_win, edges_used)
- Entry context snapshot (spread_at_entry, sentiment_fng, sentiment_btc_dom)
- Core position fields (entry_price, size, side, peak excursions, cash, total_pnl)
"""

import json
import os
import pytest
from datetime import datetime, timezone

from src.paper_trading import (
    PaperTrader,
    PaperPosition,
    _save_open_positions,
    _load_open_positions,
    _POSITIONS_FILE,
)


# ── helpers ───────────────────────────────────────────────────────────────────

def _fresh_trader(capital: float = 1_000.0) -> PaperTrader:
    return PaperTrader(initial_capital=capital)


def _spot_position(
    entry_price: float = 50_000.0,
    size: float = 0.002,
    side: str = 'buy',
) -> PaperPosition:
    return PaperPosition(
        entry_time           = datetime(2024, 6, 1, 12, 0, 0, tzinfo=timezone.utc),
        entry_price          = entry_price,
        size                 = size,
        side                 = side,
        entry_fee            = 0.26,
        peak_favorable_price = entry_price * 1.01,
        peak_adverse_price   = entry_price * 0.99,
        entry_path           = 'main',
        size_usd_target      = 100.0,
        tier                 = 'swing',
        intended_hold_min    = 30,
        trail_style          = 'pct_trail',
        trail_stop_price     = entry_price * 0.985,
        target_usd_at_entry  = 103.0,
        prob_win             = 0.62,
        edges_used           = ['ofi', 'lead_lag', 'regime'],
        spread_at_entry      = 0.5,
        sentiment_fng        = 55,
        sentiment_btc_dom    = 52.3,
    )


def _perp_position(entry_price: float = 65_000.0) -> PaperPosition:
    return PaperPosition(
        entry_time           = datetime(2024, 6, 2, 8, 0, 0, tzinfo=timezone.utc),
        entry_price          = entry_price,
        size                 = 0.001,
        side                 = 'buy',
        entry_fee            = 0.05,
        peak_favorable_price = entry_price * 1.005,
        peak_adverse_price   = entry_price * 0.998,
        entry_path           = 'fast-track',
        size_usd_target      = 65.0,
        tier                 = 'scalp',
        intended_hold_min    = 5,
        trail_style          = 'atr_stop',
        trail_stop_price     = entry_price * 0.992,
        target_usd_at_entry  = 66.0,
        prob_win             = 0.58,
        edges_used           = ['ofi'],
        is_perp              = True,
        leverage             = 3.0,
        margin_locked        = 21.67,
        funding_accrued      = -0.015,
        last_funding_ts      = datetime(2024, 6, 2, 8, 0, 0, tzinfo=timezone.utc),
    )


# ── fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def clean_positions_file(tmp_path, monkeypatch):
    """Redirect _POSITIONS_FILE to a temp path so tests don't touch real data."""
    fake_path = str(tmp_path / 'open_positions.json')
    monkeypatch.setattr('src.paper_trading._POSITIONS_FILE', fake_path)
    yield fake_path


# ── round-trip tests ──────────────────────────────────────────────────────────

class TestCoreFieldsRoundTrip:
    def test_basic_fields_survive(self, clean_positions_file):
        trader = _fresh_trader()
        pos = _spot_position()
        trader.account.positions['BTC/USD'] = pos
        trader.account.cash = 900.0

        _save_open_positions(trader)

        trader2 = _fresh_trader()
        n = _load_open_positions(trader2)

        assert n == 1
        restored = trader2.account.positions['BTC/USD']
        assert restored.entry_price == pos.entry_price
        assert restored.size == pos.size
        assert restored.side == pos.side
        assert restored.entry_fee == pytest.approx(pos.entry_fee)

    def test_peak_excursion_fields_survive(self, clean_positions_file):
        trader = _fresh_trader()
        pos = _spot_position()
        trader.account.positions['ETH/USD'] = pos

        _save_open_positions(trader)

        trader2 = _fresh_trader()
        _load_open_positions(trader2)

        restored = trader2.account.positions['ETH/USD']
        assert restored.peak_favorable_price == pytest.approx(pos.peak_favorable_price)
        assert restored.peak_adverse_price == pytest.approx(pos.peak_adverse_price)

    def test_cash_and_pnl_restored(self, clean_positions_file):
        trader = _fresh_trader(capital=1_000.0)
        trader.account.positions['BTC/USD'] = _spot_position()
        trader.account.cash = 850.0
        trader.account.total_pnl = -12.5

        _save_open_positions(trader)

        trader2 = _fresh_trader(capital=1_000.0)
        _load_open_positions(trader2)

        assert trader2.account.cash == pytest.approx(850.0)
        assert trader2.account.total_pnl == pytest.approx(-12.5)

    def test_entry_path_preserved(self, clean_positions_file):
        trader = _fresh_trader()
        pos = _spot_position()
        pos.entry_path = 'mr-extreme'
        trader.account.positions['SOL/USD'] = pos

        _save_open_positions(trader)
        trader2 = _fresh_trader()
        _load_open_positions(trader2)

        assert trader2.account.positions['SOL/USD'].entry_path == 'mr-extreme'


class TestTrailingStopStateRoundTrip:
    def test_tier_survives(self, clean_positions_file):
        trader = _fresh_trader()
        trader.account.positions['BTC/USD'] = _spot_position()

        _save_open_positions(trader)
        trader2 = _fresh_trader()
        _load_open_positions(trader2)

        assert trader2.account.positions['BTC/USD'].tier == 'swing'

    def test_trail_style_survives(self, clean_positions_file):
        trader = _fresh_trader()
        trader.account.positions['BTC/USD'] = _spot_position()

        _save_open_positions(trader)
        trader2 = _fresh_trader()
        _load_open_positions(trader2)

        assert trader2.account.positions['BTC/USD'].trail_style == 'pct_trail'

    def test_trail_stop_price_survives(self, clean_positions_file):
        trader = _fresh_trader()
        pos = _spot_position()
        trader.account.positions['BTC/USD'] = pos

        _save_open_positions(trader)
        trader2 = _fresh_trader()
        _load_open_positions(trader2)

        restored = trader2.account.positions['BTC/USD']
        assert restored.trail_stop_price == pytest.approx(pos.trail_stop_price)

    def test_intended_hold_min_survives(self, clean_positions_file):
        trader = _fresh_trader()
        trader.account.positions['BTC/USD'] = _spot_position()

        _save_open_positions(trader)
        trader2 = _fresh_trader()
        _load_open_positions(trader2)

        assert trader2.account.positions['BTC/USD'].intended_hold_min == 30

    def test_target_usd_at_entry_survives(self, clean_positions_file):
        trader = _fresh_trader()
        pos = _spot_position()
        trader.account.positions['BTC/USD'] = pos

        _save_open_positions(trader)
        trader2 = _fresh_trader()
        _load_open_positions(trader2)

        assert trader2.account.positions['BTC/USD'].target_usd_at_entry == pytest.approx(103.0)


class TestPerpStateRoundTrip:
    def test_is_perp_flag_survives(self, clean_positions_file):
        trader = _fresh_trader()
        trader.account.positions['BTC/USD'] = _perp_position()

        _save_open_positions(trader)
        trader2 = _fresh_trader()
        _load_open_positions(trader2)

        assert trader2.account.positions['BTC/USD'].is_perp is True

    def test_leverage_survives(self, clean_positions_file):
        trader = _fresh_trader()
        trader.account.positions['BTC/USD'] = _perp_position()

        _save_open_positions(trader)
        trader2 = _fresh_trader()
        _load_open_positions(trader2)

        assert trader2.account.positions['BTC/USD'].leverage == pytest.approx(3.0)

    def test_margin_locked_survives(self, clean_positions_file):
        trader = _fresh_trader()
        trader.account.positions['BTC/USD'] = _perp_position()

        _save_open_positions(trader)
        trader2 = _fresh_trader()
        _load_open_positions(trader2)

        assert trader2.account.positions['BTC/USD'].margin_locked == pytest.approx(21.67)

    def test_funding_accrued_survives(self, clean_positions_file):
        """Accrued funding must survive restart to avoid double-accrual of fees."""
        trader = _fresh_trader()
        trader.account.positions['BTC/USD'] = _perp_position()

        _save_open_positions(trader)
        trader2 = _fresh_trader()
        _load_open_positions(trader2)

        assert trader2.account.positions['BTC/USD'].funding_accrued == pytest.approx(-0.015)

    def test_last_funding_ts_survives(self, clean_positions_file):
        """last_funding_ts must be persisted to prevent re-accruing from entry_time on restart."""
        trader = _fresh_trader()
        pos = _perp_position()
        trader.account.positions['BTC/USD'] = pos

        _save_open_positions(trader)
        trader2 = _fresh_trader()
        _load_open_positions(trader2)

        restored_ts = trader2.account.positions['BTC/USD'].last_funding_ts
        assert restored_ts is not None
        assert restored_ts == pos.last_funding_ts

    def test_last_funding_ts_none_handled(self, clean_positions_file):
        """Positions with last_funding_ts=None don't break on load."""
        trader = _fresh_trader()
        pos = _perp_position()
        pos.last_funding_ts = None
        trader.account.positions['BTC/USD'] = pos

        _save_open_positions(trader)
        trader2 = _fresh_trader()
        _load_open_positions(trader2)

        assert trader2.account.positions['BTC/USD'].last_funding_ts is None

    def test_spot_position_is_perp_false_by_default(self, clean_positions_file):
        trader = _fresh_trader()
        trader.account.positions['ETH/USD'] = _spot_position()

        _save_open_positions(trader)
        trader2 = _fresh_trader()
        _load_open_positions(trader2)

        assert trader2.account.positions['ETH/USD'].is_perp is False
        assert trader2.account.positions['ETH/USD'].leverage == pytest.approx(1.0)


class TestProbabilityGateContextRoundTrip:
    def test_prob_win_survives(self, clean_positions_file):
        trader = _fresh_trader()
        trader.account.positions['BTC/USD'] = _spot_position()

        _save_open_positions(trader)
        trader2 = _fresh_trader()
        _load_open_positions(trader2)

        assert trader2.account.positions['BTC/USD'].prob_win == pytest.approx(0.62)

    def test_edges_used_survives(self, clean_positions_file):
        trader = _fresh_trader()
        trader.account.positions['BTC/USD'] = _spot_position()

        _save_open_positions(trader)
        trader2 = _fresh_trader()
        _load_open_positions(trader2)

        assert trader2.account.positions['BTC/USD'].edges_used == ['ofi', 'lead_lag', 'regime']

    def test_edges_used_empty_list_survives(self, clean_positions_file):
        trader = _fresh_trader()
        pos = _spot_position()
        pos.edges_used = []
        trader.account.positions['SOL/USD'] = pos

        _save_open_positions(trader)
        trader2 = _fresh_trader()
        _load_open_positions(trader2)

        assert trader2.account.positions['SOL/USD'].edges_used == []


class TestEntryContextRoundTrip:
    def test_spread_at_entry_survives(self, clean_positions_file):
        trader = _fresh_trader()
        trader.account.positions['BTC/USD'] = _spot_position()

        _save_open_positions(trader)
        trader2 = _fresh_trader()
        _load_open_positions(trader2)

        assert trader2.account.positions['BTC/USD'].spread_at_entry == pytest.approx(0.5)

    def test_sentiment_fng_survives(self, clean_positions_file):
        trader = _fresh_trader()
        trader.account.positions['BTC/USD'] = _spot_position()

        _save_open_positions(trader)
        trader2 = _fresh_trader()
        _load_open_positions(trader2)

        assert trader2.account.positions['BTC/USD'].sentiment_fng == 55

    def test_sentiment_btc_dom_survives(self, clean_positions_file):
        trader = _fresh_trader()
        trader.account.positions['BTC/USD'] = _spot_position()

        _save_open_positions(trader)
        trader2 = _fresh_trader()
        _load_open_positions(trader2)

        assert trader2.account.positions['BTC/USD'].sentiment_btc_dom == pytest.approx(52.3)

    def test_sentiment_none_handled(self, clean_positions_file):
        trader = _fresh_trader()
        pos = _spot_position()
        pos.sentiment_fng = None
        pos.sentiment_btc_dom = None
        trader.account.positions['ETH/USD'] = pos

        _save_open_positions(trader)
        trader2 = _fresh_trader()
        _load_open_positions(trader2)

        restored = trader2.account.positions['ETH/USD']
        assert restored.sentiment_fng is None
        assert restored.sentiment_btc_dom is None


class TestMultiPositionRoundTrip:
    def test_multiple_symbols_all_restored(self, clean_positions_file):
        trader = _fresh_trader()
        trader.account.positions['BTC/USD'] = _spot_position(entry_price=50_000.0)
        trader.account.positions['ETH/USD'] = _spot_position(entry_price=3_000.0)
        trader.account.positions['SOL/USD'] = _spot_position(entry_price=150.0)

        _save_open_positions(trader)
        trader2 = _fresh_trader()
        n = _load_open_positions(trader2)

        assert n == 3
        assert set(trader2.account.positions.keys()) == {'BTC/USD', 'ETH/USD', 'SOL/USD'}

    def test_mixed_spot_and_perp_positions(self, clean_positions_file):
        trader = _fresh_trader()
        trader.account.positions['BTC/USD'] = _spot_position()
        trader.account.positions['ETH/USD'] = _perp_position(entry_price=3_000.0)

        _save_open_positions(trader)
        trader2 = _fresh_trader()
        _load_open_positions(trader2)

        assert trader2.account.positions['BTC/USD'].is_perp is False
        assert trader2.account.positions['ETH/USD'].is_perp is True
        assert trader2.account.positions['ETH/USD'].leverage == pytest.approx(3.0)

    def test_count_returned_correctly(self, clean_positions_file):
        trader = _fresh_trader()
        trader.account.positions['BTC/USD'] = _spot_position()
        trader.account.positions['ETH/USD'] = _spot_position()

        _save_open_positions(trader)
        trader2 = _fresh_trader()
        n = _load_open_positions(trader2)

        assert n == 2


class TestEdgeCases:
    def test_no_positions_file_returns_zero(self, clean_positions_file):
        trader = _fresh_trader()
        n = _load_open_positions(trader)
        assert n == 0
        assert trader.account.positions == {}

    def test_empty_positions_list_returns_zero(self, clean_positions_file):
        trader = _fresh_trader()
        # write an empty snapshot
        _save_open_positions(trader)

        trader2 = _fresh_trader()
        n = _load_open_positions(trader2)
        assert n == 0

    def test_corrupted_file_returns_zero_gracefully(self, clean_positions_file):
        with open(clean_positions_file, 'w') as f:
            f.write("NOT VALID JSON {{{")

        trader = _fresh_trader()
        n = _load_open_positions(trader)
        assert n == 0

    def test_cash_not_overwritten_if_saved_cash_higher(self, clean_positions_file):
        """If saved cash > trader's current cash, don't restore (we only restore if saved < current)."""
        trader = _fresh_trader(capital=1_000.0)
        trader.account.positions['BTC/USD'] = _spot_position()
        trader.account.cash = 2_000.0  # higher than initial (impossible in practice, but testing the guard)

        _save_open_positions(trader)

        trader2 = _fresh_trader(capital=1_000.0)  # starts with 1_000
        _load_open_positions(trader2)

        # Saved cash (2000) > trader2's cash (1000): guard says only restore if saved < current,
        # so cash should stay at 1000 (NOT overwritten to 2000).
        assert trader2.account.cash == pytest.approx(1_000.0)

    def test_json_file_is_valid_after_save(self, clean_positions_file):
        trader = _fresh_trader()
        trader.account.positions['BTC/USD'] = _perp_position()
        _save_open_positions(trader)

        with open(clean_positions_file) as f:
            data = json.load(f)

        assert 'saved_at' in data
        assert 'cash' in data
        assert 'positions' in data
        assert len(data['positions']) == 1
        p = data['positions'][0]
        assert p['symbol'] == 'BTC/USD'
        assert p['is_perp'] is True
        assert p['last_funding_ts'] is not None

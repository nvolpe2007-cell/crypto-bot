import json
from dataclasses import asdict

import pytest

from polymarket_bot import runner
from polymarket_bot.client import BinaryMarket
from polymarket_bot.fair_value import (
    edge_per_share,
    prob_up_endpoint,
    prob_up_window_twap,
    sigma_per_second,
    taker_fee_per_share,
)
from polymarket_bot.paper_sim import PaperPosition, PolymarketPaperState, settle_position


def test_endpoint_prob_is_half_at_the_money_and_monotone():
    assert prob_up_endpoint(100.0, 100.0, 300, 1e-4) == pytest.approx(0.5)
    assert prob_up_endpoint(100.0, 100.1, 300, 1e-4) > 0.5 > prob_up_endpoint(100.0, 99.9, 300, 1e-4)


def test_endpoint_prob_collapses_to_outcome_at_expiry():
    assert prob_up_endpoint(100.0, 100.01, 0, 1e-4) == 1.0
    assert prob_up_endpoint(100.0, 99.99, 0, 1e-4) == 0.0


def test_window_twap_locks_in_elapsed_average():
    # Average so far far above start, little time left -> Up nearly certain
    # even if spot has just dipped below the start.
    p = prob_up_window_twap(100.0, elapsed_avg=101.0, elapsed_s=840, spot=99.9, sigma_per_s=1e-4)
    assert p > 0.99


def test_sigma_per_second_scales_with_bar_length():
    closes = [100.0, 101.0, 100.0, 101.0, 100.0]
    assert sigma_per_second(closes, 60) == pytest.approx(sigma_per_second(closes, 15) / 2)
    assert sigma_per_second([100.0]) == 0.0


def test_fee_peaks_at_half_and_edge_nets_it():
    assert taker_fee_per_share(0.5) == pytest.approx(0.0175)
    assert taker_fee_per_share(0.9) < taker_fee_per_share(0.5)
    assert edge_per_share(0.60, 0.50) == pytest.approx(0.60 - 0.50 - 0.0175)


def _fee_position(entry: float, fee: float) -> PolymarketPaperState:
    state = PolymarketPaperState()
    state.open.append(asdict(PaperPosition("c", "q", "YES", "t", entry, 10.0, "2026-01-01T00:00:00+00:00",
                                           fee_usd=fee)))
    return state


def test_fee_aware_settlement_charges_fee_win_or_lose(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    win = _fee_position(0.5, 0.35)
    settle_position(win, "c", resolved_yes=True)
    assert win.closed[0]["pnl"] == pytest.approx(20.0 - 10.0 - 0.35)
    loss = _fee_position(0.5, 0.35)
    settle_position(loss, "c", resolved_yes=False)
    assert loss.closed[0]["pnl"] == pytest.approx(-10.35)


def _patch_live(monkeypatch, tmp_path, q_inputs, ask_up, ask_down, depth=1e6):
    monkeypatch.chdir(tmp_path)
    market = BinaryMarket("cond", "btc-updown-15m-0", "q", "UP_TOK", "DOWN_TOK", None, True, False)
    monkeypatch.setattr(runner, "fetch_window_market", lambda start: market)
    monkeypatch.setattr(runner, "kraken_btc", lambda start: q_inputs)
    books = {"UP_TOK": (ask_up, depth), "DOWN_TOK": (ask_down, depth)}
    monkeypatch.setattr(runner, "best_ask", lambda tok: books[tok])


def _decisions(tmp_path):
    return [json.loads(line) for line in (tmp_path / "data/polymarket_decisions.jsonl").read_text().splitlines()]


def test_runner_buys_the_underpriced_side_at_the_ask(monkeypatch, tmp_path):
    # Spot well above start with 5 min left -> fair P(Up) high; Up offered at 0.55.
    _patch_live(monkeypatch, tmp_path, (100.0, 100.3, 1e-4), ask_up=0.55, ask_down=0.47)
    state = PolymarketPaperState()
    runner.maybe_enter(state, now=600)
    assert len(state.open) == 1
    pos = state.open[0]
    assert pos["side"] == "YES" and pos["entry_price"] == 0.55
    assert pos["fee_usd"] == pytest.approx(taker_fee_per_share(0.55) * 10.0 / 0.55)
    assert _decisions(tmp_path)[0]["action"] == "BUY_UP"


def test_runner_holds_when_market_already_prices_it(monkeypatch, tmp_path):
    _patch_live(monkeypatch, tmp_path, (100.0, 100.0, 1e-4), ask_up=0.51, ask_down=0.51)
    state = PolymarketPaperState()
    runner.maybe_enter(state, now=600)
    assert state.open == []
    assert _decisions(tmp_path)[0]["action"] == "HOLD"  # non-trades are recorded too


def test_runner_respects_decision_window_and_one_per_window(monkeypatch, tmp_path):
    _patch_live(monkeypatch, tmp_path, (100.0, 100.3, 1e-4), ask_up=0.55, ask_down=0.47)
    state = PolymarketPaperState()
    runner.maybe_enter(state, now=60)       # minute 1: too early
    assert state.open == []
    runner.maybe_enter(state, now=600)
    runner.maybe_enter(state, now=660)      # same window again
    assert len(state.open) == 1


def test_runner_skips_when_book_too_thin(monkeypatch, tmp_path):
    _patch_live(monkeypatch, tmp_path, (100.0, 100.3, 1e-4), ask_up=0.55, ask_down=0.47, depth=1.0)
    state = PolymarketPaperState()
    runner.maybe_enter(state, now=600)
    assert state.open == []

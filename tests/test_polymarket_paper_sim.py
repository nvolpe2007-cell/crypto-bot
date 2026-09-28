from dataclasses import asdict

from polymarket_bot.paper_sim import (
    PaperPosition,
    PolymarketPaperState,
    settle_position,
)


def _state_with_position(side: str, entry_price: float, size_usd: float = 50.0) -> PolymarketPaperState:
    state = PolymarketPaperState()
    pos = PaperPosition(
        condition_id="cond1",
        question="Will BTC be up in 15 minutes?",
        side=side,
        token_id="tok1",
        entry_price=entry_price,
        size_usd=size_usd,
        opened_at="2026-01-01T00:00:00+00:00",
    )
    state.open.append(asdict(pos))
    return state


def test_settle_winning_yes_position_increases_equity(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    state = _state_with_position(side="YES", entry_price=0.5, size_usd=50.0)
    start_equity = state.equity

    settle_position(state, condition_id="cond1", resolved_yes=True)

    assert state.equity > start_equity
    assert state.open == []
    assert len(state.closed) == 1
    assert state.closed[0]["pnl"] > 0


def test_settle_losing_yes_position_loses_full_stake(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    state = _state_with_position(side="YES", entry_price=0.5, size_usd=50.0)
    start_equity = state.equity

    settle_position(state, condition_id="cond1", resolved_yes=False)

    assert state.equity == start_equity - 50.0
    assert state.closed[0]["pnl"] == -50.0


def test_settle_winning_no_position(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    state = _state_with_position(side="NO", entry_price=0.5, size_usd=50.0)

    settle_position(state, condition_id="cond1", resolved_yes=False)

    assert state.closed[0]["pnl"] > 0
    assert state.open == []


def test_settle_only_matches_condition_id(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    state = _state_with_position(side="YES", entry_price=0.5)
    other = asdict(
        PaperPosition(
            condition_id="cond2",
            question="other market",
            side="YES",
            token_id="tok2",
            entry_price=0.3,
            size_usd=50.0,
            opened_at="2026-01-01T00:00:00+00:00",
        )
    )
    state.open.append(other)

    settle_position(state, condition_id="cond1", resolved_yes=True)

    assert len(state.open) == 1
    assert state.open[0]["condition_id"] == "cond2"
    assert len(state.closed) == 1


def test_state_round_trips_through_save_and_load(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    state = _state_with_position(side="YES", entry_price=0.5)
    state.save()

    loaded = PolymarketPaperState.load()

    assert loaded.equity == state.equity
    assert loaded.open == state.open


def test_load_missing_file_returns_defaults(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    state = PolymarketPaperState.load()
    assert state.equity == state.start_equity
    assert state.open == []
    assert state.closed == []

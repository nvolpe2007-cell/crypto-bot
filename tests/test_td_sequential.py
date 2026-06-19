"""Unit tests for src/td_sequential.py — the list-based TD Sequential tag.

Verifies setup counting (buy = closes below close-4-back), the fresh-signal
string, countdown progression, and fail-open on warm-up.
"""
from src.td_sequential import td_state, _setups, _countdowns


def _bars(closes):
    return [{"o": c, "h": c + 1.0, "l": c - 1.0, "c": c} for c in closes]


def test_warmup_on_short_input():
    assert td_state([])["signal"] == "warmup"
    assert td_state(_bars([100, 101, 102]))["signal"] == "warmup"


def test_buy_setup_counts_down_closes():
    # strictly DECREASING closes → each close < close 4 bars prior → buy setup.
    closes = [100.0 - i for i in range(20)]
    buy, sell = _setups(closes)
    assert max(buy) >= 9                      # reaches a completed buy setup
    assert max(sell) == 0
    st = td_state(_bars(closes))
    assert st["signal"] in ("buy_setup_9", "buy_countdown_13")


def test_sell_setup_counts_up_closes():
    closes = [100.0 + i for i in range(20)]
    buy, sell = _setups(closes)
    assert max(sell) >= 9
    assert max(buy) == 0
    assert td_state(_bars(closes))["signal"] in ("sell_setup_9", "sell_countdown_13")


def test_buy_countdown_progresses_after_setup():
    # long decline → setup completes, then countdown (close <= low 2 bars prior)
    # increments and eventually completes (13).
    closes = [200.0 - i for i in range(40)]
    buy_setup, sell_setup = _setups(closes)
    buy_cd, _ = _countdowns(closes, [c + 1 for c in closes], [c - 1 for c in closes],
                            buy_setup, sell_setup)
    assert max(buy_cd) >= 13                  # countdown reaches completion


def test_no_signal_on_choppy_series():
    # alternating closes → no 4-bar-prior streak survives → no setup fires.
    closes = [100.0 + (5 if i % 2 else -5) for i in range(20)]
    st = td_state(_bars(closes))
    assert st["signal"] == "none"


def test_state_has_all_fields():
    st = td_state(_bars([100.0 - i for i in range(20)]))
    for k in ("buy_setup", "sell_setup", "buy_countdown", "sell_countdown", "signal"):
        assert k in st

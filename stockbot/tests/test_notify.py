"""stockbot Telegram poster tests — opt-in + fail-safe, and the report format."""
from stockbot.notify import enabled, post, format_report
from stockbot.metrics import summary
from stockbot.strategy import Trade


def _trade(net):
    return Trade("SPY", "2026-01-05", "long", "t0", 100.0, "t1", 101.0, "target",
                 net + 0.0004, 0.0004, net)


def test_disabled_by_default(monkeypatch):
    monkeypatch.delenv("STOCKBOT_TELEGRAM", raising=False)
    assert enabled() is False
    assert post("hi") is False                       # never posts when disabled


def test_enabled_but_no_token_is_failsafe(monkeypatch):
    monkeypatch.setenv("STOCKBOT_TELEGRAM", "1")
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    monkeypatch.delenv("STOCKBOT_TELEGRAM_CHAT_ID", raising=False)
    assert post("hi") is False                       # missing token/chat → no raise, False


def test_format_report_has_pnl_and_verdict():
    rets = [0.012, 0.008] * 25
    s = summary(rets)
    msg = format_report("SPY", s, [_trade(r) for r in rets])
    assert "SPY" in msg and "net" in msg
    assert "PROVEN" in msg or "NOT PROVEN" in msg or "FAILED" in msg


def test_format_report_dollars_when_capital_given():
    rets = [0.01] * 40
    s = summary(rets)
    msg = format_report("SPY", s, [_trade(r) for r in rets], capital=10000)
    assert "$" in msg                                # net % translated to $


def test_format_report_no_trades():
    assert "no trades" in format_report("SPY", summary([]), [])

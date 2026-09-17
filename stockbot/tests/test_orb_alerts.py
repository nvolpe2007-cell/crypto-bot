"""ORB interactive alert tests -- scan/alert + button-response handling, all via
fakes (no network, no Telegram, no Alpaca, no matplotlib required)."""
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from stockbot import notify, orb_alerts
from stockbot.strategy import ORBConfig

ET = ZoneInfo("America/New_York")
CFG = ORBConfig(or_minutes=15, direction="long", target_r=2.0)
DAY = "2026-01-05"


def _bars():
    rows = [
        ("09:30", 100, 101, 99, 100),
        ("09:35", 100, 101, 99.5, 100.5),
        ("09:40", 100.5, 101, 100, 100.8),     # OR high=101, low=99
        ("09:45", 100.8, 102, 100.5, 101.8),   # breaks above 101
    ]
    idx = pd.DatetimeIndex([pd.Timestamp(f"{DAY} {t}") for t, *_ in rows])
    return pd.DataFrame([(o, h, l, c, 1000.0) for _, o, h, l, c in rows],
                        columns=["open", "high", "low", "close", "volume"], index=idx)


def _now():
    return datetime(2026, 1, 5, 9, 45, tzinfo=ET)


@pytest.fixture(autouse=True)
def _telegram_on(monkeypatch):
    monkeypatch.setenv("STOCKBOT_TELEGRAM", "1")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.setenv("STOCKBOT_TELEGRAM_CHAT_ID", "42")


# ── scan_and_alert ───────────────────────────────────────────────────────────

def test_noop_when_telegram_disabled(monkeypatch):
    monkeypatch.delenv("STOCKBOT_TELEGRAM", raising=False)
    state = {}
    out = orb_alerts.scan_and_alert(["SPY"], CFG, 2000, _now(), state,
                                    fetch_bars=lambda s, n: _bars())
    assert out == []


def test_posts_alert_on_breakout(monkeypatch):
    posted = {}

    def fake_post_photo(caption, png, reply_markup=None):
        posted["caption"] = caption
        posted["reply_markup"] = reply_markup
        return 999

    monkeypatch.setattr(notify, "post_photo", fake_post_photo)
    # avoid a real matplotlib dependency in this test
    monkeypatch.setattr("stockbot.chart.render_orb_chart", lambda *a, **k: b"PNG")

    state = {}
    actions = orb_alerts.scan_and_alert(["SPY"], CFG, 2000, _now(), state,
                                        fetch_bars=lambda s, n: _bars())
    assert len(actions) == 1 and "SPY" in actions[0]
    assert state["alerted"][DAY] == ["SPY"]
    alert = state["pending"]["SPY_" + DAY]
    assert alert["side"] == "long" and alert["status"] == "pending"
    assert alert["stop"] == 99.0 and alert["target"] == 101.0 + 2 * 2.0
    assert "SPY" in posted["caption"]
    assert posted["reply_markup"]["inline_keyboard"][0][0]["callback_data"] == f"t:SPY_{DAY}"


def test_no_repeat_alert_same_day(monkeypatch):
    monkeypatch.setattr(notify, "post_photo", lambda *a, **k: 1)
    monkeypatch.setattr("stockbot.chart.render_orb_chart", lambda *a, **k: b"PNG")
    state = {}
    orb_alerts.scan_and_alert(["SPY"], CFG, 2000, _now(), state, fetch_bars=lambda s, n: _bars())
    actions2 = orb_alerts.scan_and_alert(["SPY"], CFG, 2000, _now(), state,
                                         fetch_bars=lambda s, n: _bars())
    assert actions2 == []


def test_falls_back_to_text_when_chart_import_fails(monkeypatch):
    posted = {}
    monkeypatch.setattr(notify, "post_photo", lambda *a, **k: None)
    monkeypatch.setattr(notify, "post", lambda text: posted.setdefault("text", text) or True)

    def _boom(*a, **k):
        raise ImportError("no matplotlib")
    monkeypatch.setattr("stockbot.chart.render_orb_chart", _boom)

    state = {}
    actions = orb_alerts.scan_and_alert(["SPY"], CFG, 2000, _now(), state,
                                        fetch_bars=lambda s, n: _bars())
    assert len(actions) == 1
    assert "SPY" in posted["text"]
    assert state["pending"]["SPY_" + DAY]["message_id"] is None


# ── handle_responses ─────────────────────────────────────────────────────────

class _FakeAlpaca:
    def __init__(self, avail=True, oid="ord_1"):
        self._avail = avail
        self._oid = oid
        self.orders = []

    def available(self):
        return self._avail

    def submit_bracket(self, symbol, qty, side, take_profit, stop_loss):
        self.orders.append((symbol, qty, side, take_profit, stop_loss))
        return self._oid


def _pending_state(status="pending"):
    return {"pending": {"SPY_" + DAY: {
        "symbol": "SPY", "date": DAY, "side": "long", "qty": 10,
        "entry": 101.0, "stop": 99.0, "target": 105.0,
        "message_id": 555, "status": status,
    }}, "next_offset": None}


def _callback_update(uid, action, alert_id):
    return {"update_id": uid,
           "callback_query": {"id": f"cq{uid}", "data": f"{action}:{alert_id}",
                              "message": {"chat": {"id": 42}, "message_id": 555}}}


def test_manual_button_acknowledged(monkeypatch):
    calls = []
    monkeypatch.setattr(notify, "get_updates", lambda offset=None: [_callback_update(1, "m", "SPY_" + DAY)])
    monkeypatch.setattr(notify, "answer_callback_query", lambda cid, text="": calls.append(("ack", text)))
    monkeypatch.setattr(notify, "edit_message_reply_markup", lambda c, m, rm=None: calls.append(("edit", c, m)))
    monkeypatch.setattr(notify, "post", lambda text: calls.append(("post", text)))

    state = _pending_state()
    actions = orb_alerts.handle_responses(state, client=_FakeAlpaca())
    assert state["pending"]["SPY_" + DAY]["status"] == "manual"
    assert state["next_offset"] == 2
    assert any(c[0] == "edit" for c in calls)
    assert len(actions) == 1


def test_trade_button_places_paper_order(monkeypatch):
    monkeypatch.setattr(notify, "get_updates", lambda offset=None: [_callback_update(5, "t", "SPY_" + DAY)])
    monkeypatch.setattr(notify, "answer_callback_query", lambda cid, text="": None)
    monkeypatch.setattr(notify, "edit_message_reply_markup", lambda c, m, rm=None: None)
    monkeypatch.setattr(notify, "post", lambda text: None)

    fake = _FakeAlpaca(avail=True, oid="ord_42")
    state = _pending_state()
    orb_alerts.handle_responses(state, client=fake)
    assert state["pending"]["SPY_" + DAY]["status"] == "traded"
    assert fake.orders == [("SPY", 10, "long", 105.0, 99.0)]


def test_trade_button_no_alpaca_keys_stays_safe(monkeypatch):
    monkeypatch.setattr(notify, "get_updates", lambda offset=None: [_callback_update(9, "t", "SPY_" + DAY)])
    notes = []
    monkeypatch.setattr(notify, "answer_callback_query", lambda cid, text="": notes.append(text))
    monkeypatch.setattr(notify, "edit_message_reply_markup", lambda c, m, rm=None: None)
    monkeypatch.setattr(notify, "post", lambda text: None)

    fake = _FakeAlpaca(avail=False)
    state = _pending_state()
    orb_alerts.handle_responses(state, client=fake)
    assert fake.orders == []
    assert state["pending"]["SPY_" + DAY]["status"] == "trade_failed"
    assert "keys" in notes[0].lower()


def test_already_resolved_alert_is_not_reexecuted(monkeypatch):
    monkeypatch.setattr(notify, "get_updates", lambda offset=None: [_callback_update(3, "t", "SPY_" + DAY)])
    notes = []
    monkeypatch.setattr(notify, "answer_callback_query", lambda cid, text="": notes.append(text))
    monkeypatch.setattr(notify, "edit_message_reply_markup", lambda c, m, rm=None: None)
    monkeypatch.setattr(notify, "post", lambda text: None)

    fake = _FakeAlpaca(avail=True)
    state = _pending_state(status="manual")
    orb_alerts.handle_responses(state, client=fake)
    assert fake.orders == []                    # never traded -- already resolved
    assert "already handled" in notes[0]


def test_unknown_alert_id_is_ignored(monkeypatch):
    monkeypatch.setattr(notify, "get_updates", lambda offset=None: [_callback_update(1, "t", "NOPE_" + DAY)])
    notes = []
    monkeypatch.setattr(notify, "answer_callback_query", lambda cid, text="": notes.append(text))
    state = {"pending": {}, "next_offset": None}
    actions = orb_alerts.handle_responses(state, client=_FakeAlpaca())
    assert actions == []
    assert "unknown" in notes[0]


def test_handle_responses_noop_when_disabled(monkeypatch):
    monkeypatch.delenv("STOCKBOT_TELEGRAM", raising=False)
    assert orb_alerts.handle_responses({"pending": {}}) == []

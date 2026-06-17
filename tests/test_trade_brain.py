"""Tests for the Claude-driven discretionary arm (src/trade_brain.py + brain_paper.py).

No network: a fake Anthropic client is injected, mirroring src/altperp/ai_brain tests.
"""

import importlib
import json
from datetime import datetime, timezone

import pytest

import src.trade_brain as tb
import brain_paper as bp
import brain_overseer as bo


# ── fake Anthropic plumbing ──────────────────────────────────────────────────

class _Block:
    def __init__(self, name, inp):
        self.type = "tool_use"; self.name = name; self.input = inp


class _Usage:
    input_tokens = 1200; output_tokens = 200


class _Resp:
    def __init__(self, decisions):
        self.content = [_Block("submit_decisions", {"decisions": decisions})]
        self.usage = _Usage()


class _FakeClient:
    def __init__(self, decisions):
        self._decisions = decisions
        self.messages = self
        self.last_kwargs = None

    def create(self, **kwargs):
        self.last_kwargs = kwargs
        return _Resp(self._decisions)


def _dec(coin, action, size=1.0, conv=7):
    return {"coin": coin, "action": action, "conviction": conv, "size_mult": size,
            "key_signal": "sma50 reclaim", "invalidation": "loses sma50", "reasoning": "trend up"}


# ── brain decode ─────────────────────────────────────────────────────────────

def test_decide_parses_per_coin():
    client = _FakeClient([_dec("BTC", "long"), _dec("ETH", "short", 1.5), _dec("SOL", "flat")])
    brain = tb.TradeBrain(client=client)
    res = brain.decide({"BTC": {}}, datetime.now(timezone.utc))
    assert res.ok
    assert res.decisions["BTC"].action == "long"
    assert res.decisions["ETH"].action == "short" and res.decisions["ETH"].size_mult == 1.5
    assert res.decisions["SOL"].action == "flat"


def test_size_mult_clamped():
    client = _FakeClient([_dec("BTC", "long", size=99.0)])
    res = tb.TradeBrain(client=client).decide({}, datetime.now(timezone.utc))
    assert res.decisions["BTC"].size_mult == 2.5            # hard clamp (aggressive cap)


def test_aggressive_size_allowed():
    client = _FakeClient([_dec("BTC", "long", size=2.3, conv=10)])
    res = tb.TradeBrain(client=client).decide({}, datetime.now(timezone.utc))
    assert res.decisions["BTC"].size_mult == 2.3            # high-conviction big bet preserved


def test_bad_action_defaults_flat():
    client = _FakeClient([{"coin": "BTC", "action": "yolo", "conviction": 9,
                           "size_mult": 1.0, "key_signal": "", "invalidation": "", "reasoning": ""}])
    res = tb.TradeBrain(client=client).decide({}, datetime.now(timezone.utc))
    assert res.decisions["BTC"].action == "flat"


def test_decide_fail_safe_on_error():
    class _Boom:
        messages = property(lambda self: self)
        def create(self, **k):
            raise RuntimeError("api down")
    brain = tb.TradeBrain(client=_Boom())
    res = brain.decide({}, datetime.now(timezone.utc))
    assert not res.ok and res.error and res.decisions == {}     # holds, never raises


def test_available_false_without_key_or_client(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert tb.TradeBrain(api_key="").available() is False
    assert tb.TradeBrain(client=_FakeClient([])).available() is True


# ── chart vision (multimodal user content) ───────────────────────────────────

def test_user_content_is_plain_string_without_charts():
    content = tb._build_user_content({"BTC": {}}, datetime.now(timezone.utc))
    assert isinstance(content, str) and "submit_decisions" in content


def test_user_content_attaches_image_blocks_with_charts():
    charts = {"BTC": "QkFTRTY0", "ETH": "aW1hZ2U="}
    content = tb._build_user_content({"BTC": {}}, datetime.now(timezone.utc), charts=charts)
    assert isinstance(content, list)
    imgs = [b for b in content if b.get("type") == "image"]
    assert len(imgs) == 2
    assert imgs[0]["source"] == {"type": "base64", "media_type": "image/png", "data": "QkFTRTY0"}
    assert content[0]["type"] == "text"                      # text picture leads


def test_empty_chart_value_is_skipped():
    charts = {"BTC": "abc", "ETH": ""}                       # ETH render failed → empty
    content = tb._build_user_content({}, datetime.now(timezone.utc), charts=charts)
    assert sum(1 for b in content if b.get("type") == "image") == 1


def test_multi_timeframe_charts_attach_two_images_per_coin():
    charts = {"BTC": [("BTC WEEKLY ...", "d2Vla2x5"), ("BTC DAILY ...", "ZGFpbHk=")]}
    content = tb._build_user_content({"BTC": {}}, datetime.now(timezone.utc), charts=charts)
    imgs = [b for b in content if b.get("type") == "image"]
    assert len(imgs) == 2
    assert imgs[0]["source"]["data"] == "d2Vla2x5"      # weekly first
    assert imgs[1]["source"]["data"] == "ZGFpbHk="       # then daily
    # the label text precedes each image
    texts = [b["text"] for b in content if b.get("type") == "text"]
    assert any("WEEKLY" in t for t in texts) and any("DAILY" in t for t in texts)


def test_coin_charts_accepts_both_str_and_list():
    assert tb._coin_charts("BTC", "abc") == [(
        "Daily candlestick chart for BTC "
        "(last ~140d; SMA50=blue, SMA100=orange, SMA200=purple):", "abc")]
    assert tb._coin_charts("ETH", [("wk", "x"), ("dy", "")]) == [("wk", "x")]   # empty dropped
    assert tb._coin_charts("SOL", None) == []


def test_decide_passes_images_through_to_client():
    client = _FakeClient([_dec("BTC", "long")])
    res = tb.TradeBrain(client=client).decide(
        {"BTC": {}}, datetime.now(timezone.utc), charts={"BTC": "QUJD"})
    assert res.ok
    sent = client.last_kwargs["messages"][0]["content"]
    assert isinstance(sent, list) and any(b.get("type") == "image" for b in sent)


def test_decide_without_charts_sends_string_content():
    client = _FakeClient([_dec("BTC", "long")])
    tb.TradeBrain(client=client).decide({"BTC": {}}, datetime.now(timezone.utc))
    assert isinstance(client.last_kwargs["messages"][0]["content"], str)


# ── runner: applying decisions to the paper account ──────────────────────────

def _state():
    return {"positions": {}, "closed": [], "last_bar_t": {}, "decisions": [],
            "starting_equity": 1000.0, "equity": 1000.0}


def test_apply_opens_long(monkeypatch):
    monkeypatch.setattr(bp, "BASE_ALLOC", 333.0)
    st = _state()
    d = tb.CoinDecision("BTC", action="long", size_mult=1.0)
    acted = bp.apply_decision(st, "BTC", d, price=100.0, ts="1000")
    assert acted == 1 and st["positions"]["BTC"]["side"] == 1
    assert st["positions"]["BTC"]["size_usd"] == 333.0


def test_apply_flip_long_to_short_books_pnl(monkeypatch):
    monkeypatch.setattr(bp, "BASE_ALLOC", 100.0)
    monkeypatch.setattr(bp, "COST_FRAC", 0.0)
    monkeypatch.setattr(bp, "FUNDING_APY", 0.0)
    st = _state()
    bp.apply_decision(st, "BTC", tb.CoinDecision("BTC", "long", 1.0), 100.0, "1000")
    acted = bp.apply_decision(st, "BTC", tb.CoinDecision("BTC", "short", 1.0), 120.0, "2000")
    assert acted == 2                                   # closed long + opened short
    assert st["closed"][0]["pnl"] == pytest.approx(20.0)   # long +20% on $100
    assert st["positions"]["BTC"]["side"] == -1


def test_apply_flat_closes_position(monkeypatch):
    monkeypatch.setattr(bp, "BASE_ALLOC", 100.0)
    monkeypatch.setattr(bp, "COST_FRAC", 0.0)
    monkeypatch.setattr(bp, "FUNDING_APY", 0.0)
    st = _state()
    bp.apply_decision(st, "BTC", tb.CoinDecision("BTC", "short", 1.0), 100.0, "1000")
    acted = bp.apply_decision(st, "BTC", tb.CoinDecision("BTC", "flat"), 80.0, "2000")
    assert acted == 1 and "BTC" not in st["positions"]
    assert st["closed"][0]["pnl"] == pytest.approx(20.0)   # short +20% as price fell 100->80


def test_apply_hold_same_side_no_action(monkeypatch):
    monkeypatch.setattr(bp, "BASE_ALLOC", 100.0)
    st = _state()
    bp.apply_decision(st, "BTC", tb.CoinDecision("BTC", "long", 1.0), 100.0, "1000")
    acted = bp.apply_decision(st, "BTC", tb.CoinDecision("BTC", "long", 1.0), 110.0, "2000")
    assert acted == 0 and st["positions"]["BTC"]["entry"] == 100.0   # untouched, no churn


def test_build_snapshot_has_factual_fields():
    closes = {"BTC": [100 + i for i in range(220)]}
    snap = bp.build_snapshot(closes, _state())
    assert "vs_sma50_pct" in snap["BTC"] and "ret_20d_pct" in snap["BTC"]
    assert snap["BTC"]["current_position"] == "flat"


# ── mark-to-market + drawdown stop ───────────────────────────────────────────

def test_mtm_equity_includes_open_unrealized(monkeypatch):
    monkeypatch.setattr(bp, "BASE_ALLOC", 100.0)
    st = _state()
    bp.apply_decision(st, "BTC", tb.CoinDecision("BTC", "short", 1.0), 100.0, "1000")
    # price rose 10% against a short on $100 → -$10 unrealized, realized still flat.
    assert bp.mtm_equity(st, {"BTC": 110.0}) == pytest.approx(990.0)
    assert st["equity"] == 1000.0   # realized untouched until close


def test_drawdown_stop_flattens_and_halts(monkeypatch):
    monkeypatch.setattr(bp, "BASE_ALLOC", 100.0)
    monkeypatch.setattr(bp, "COST_FRAC", 0.0)
    monkeypatch.setattr(bp, "FUNDING_APY", 0.0)
    monkeypatch.setattr(bp, "MAX_DRAWDOWN", 50.0)
    monkeypatch.setenv("BRAIN_NOTIFY", "0")
    st = _state()
    bp.apply_decision(st, "BTC", tb.CoinDecision("BTC", "short", 1.0), 100.0, "1000")
    now = datetime.now(timezone.utc)
    # price +60% → -$60 unrealized, breaches the -$50 cap.
    engaged = bp.maybe_drawdown_stop(st, {"BTC": 160.0}, {"BTC": {"t": 2000}}, now)
    assert engaged is True
    assert st["halted"] is True and "BTC" not in st["positions"]
    assert st["equity"] == pytest.approx(940.0)   # -$60 realized at the stop


def test_drawdown_stop_blocks_new_opens(monkeypatch):
    monkeypatch.setattr(bp, "BASE_ALLOC", 100.0)
    st = _state()
    st["halted"] = True
    acted = bp.apply_decision(st, "BTC", tb.CoinDecision("BTC", "long", 1.0),
                              100.0, "1000", allow_open=False)
    assert acted == 0 and "BTC" not in st["positions"]


def test_drawdown_stop_noop_within_cap(monkeypatch):
    monkeypatch.setattr(bp, "BASE_ALLOC", 100.0)
    monkeypatch.setattr(bp, "MAX_DRAWDOWN", 200.0)
    st = _state()
    bp.apply_decision(st, "BTC", tb.CoinDecision("BTC", "short", 1.0), 100.0, "1000")
    now = datetime.now(timezone.utc)
    # -$10 unrealized, well inside the -$200 cap → no stop.
    assert bp.maybe_drawdown_stop(st, {"BTC": 110.0}, {"BTC": {"t": 2000}}, now) is False
    assert st.get("halted", False) is False and "BTC" in st["positions"]


# ── portfolio overseer (advisory risk review across all arms) ─────────────────

class _ReviewResp:
    def __init__(self, payload):
        self.content = [_Block("submit_review", payload)]
        self.usage = _Usage()


class _ReviewClient:
    def __init__(self, payload):
        self._payload = payload
        self.messages = self

    def create(self, **kwargs):
        return _ReviewResp(self._payload)


def test_review_parses_flags_and_risk():
    payload = {"overall_risk": "Medium", "portfolio_note": "Book is one big short bet.",
               "flags": [{"arm": "AI Brain", "severity": "Warn", "note": "short into a bounce"}],
               "suggestions": ["consider trimming SOL short"]}
    res = tb.TradeBrain(client=_ReviewClient(payload)).review({}, datetime.now(timezone.utc))
    assert res.ok and res.overall_risk == "medium"          # normalised lower
    assert res.flags[0]["arm"] == "AI Brain" and res.flags[0]["severity"] == "warn"
    assert res.suggestions == ["consider trimming SOL short"]


def test_review_failsafe_on_no_tool_block():
    class _Empty:
        messages = property(lambda self: self)
    client = _FakeClient([])            # returns submit_decisions, not submit_review
    res = tb.TradeBrain(client=client).review({}, datetime.now(timezone.utc))
    assert res.ok is False and res.error is not None        # fail-safe → skip, no crash


def test_positions_brief_handles_dict_and_list():
    as_dict = bo._positions_brief({"BTC": {"symbol": "BTC", "side": -1, "entry": 100, "size_usd": 50}})
    as_list = bo._positions_brief([{"symbol": "ETH", "side": 1, "entry": 200, "size_usd": 60}])
    assert as_dict[0]["dir"] == "short" and as_dict[0]["symbol"] == "BTC"
    assert as_list[0]["dir"] == "long" and as_list[0]["size_usd"] == 60


def test_format_telegram_bounded_for_telegram_limit():
    res = tb.ReviewResult(overall_risk="high", portfolio_note="X" * 5000,
                          flags=[{"arm": "A" * 100, "severity": "critical",
                                  "note": "N" * 1000} for _ in range(20)],
                          suggestions=["S" * 1000 for _ in range(20)])
    msg = bo._format_telegram(res, datetime.now(timezone.utc))
    assert len(msg) <= 3900            # never exceed Telegram's cap


def test_positions_brief_handles_string_sides():
    # arms disagree on the representation: some store 'long'/'sell' not ±1.
    out = bo._positions_brief([{"symbol": "BTC", "side": "long"},
                               {"symbol": "ETH", "side": "sell"},
                               {"symbol": "SOL", "side": "weird"}])
    assert [p["dir"] for p in out] == ["long", "short", "?"]


def test_own_book_summary_uses_mtm_and_pnl(tmp_path):
    p = tmp_path / "x_state.json"
    p.write_text(json.dumps({"starting_equity": 1000, "equity": 1000, "equity_mtm": 944.71,
                             "positions": {"BTC": {"symbol": "BTC", "side": -1, "entry": 63000,
                                                   "size_usd": 466}},
                             "closed": [], "halted": False}))
    s = bo._own_book_summary(p, "AI Brain")
    assert s["equity_mtm"] == 944.71 and s["pnl"] == -55.29   # MTM preferred over realized
    assert s["open_positions"][0]["dir"] == "short"

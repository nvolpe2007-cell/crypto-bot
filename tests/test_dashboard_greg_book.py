"""Tests for the Greg paper-book panel in scripts/dashboard_server.py (read-only view of the
chief-of-staff bot's paper/ folder)."""
import json

from scripts import dashboard_server as ds


def _write_book(tmp_path, positions, trades_lines):
    (tmp_path / "positions.json").write_text(json.dumps(positions), encoding="utf-8")
    (tmp_path / "trades.jsonl").write_text("\n".join(trades_lines) + "\n", encoding="utf-8")


def test_missing_book_returns_none(tmp_path):
    assert ds._load_greg_book(str(tmp_path / "nope")) is None


def test_book_equity_stats_and_unrealized(tmp_path):
    _write_book(
        tmp_path,
        [{"id": 3, "pair": "BTC", "side": "long", "entry": 100.0, "stop": 95.0, "take": 110.0,
          "qty": 2.0, "risk_usd": 10.0, "reward_risk": 2.0, "thesis": "t", "opened_at": 1}],
        [json.dumps({"id": 1, "pair": "ETH", "side": "short", "entry": 10, "exit": 9, "reason": "target",
                     "pnl": 20.0, "r_multiple": 2.0, "closed_at": 5}),
         json.dumps({"id": 2, "pair": "SOL", "side": "long", "entry": 10, "exit": 9, "reason": "stop",
                     "pnl": -10.0, "r_multiple": -1.0, "closed_at": 6})],
    )
    g = ds._load_greg_book(str(tmp_path), prices={"BTC": 105.0})
    assert g["equity"] == 1010.0
    assert g["stats"] == {"n": 2, "win_rate": 0.5, "avg_r": 0.5, "total_pnl": 10.0}
    assert g["open"][0]["unrealized"] == 10.0 and g["unrealized"] == 10.0


def test_short_unrealized_sign_and_missing_price(tmp_path):
    _write_book(tmp_path, [
        {"id": 1, "pair": "ETH", "side": "short", "entry": 100.0, "qty": 1.0},
        {"id": 2, "pair": "SOL", "side": "long", "entry": 50.0, "qty": 1.0}], [])
    g = ds._load_greg_book(str(tmp_path), prices={"ETH": 90.0})
    assert g["open"][0]["unrealized"] == 10.0
    assert g["open"][1]["unrealized"] is None and g["open"][1]["price"] is None


def test_torn_ledger_line_is_skipped(tmp_path):
    _write_book(tmp_path, [], [json.dumps({"pnl": 5.0, "r_multiple": 0.5}), '{"torn'])
    g = ds._load_greg_book(str(tmp_path))
    assert g["stats"]["n"] == 1 and g["equity"] == 1005.0


def test_api_state_includes_book_and_page_escapes(tmp_path, monkeypatch):
    _write_book(tmp_path, [], [])
    monkeypatch.setattr(ds, "GREG_BOOK_DIR", str(tmp_path))
    monkeypatch.setattr(ds, "DATA_DIR", str(tmp_path / "data"))
    assert "greg_book" in ds._load_state()
    # free text (theses) is rendered through esc(), never raw
    assert "esc(p.thesis)" in ds._PAGE and "esc(t.pair)" in ds._PAGE

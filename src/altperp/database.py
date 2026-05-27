"""
SQLite logging for the alt-perp strategy. Two tables per the spec:
  signal_log — every signal evaluation (whether or not a trade fired)
  trades     — every opened/closed trade with full signal context at entry

Thread-safe enough for the single-loop use here: each call opens a short-lived
connection. Path comes from config.DB_PATH (data/trades.db by default).
"""

import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Optional, Dict

from . import config

_SIGNAL_LOG_DDL = """
CREATE TABLE IF NOT EXISTS signal_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp DATETIME,
    coin TEXT,
    price REAL,
    funding_rate REAL,
    funding_rate_48hr_avg REAL,
    oi_current REAL,
    oi_4hr_change_pct REAL,
    oi_8hr_change_pct REAL,
    perp_cvd_4hr REAL,
    spot_cvd_4hr REAL,
    cvd_divergence INTEGER,
    liq_proximity INTEGER,
    tier1_triggered INTEGER,
    tier2_score INTEGER,
    minutes_to_funding_reset INTEGER,
    setup_type TEXT,
    trade_fired INTEGER
);
"""

_TRADES_DDL = """
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    open_timestamp DATETIME,
    close_timestamp DATETIME,
    coin TEXT,
    direction TEXT,
    setup_type TEXT,
    entry_price REAL,
    exit_price REAL,
    position_size_usdt REAL,
    leverage INTEGER,
    tier2_active INTEGER,
    funding_at_entry REAL,
    oi_change_at_entry REAL,
    cvd_confirmed INTEGER,
    liq_proximity INTEGER,
    tp1_hit INTEGER,
    tp2_hit INTEGER,
    tp3_hit INTEGER,
    exit_reason TEXT,
    pnl_usdt REAL,
    pnl_pct REAL,
    fees_usdt REAL,
    net_pnl_usdt REAL
);
"""

# Every AI brain consultation (only fires when the structural gate passes), whether
# it confirmed or vetoed — the dataset for "what confidence cutoff predicts profit".
_AI_DECISIONS_DDL = """
CREATE TABLE IF NOT EXISTS ai_decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp DATETIME,
    coin TEXT,
    gate_setup_type TEXT,
    action TEXT,
    confidence INTEGER,
    size_multiplier REAL,
    key_signal TEXT,
    invalidation TEXT,
    urgency TEXT,
    reasoning TEXT,
    model TEXT,
    latency_ms INTEGER,
    input_tokens INTEGER,
    output_tokens INTEGER,
    trade_fired INTEGER,
    error TEXT
);
"""

# New signal_log columns added after the original schema shipped. _ensure_columns
# ALTERs them onto any pre-existing DB (CREATE TABLE IF NOT EXISTS won't add cols).
_SIGNAL_LOG_EXTRA_COLS = {
    "funding_velocity": "REAL",
    "funding_24h_change": "REAL",
    "basis": "REAL",
    "basis_compression": "REAL",
    "taker_ratio_short": "REAL",
    "taker_ratio_long": "REAL",
    "taker_divergence": "INTEGER",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def _conn(db_path: Optional[str] = None):
    path = db_path or config.DB_PATH
    os.makedirs(os.path.dirname(path), exist_ok=True)
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    try:
        yield con
        con.commit()
    finally:
        con.close()


def _ensure_columns(con, table: str, cols: Dict[str, str]):
    """Idempotently ALTER-add any missing columns (SQLite has no ADD COLUMN IF NOT EXISTS)."""
    existing = {r["name"] for r in con.execute(f"PRAGMA table_info({table})").fetchall()}
    for name, decl in cols.items():
        if name not in existing:
            con.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")


def init_db(db_path: Optional[str] = None):
    """Create tables if they don't exist + migrate new columns. Safe every startup."""
    with _conn(db_path) as con:
        con.execute(_SIGNAL_LOG_DDL)
        con.execute(_TRADES_DDL)
        con.execute(_AI_DECISIONS_DDL)
        _ensure_columns(con, "signal_log", _SIGNAL_LOG_EXTRA_COLS)


def log_signal(row: Dict, db_path: Optional[str] = None) -> int:
    """Insert one signal evaluation. Missing keys default to None/0."""
    fields = [
        "timestamp", "coin", "price", "funding_rate", "funding_rate_48hr_avg",
        "oi_current", "oi_4hr_change_pct", "oi_8hr_change_pct",
        "perp_cvd_4hr", "spot_cvd_4hr", "cvd_divergence", "liq_proximity",
        "tier1_triggered", "tier2_score", "minutes_to_funding_reset",
        "setup_type", "trade_fired",
        # signals #4/#7/#9/#10 (AI context)
        "funding_velocity", "funding_24h_change", "basis", "basis_compression",
        "taker_ratio_short", "taker_ratio_long", "taker_divergence",
    ]
    row = dict(row)
    row.setdefault("timestamp", _now())
    values = [row.get(f) for f in fields]
    with _conn(db_path) as con:
        cur = con.execute(
            f"INSERT INTO signal_log ({','.join(fields)}) "
            f"VALUES ({','.join('?' for _ in fields)})",
            values,
        )
        return cur.lastrowid


def open_trade(row: Dict, db_path: Optional[str] = None) -> int:
    """Insert a newly opened trade; returns its row id for later close update."""
    fields = [
        "open_timestamp", "coin", "direction", "setup_type", "entry_price",
        "position_size_usdt", "leverage", "tier2_active", "funding_at_entry",
        "oi_change_at_entry", "cvd_confirmed", "liq_proximity",
    ]
    row = dict(row)
    row.setdefault("open_timestamp", _now())
    values = [row.get(f) for f in fields]
    with _conn(db_path) as con:
        cur = con.execute(
            f"INSERT INTO trades ({','.join(fields)}) "
            f"VALUES ({','.join('?' for _ in fields)})",
            values,
        )
        return cur.lastrowid


def log_ai_decision(row: Dict, db_path: Optional[str] = None) -> int:
    """Insert one AI brain consultation (confirm or veto). Missing keys → None."""
    fields = [
        "timestamp", "coin", "gate_setup_type", "action", "confidence",
        "size_multiplier", "key_signal", "invalidation", "urgency", "reasoning",
        "model", "latency_ms", "input_tokens", "output_tokens", "trade_fired", "error",
    ]
    row = dict(row)
    row.setdefault("timestamp", _now())
    values = [row.get(f) for f in fields]
    with _conn(db_path) as con:
        cur = con.execute(
            f"INSERT INTO ai_decisions ({','.join(fields)}) "
            f"VALUES ({','.join('?' for _ in fields)})",
            values,
        )
        return cur.lastrowid


def close_trade(trade_id: int, updates: Dict, db_path: Optional[str] = None):
    """Patch a trade row on close (exit price/reason/pnl/tp flags/etc.)."""
    updates = dict(updates)
    updates.setdefault("close_timestamp", _now())
    allowed = {
        "close_timestamp", "exit_price", "tp1_hit", "tp2_hit", "tp3_hit",
        "exit_reason", "pnl_usdt", "pnl_pct", "fees_usdt", "net_pnl_usdt",
    }
    cols = [k for k in updates if k in allowed]
    if not cols:
        return
    with _conn(db_path) as con:
        con.execute(
            f"UPDATE trades SET {','.join(f'{c}=?' for c in cols)} WHERE id=?",
            [updates[c] for c in cols] + [trade_id],
        )


def closed_stats_since(iso_ts: str, db_path: Optional[str] = None) -> Dict:
    """Aggregate closed trades with close_timestamp >= iso_ts (for daily rollups)."""
    with _conn(db_path) as con:
        row = con.execute(
            "SELECT COUNT(*) n, COALESCE(SUM(net_pnl_usdt),0) net, "
            "COALESCE(SUM(CASE WHEN net_pnl_usdt>0 THEN 1 ELSE 0 END),0) wins "
            "FROM trades WHERE close_timestamp IS NOT NULL AND close_timestamp >= ?",
            (iso_ts,),
        ).fetchone()
        return {"count": row["n"], "net_pnl": round(row["net"], 4), "wins": row["wins"]}


def _selftest():
    import tempfile
    p = os.path.join(tempfile.mkdtemp(), "trades_test.db")
    init_db(p)
    sid = log_signal({"coin": "SOLUSDT", "price": 185.4, "tier1_triggered": 1,
                      "setup_type": "fade_short", "trade_fired": 1}, db_path=p)
    tid = open_trade({"coin": "SOLUSDT", "direction": "short", "setup_type": "fade_short",
                      "entry_price": 185.4, "position_size_usdt": 450, "leverage": 5,
                      "tier2_active": 1}, db_path=p)
    close_trade(tid, {"exit_price": 182.6, "exit_reason": "TP1",
                      "pnl_usdt": 6.8, "pnl_pct": 1.5, "net_pnl_usdt": 6.3,
                      "tp1_hit": 1}, db_path=p)
    with _conn(p) as con:
        assert con.execute("SELECT COUNT(*) FROM signal_log").fetchone()[0] == 1
        row = con.execute("SELECT exit_reason, net_pnl_usdt FROM trades WHERE id=?", (tid,)).fetchone()
        assert row["exit_reason"] == "TP1" and abs(row["net_pnl_usdt"] - 6.3) < 1e-9
    print(f"database selftest OK (sid={sid}, tid={tid})")


if __name__ == "__main__":
    _selftest()

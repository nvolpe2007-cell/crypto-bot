"""The test suite must never write to the real P&L ledger.

Regression guard for a live bug found 2026-09-06: `src/attribution.record()`
writes through a process-wide singleton defaulting to `data/attribution.db` --
the ledger behind the dashboard and the daily Telegram scorecard. Because
`regime_arm.py` and `arbitrage/funding_arb_paper.py` call `record()` directly,
merely exercising those paths in a test appended rows to PRODUCTION.

Measured damage before the fix: one `pytest tests/` run added 6 rows, and 94
accumulated rows carried reason='test' with a hardcoded net_pnl of 10.0, which
made the regime_intraday arm read as a 100%-win-rate, +$942 strategy. The stored
ledger totalled +$625.60; with test rows and duplicate writes removed, the real
distinct record was 2 fills and -$1.85.

tests/conftest.py claims the singleton first and points it at a temp file. These
tests fail if that guard is removed or stops working.
"""
import os
from pathlib import Path

import src.attribution as attribution

REPO_DATA = Path(__file__).resolve().parent.parent / 'data'
PROD_DB = REPO_DATA / 'attribution.db'


def test_singleton_is_not_the_production_database():
    led = attribution.get_ledger()
    assert Path(led.db_path).resolve() != PROD_DB.resolve(), (
        'the attribution singleton points at the PRODUCTION ledger; tests would '
        'corrupt the dashboard and Telegram P&L numbers'
    )


def test_singleton_is_outside_the_repo_data_directory():
    led = attribution.get_ledger()
    db = Path(led.db_path).resolve()
    assert REPO_DATA.resolve() not in db.parents, (
        f'attribution ledger {db} is inside {REPO_DATA}; it must be a temp file'
    )


def test_recording_does_not_touch_the_production_file():
    """The end-to-end guarantee: a record() call leaves the real DB untouched."""
    before = PROD_DB.stat().st_mtime_ns if PROD_DB.exists() else None
    before_size = PROD_DB.stat().st_size if PROD_DB.exists() else None

    ok = attribution.record('unit_test_arm', 'BTC/USD', side='buy',
                            size_usd=1.0, fees_paid=0.0, net_pnl=0.0,
                            reason='isolation_probe')
    assert ok is True

    if PROD_DB.exists():
        assert PROD_DB.stat().st_mtime_ns == before, 'production ledger was modified'
        assert PROD_DB.stat().st_size == before_size, 'production ledger grew'


def test_the_probe_row_landed_in_the_temp_ledger():
    """Complements the test above: prove the write actually happened somewhere,
    so a silently broken record() can't pass the 'production untouched' check."""
    led = attribution.get_ledger()
    assert os.path.exists(led.db_path)
    import sqlite3
    n = sqlite3.connect(led.db_path).execute(
        "SELECT COUNT(*) FROM fills WHERE arm='unit_test_arm'").fetchone()[0]
    assert n >= 1, 'record() reported success but wrote nothing anywhere'

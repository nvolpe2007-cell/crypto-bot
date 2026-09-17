#!/usr/bin/env python3
"""Audit (and optionally repair) the attribution ledger's P&L numbers.

WHY
    `data/attribution.db` is what the dashboard and the daily Telegram scorecard
    report as this system's realised P&L. On 2026-09-06 it was found to contain
    two kinds of junk that made those numbers meaningless:

      1. TEST ROWS. `src/attribution.record()` writes through a process-wide
         singleton defaulting to the production DB, and regime_arm.py /
         funding_arb_paper.py call it directly -- so running `pytest tests/`
         appended rows to production. 94 of them carried reason='test' with a
         hardcoded net_pnl of 10.0, which made the regime_intraday arm read as a
         100%-win-rate, +$942 strategy. (Root cause fixed in tests/conftest.py;
         guarded by tests/test_attribution_isolation.py.)

      2. DUPLICATE WRITES. The same funding_aggr fill appeared 153 times --
         identical symbol, size, fee and P&L, differing only in timestamp --
         turning one ~$2 cost into a $316 "loss".

    Net effect: the ledger read +$625.60 when the distinct real record was
    2 fills and -$1.85.

USAGE
    python scripts/attribution_forensics.py                # read-only report
    python scripts/attribution_forensics.py --apply        # repair (backs up first)

    --apply deletes rows whose `reason` is 'test' and collapses exact duplicate
    fills to one row each. It writes a timestamped .bak beside the database
    first and prints the before/after totals. Nothing is deleted without it.
"""
from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

DB = Path(__file__).resolve().parent.parent / 'data' / 'attribution.db'

# Rows whose `reason` marks them as synthetic rather than a real fill.
TEST_REASONS = ('test',)

# The columns that identify "the same fill written twice". Deliberately excludes
# ts and id: a genuine duplicate write differs only in when it was written.
DUP_KEY = ('arm', 'symbol', 'side', 'size_usd', 'fees_paid', 'gross_pnl',
           'net_pnl', 'reason')


def _totals(con: sqlite3.Connection) -> tuple[int, float]:
    n, s = con.execute('SELECT COUNT(*), COALESCE(SUM(net_pnl), 0) FROM fills').fetchone()
    return n, s


def _placeholders(reasons) -> str:
    return ','.join('?' for _ in reasons)


def report(con: sqlite3.Connection) -> dict:
    n, total = _totals(con)
    ph = _placeholders(TEST_REASONS)
    n_test, s_test = con.execute(
        f'SELECT COUNT(*), COALESCE(SUM(net_pnl),0) FROM fills WHERE reason IN ({ph})',
        TEST_REASONS).fetchone()

    key = ', '.join(DUP_KEY)
    dup_rows = list(con.execute(f"""
        SELECT {key}, COUNT(*) AS c
        FROM fills WHERE reason NOT IN ({ph}) OR reason IS NULL
        GROUP BY {key} HAVING c > 1 ORDER BY c DESC""", TEST_REASONS))
    n_dup_extra = sum(r[-1] - 1 for r in dup_rows)
    s_dup_extra = sum((r[DUP_KEY.index('net_pnl')] or 0) * (r[-1] - 1) for r in dup_rows)

    print('=' * 78)
    print(f'ATTRIBUTION LEDGER FORENSICS — {DB}')
    print('=' * 78)
    print(f'as stored                : n={n:<5} net=${total:+.2f}')
    print(f'  synthetic "test" rows  : n={n_test:<5} net=${s_test:+.2f}')
    print(f'  duplicate extra copies : n={n_dup_extra:<5} net=${s_dup_extra:+.2f}')
    print(f'REAL distinct record     : n={n - n_test - n_dup_extra:<5} '
          f'net=${total - s_test - s_dup_extra:+.2f}')

    print('\nPER-ARM, as stored vs cleaned:')
    print(f"  {'arm':<24}{'n':>6}{'win%':>7}{'net$':>11}   |{'n':>6}{'win%':>7}{'net$':>11}")
    for (arm,) in con.execute('SELECT DISTINCT arm FROM fills ORDER BY arm'):
        raw = list(con.execute('SELECT net_pnl FROM fills WHERE arm=?', (arm,)))
        clean = list(con.execute(f"""
            SELECT net_pnl FROM fills
            WHERE arm=? AND (reason NOT IN ({ph}) OR reason IS NULL)
            GROUP BY {key}""", (arm,) + TEST_REASONS))
        f = lambda rs: (len(rs),
                        (sum(1 for x in rs if (x[0] or 0) > 0) / len(rs) * 100) if rs else 0.0,
                        sum(x[0] or 0 for x in rs))
        a, b = f(raw), f(clean)
        print(f'  {arm:<24}{a[0]:>6}{a[1]:>6.0f}%{a[2]:>11.2f}   |'
              f'{b[0]:>6}{b[1]:>6.0f}%{b[2]:>11.2f}')

    if dup_rows:
        print('\nWorst duplicate groups:')
        for r in dup_rows[:5]:
            d = dict(zip(DUP_KEY, r))
            print(f"  {d['arm']:<16}{str(d['symbol']):<14} net={d['net_pnl'] or 0:+.4f} "
                  f"written {r[-1]}x")
    print('=' * 78)
    return {'n_test': n_test, 'n_dup_extra': n_dup_extra}


def repair(con: sqlite3.Connection) -> None:
    ph = _placeholders(TEST_REASONS)
    key = ', '.join(DUP_KEY)
    cur = con.cursor()
    cur.execute(f'DELETE FROM fills WHERE reason IN ({ph})', TEST_REASONS)
    removed_test = cur.rowcount
    # keep the EARLIEST id of each duplicate group -- the first write is the real one
    cur.execute(f"""
        DELETE FROM fills WHERE id NOT IN (
            SELECT MIN(id) FROM fills GROUP BY {key})""")
    removed_dup = cur.rowcount
    con.commit()
    print(f'\nremoved {removed_test} test rows and {removed_dup} duplicate rows')


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--apply', action='store_true',
                    help='actually delete the junk rows (backs up the DB first)')
    a = ap.parse_args()

    if not DB.exists():
        print(f'no ledger at {DB}', file=sys.stderr)
        return 1

    con = sqlite3.connect(DB)
    found = report(con)

    if not a.apply:
        if found['n_test'] or found['n_dup_extra']:
            print('\nread-only. Re-run with --apply to repair (a .bak is written first).')
        else:
            print('\nledger is clean.')
        return 0

    con.close()
    bak = DB.with_suffix(f'.{datetime.now():%Y%m%d-%H%M%S}.bak')
    shutil.copy2(DB, bak)
    print(f'\nbacked up to {bak}')
    con = sqlite3.connect(DB)
    repair(con)
    print()
    report(con)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

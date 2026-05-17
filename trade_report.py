#!/usr/bin/env python3
"""
Print a detailed report from the trade journal.

Usage:
    python trade_report.py          # full breakdown
    python trade_report.py --csv    # show CSV file path
    python trade_report.py --tail 10  # last 10 trades
"""
import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))

from src.trade_journal import TradeJournal, JOURNAL_FILE, CSV_FILE


def _print_recent(records, n):
    print(f"\n— Last {min(n, len(records))} trades —")
    for r in records[-n:]:
        flag = "WIN " if r.won else "LOSS"
        print(
            f"  {r.opened_at[:19]}  {r.symbol:<9} {r.direction:<5} "
            f"path={r.entry_path:<11} conf={r.confidence:>5.1f} "
            f"pnl=${r.pnl:+.3f} ({r.pnl_pct:+.2f}%) "
            f"mfe={r.mfe_pct:+.2f}% mae={r.mae_pct:+.2f}% "
            f"hold={r.time_in_trade_sec:.0f}s reason={r.reason}  [{flag}]"
        )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--tail', type=int, default=0,
                        help='Show last N trades instead of full report')
    parser.add_argument('--csv', action='store_true',
                        help='Print path to the CSV trade log')
    args = parser.parse_args()

    if args.csv:
        print(CSV_FILE)
        print(f"Exists: {os.path.exists(CSV_FILE)}")
        if os.path.exists(CSV_FILE):
            print(f"Size:   {os.path.getsize(CSV_FILE)} bytes")
        return

    journal = TradeJournal()

    if not journal.records:
        print("No trades recorded yet.")
        print(f"Journal: {JOURNAL_FILE}")
        return

    if args.tail:
        _print_recent(journal.records, args.tail)
        return

    journal.print_report()
    _print_recent(journal.records, 5)
    print(f"\nJSON: {JOURNAL_FILE}")
    print(f"CSV:  {CSV_FILE}")


if __name__ == '__main__':
    main()

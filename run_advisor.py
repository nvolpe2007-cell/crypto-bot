#!/usr/bin/env python3
"""
Strategy Advisor — standalone runner
Can run on the VPS (or locally) separately from the main bot.

Usage:
    python run_advisor.py
    python run_advisor.py --now          # send a report right now and exit
    python run_advisor.py --eod          # send end-of-day report now and exit
"""

import argparse
import asyncio
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from dotenv import load_dotenv
load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s  %(message)s',
    datefmt='%H:%M:%S',
)


def main():
    parser = argparse.ArgumentParser(description='Strategy Advisor Agent')
    parser.add_argument('--now',  action='store_true', help='Send hourly check-in now and exit')
    parser.add_argument('--eod',  action='store_true', help='Send end-of-day report now and exit')
    args = parser.parse_args()

    from src.notifications import create_notifier_from_env
    from src.trade_journal import TradeJournal
    from src.strategy_advisor import (
        StrategyAdvisor, run_advisor_standalone,
        _hourly_message, _eod_message
    )

    notifier = create_notifier_from_env()
    journal  = TradeJournal()

    if args.now:
        msg = _hourly_message(journal)
        ok  = notifier.send_message(msg)
        print("Sent hourly check-in" if ok else "Failed to send")
        return

    if args.eod:
        msg = _eod_message(journal)
        ok  = notifier.send_message(msg)
        print("Sent end-of-day report" if ok else "Failed to send")
        return

    print()
    print("  ◆ STRATEGY ADVISOR AGENT")
    print("  Sends hourly check-ins (12–21 UTC) + end-of-day report")
    print(f"  Loaded {len(journal.records)} historical trades")
    print()
    print("  Ctrl+C to stop")
    print()

    asyncio.run(run_advisor_standalone())


if __name__ == '__main__':
    main()

"""Entry point: python -m alpaca_tjr.main"""
from __future__ import annotations

import logging
import os
import sys

# Allow running from the crypto-bot root: python -m alpaca_tjr.main
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from alpaca_tjr.bot import AlpacaTJRBot


def _setup_logging(level: str = "INFO") -> None:
    os.makedirs("logs", exist_ok=True)
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler("logs/alpaca_tjr.log"),
        ],
    )


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Alpaca TJR Day-Trading Bot")
    parser.add_argument(
        "--config",
        default="alpaca_tjr/config.yaml",
        help="Path to config.yaml (default: alpaca_tjr/config.yaml)",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    args = parser.parse_args()

    _setup_logging(args.log_level)
    bot = AlpacaTJRBot(config_path=args.config)
    bot.run()

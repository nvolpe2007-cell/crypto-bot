#!/usr/bin/env python3
"""
Polymarket LIVE order tool — human-in-the-loop only. Run this yourself, on
your own machine, with your own wallet. Nothing in this repository, and no
agent session, ever runs this automatically or holds your private key.

SAFETY MODEL
------------
- Your private key is read ONLY from the POLYMARKET_PRIVATE_KEY environment
  variable on the machine YOU run this on. It is never logged, printed,
  written to a file, or sent anywhere except Polymarket's own CLOB API (via
  py-clob-client, to locally sign your order — signing happens on your
  machine, the key itself is never transmitted).
- Every invocation is DRY-RUN by default: it builds and prints the exact
  order (market, side, price, size, notional, est. fees) without submitting
  anything.
- Submitting for real requires BOTH `--live` and typing the literal word
  CONFIRM at an interactive prompt naming the exact order. There is no way
  to submit non-interactively (no --yes / --force flag) — that's deliberate.
- This tool does not decide anything for you. `polymarket_bot/signal.py` is
  a placeholder (always HOLD); you choose the market/side/size yourself
  each time you run this.

SETUP (do this yourself, never share your key with anyone, including me)
--------------------------------------------------------------------
    pip install -r polymarket_bot/requirements_live.txt
    export POLYMARKET_PRIVATE_KEY="0x..."          # your wallet's private key
    export POLYMARKET_FUNDER_ADDRESS="0x..."        # your Polymarket proxy wallet address

USAGE
-----
    # 1. Look up a market's token ids (read-only, no key needed):
    python -m polymarket_bot.live_execution search "bitcoin up or down"

    # 2. Preview an order (dry-run, default — no key needed for this step
    #    either, since nothing is submitted):
    python -m polymarket_bot.live_execution order \\
        --token-id <YES_or_NO_token_id> --side BUY --price 0.55 --size 10

    # 3. Actually submit (needs the env vars above set, and typed confirmation):
    python -m polymarket_bot.live_execution order \\
        --token-id <token_id> --side BUY --price 0.55 --size 10 --live
"""

from __future__ import annotations

import argparse
import os
import sys

from polymarket_bot.client import search_markets, get_midpoint_price

CLOB_HOST = "https://clob.polymarket.com"
POLYGON_CHAIN_ID = 137


def _cmd_search(args: argparse.Namespace) -> None:
    markets = search_markets(args.query, limit=args.limit)
    if not markets:
        print("No matching active markets found.")
        return
    for m in markets:
        print(f"\n{m.question}")
        print(f"  slug:          {m.slug}")
        print(f"  condition_id:  {m.condition_id}")
        print(f"  yes_token_id:  {m.yes_token_id}")
        print(f"  no_token_id:   {m.no_token_id}")
        print(f"  end_date:      {m.end_date_iso}")


def _build_order_preview(token_id: str, side: str, price: float, size: float) -> dict:
    notional = round(price * size, 4)
    return {
        "token_id": token_id,
        "side": side,
        "price": price,
        "size": size,
        "notional_usdc": notional,
        "current_midpoint": get_midpoint_price(token_id),
    }


def _print_preview(preview: dict) -> None:
    print("\n--- ORDER PREVIEW (nothing submitted yet) ---")
    for k, v in preview.items():
        print(f"  {k}: {v}")
    print("----------------------------------------------")


def _cmd_order(args: argparse.Namespace) -> None:
    preview = _build_order_preview(args.token_id, args.side, args.price, args.size)
    _print_preview(preview)

    if not args.live:
        print("\nDry-run only. Re-run with --live to actually submit this order.")
        return

    private_key = os.environ.get("POLYMARKET_PRIVATE_KEY")
    funder = os.environ.get("POLYMARKET_FUNDER_ADDRESS")
    if not private_key or not funder:
        print(
            "\nERROR: --live requires POLYMARKET_PRIVATE_KEY and "
            "POLYMARKET_FUNDER_ADDRESS to be set in your own shell environment. "
            "Not proceeding.",
            file=sys.stderr,
        )
        sys.exit(1)

    confirm_prompt = (
        f"\nType CONFIRM to submit this REAL order "
        f"({args.side} {args.size} @ {args.price} on token {args.token_id}): "
    )
    typed = input(confirm_prompt)
    if typed.strip() != "CONFIRM":
        print("Not confirmed. Nothing submitted.")
        return

    try:
        from py_clob_client.client import ClobClient
        from py_clob_client.clob_types import OrderArgs
        from py_clob_client.order_builder.constants import BUY, SELL
    except ImportError:
        print(
            "\nERROR: py-clob-client is not installed. "
            "Run: pip install -r polymarket_bot/requirements_live.txt",
            file=sys.stderr,
        )
        sys.exit(1)

    client = ClobClient(
        host=CLOB_HOST,
        key=private_key,
        chain_id=POLYGON_CHAIN_ID,
        funder=funder,
    )
    client.set_api_creds(client.create_or_derive_api_creds())

    order_args = OrderArgs(
        token_id=args.token_id,
        price=args.price,
        size=args.size,
        side=BUY if args.side == "BUY" else SELL,
    )
    signed_order = client.create_order(order_args)
    resp = client.post_order(signed_order)
    print("\nSubmitted. Response:")
    print(resp)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    p_search = sub.add_parser("search", help="Find markets (read-only, no key needed)")
    p_search.add_argument("query", help="Keyword, e.g. 'bitcoin up or down'")
    p_search.add_argument("--limit", type=int, default=20)
    p_search.set_defaults(func=_cmd_search)

    p_order = sub.add_parser("order", help="Preview or submit an order")
    p_order.add_argument("--token-id", required=True)
    p_order.add_argument("--side", choices=["BUY", "SELL"], required=True)
    p_order.add_argument("--price", type=float, required=True, help="Limit price, 0-1")
    p_order.add_argument("--size", type=float, required=True, help="Number of shares")
    p_order.add_argument(
        "--live",
        action="store_true",
        help="Actually submit (requires env vars + typed confirmation). Omit for dry-run.",
    )
    p_order.set_defaults(func=_cmd_order)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()

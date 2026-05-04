import argparse
import asyncio
import logging
import os
import sys

import aiohttp
import yaml
from dotenv import load_dotenv

from contacted_log import BusinessRecord, ContactedLog
from email_finder import EmailFinderBot
from email_sender import EmailSenderBot
from email_templates import TemplateContext, is_construction, render_template
from maps_finder import MapsFinderBot

logger = logging.getLogger(__name__)


def load_config(path: str) -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


def setup_logging(cfg: dict) -> None:
    level = getattr(logging, cfg.get("level", "INFO").upper(), logging.INFO)
    log_file = cfg.get("file", "logs/bot.log")
    os.makedirs(os.path.dirname(log_file), exist_ok=True)
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(log_file),
        ],
    )


async def main(args: argparse.Namespace) -> None:
    load_dotenv()
    config = load_config(args.config)
    setup_logging(config.get("logging", {}))

    city = config.get("location", {}).get("city", "")
    log = ContactedLog()

    if args.phase in ("all", "maps"):
        logger.info("=== Phase 1: Scanning Google Maps ===")
        finder = MapsFinderBot(config)
        raw_businesses = await finder.run()
        logger.info(f"Found {len(raw_businesses)} businesses without websites")

        new_businesses = [b for b in raw_businesses if not log.is_seen(b.name, b.address)]
        logger.info(f"{len(new_businesses)} are new (not previously seen)")

        logger.info("=== Phase 2: Finding email addresses ===")
        async with aiohttp.ClientSession() as session:
            email_finder = EmailFinderBot(config, session)
            for raw in new_businesses:
                email, source = await email_finder.find_email(
                    raw.name, raw.address, raw.category, raw.maps_url
                )
                biz = BusinessRecord(
                    name=raw.name,
                    address=raw.address,
                    phone=raw.phone,
                    category=raw.category,
                    email=email,
                    email_source=source,
                    contacted=False,
                    contacted_at="",
                    no_email=(email == ""),
                    maps_url=raw.maps_url,
                )
                if email:
                    log.add_pending(biz)
                    logger.info(f"Found email for {raw.name} via {source}: {email}")
                else:
                    log.mark_no_email(biz)
                    logger.info(f"No email found for {raw.name}")

    if args.phase in ("all", "email"):
        pending = log.get_pending()
        logger.info(f"=== Phase 3: Sending emails to {len(pending)} businesses ===")

        if args.dry_run:
            logger.info("DRY RUN - emails will be printed, not sent")

        sender = EmailSenderBot(config)
        for biz in pending:
            ctx = TemplateContext(
                business_name=biz.name,
                is_construction=is_construction(biz.category),
                service_type=biz.category.lower() or "service",
                city=city,
            )
            subject, body = render_template(ctx)
            if args.dry_run:
                print(f"\n{'='*60}")
                print(f"TO: {biz.email}")
                print(f"SUBJECT: {subject}")
                print(f"{'='*60}")
                print(body)
            else:
                success = await sender.send_one(biz.email, subject, body)
                if success:
                    log.mark_contacted(biz)
                await asyncio.sleep(
                    config.get("rate_limits", {}).get("email_send_delay_sec", 30)
                )

    stats = log.stats()
    logger.info(f"Done. Stats: {stats}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Business Website Finder + Email Outreach Bot"
    )
    parser.add_argument(
        "--phase",
        choices=["all", "maps", "email"],
        default="all",
        help="Which phase to run (default: all)",
    )
    parser.add_argument(
        "--config",
        default="config.yaml",
        help="Path to config file (default: config.yaml)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Find businesses and print emails without sending",
    )
    args = parser.parse_args()
    asyncio.run(main(args))

#!/usr/bin/env python3
"""Render a chronological index of worklog/ entries.

Read-only by design. Writing a combined log file back into the repo would recreate
the shared-file merge conflict that one-file-per-entry exists to remove.

Usage:
    python scripts/worklog_index.py
    python scripts/worklog_index.py --agent dispatch
    python scripts/worklog_index.py --since 2026-07-01
    python scripts/worklog_index.py --full
"""
from __future__ import annotations

import argparse
import os
import re
import sys

WORKLOG_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "worklog")


def parse_entry(path: str) -> dict:
    """Pull frontmatter + first heading + body out of an entry file."""
    with open(path, encoding="utf-8") as fh:
        text = fh.read()

    meta: dict = {}
    body = text
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            raw = text[3:end]
            body = text[end + 4:]
            for line in raw.splitlines():
                if ":" not in line:
                    continue
                k, _, v = line.partition(":")
                v = v.strip()
                if v.startswith("[") and v.endswith("]"):
                    v = [x.strip() for x in v[1:-1].split(",") if x.strip()]
                meta[k.strip()] = v

    title = ""
    for line in body.splitlines():
        if line.startswith("# "):
            title = line[2:].strip()
            break

    meta["_path"] = path
    meta["_file"] = os.path.basename(path)
    meta["_title"] = title
    meta["_body"] = body.strip()
    if "date" not in meta:
        m = re.match(r"(\d{4}-\d{2}-\d{2})", meta["_file"])
        meta["date"] = m.group(1) if m else "0000-00-00"
    return meta


def load(directory: str) -> list[dict]:
    if not os.path.isdir(directory):
        return []
    entries = [
        parse_entry(os.path.join(directory, f))
        for f in os.listdir(directory)
        if f.endswith(".md") and f.lower() != "readme.md"
    ]
    entries.sort(key=lambda e: (str(e.get("date", "")), e["_file"]), reverse=True)
    return entries


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--agent", help="filter by agent name")
    ap.add_argument("--since", help="only entries on/after this date (YYYY-MM-DD)")
    ap.add_argument("--lane", help="filter by lane")
    ap.add_argument("--full", action="store_true", help="print entry bodies too")
    args = ap.parse_args(argv)

    entries = load(WORKLOG_DIR)
    if args.agent:
        entries = [e for e in entries if e.get("agent") == args.agent]
    if args.lane:
        entries = [e for e in entries if e.get("lane") == args.lane]
    if args.since:
        entries = [e for e in entries if str(e.get("date", "")) >= args.since]

    if not entries:
        print("no worklog entries matched")
        print("(history before 2026-07-28 lives in WORKLOG.md under 'Log — archive')")
        return 0

    print(f"{len(entries)} entries\n")
    for e in entries:
        pr = f"  PR #{e['pr']}" if e.get("pr") else ""
        branch = f"  [{e['branch']}]" if e.get("branch") else ""
        print(f"{e.get('date')}  {e.get('agent', '?'):<18}{pr}{branch}")
        print(f"    {e['_title'] or e['_file']}")
        if e.get("lane"):
            print(f"    lane: {e['lane']}")
        if args.full:
            print()
            for line in e["_body"].splitlines():
                print(f"    {line}")
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())

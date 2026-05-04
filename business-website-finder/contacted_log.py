import json
import os
import re
import logging
from dataclasses import dataclass, asdict
from datetime import datetime
from typing import Optional

logger = logging.getLogger(__name__)

CONTACTED_FILE = "data/contacted.json"
NO_EMAIL_FILE = "data/no_email_found.json"


@dataclass
class BusinessRecord:
    name: str
    address: str
    phone: str
    category: str
    email: str
    email_source: str
    contacted: bool
    contacted_at: str
    no_email: bool
    maps_url: str


def _normalize(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.lower())


class ContactedLog:
    def __init__(self) -> None:
        self._records: dict[str, BusinessRecord] = {}
        os.makedirs("data", exist_ok=True)
        self._load()

    def _make_key(self, name: str, address: str) -> str:
        return _normalize(name) + "|" + _normalize(address)

    def _load(self) -> None:
        for path in (CONTACTED_FILE, NO_EMAIL_FILE):
            if not os.path.exists(path):
                continue
            try:
                with open(path) as f:
                    entries = json.load(f)
                for entry in entries:
                    rec = BusinessRecord(**entry)
                    key = self._make_key(rec.name, rec.address)
                    self._records[key] = rec
            except Exception as e:
                logger.warning(f"Could not load {path}: {e}")

    def _save(self) -> None:
        contacted = [asdict(r) for r in self._records.values() if r.contacted]
        no_email = [asdict(r) for r in self._records.values() if r.no_email and not r.contacted]
        for path, data in ((CONTACTED_FILE, contacted), (NO_EMAIL_FILE, no_email)):
            tmp = path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(data, f, indent=2)
            os.replace(tmp, path)

    def is_seen(self, name: str, address: str) -> bool:
        return self._make_key(name, address) in self._records

    def add_pending(self, biz: BusinessRecord) -> None:
        key = self._make_key(biz.name, biz.address)
        self._records[key] = biz
        self._save()

    def mark_no_email(self, biz: BusinessRecord) -> None:
        biz.no_email = True
        key = self._make_key(biz.name, biz.address)
        self._records[key] = biz
        self._save()

    def mark_contacted(self, biz: BusinessRecord) -> None:
        biz.contacted = True
        biz.contacted_at = datetime.now().isoformat()
        key = self._make_key(biz.name, biz.address)
        self._records[key] = biz
        self._save()

    def get_pending(self) -> list[BusinessRecord]:
        return [
            r for r in self._records.values()
            if not r.contacted and not r.no_email and r.email
        ]

    def stats(self) -> dict:
        all_recs = list(self._records.values())
        return {
            "total_seen": len(all_recs),
            "contacted": sum(1 for r in all_recs if r.contacted),
            "no_email_found": sum(1 for r in all_recs if r.no_email),
            "pending": len(self.get_pending()),
        }

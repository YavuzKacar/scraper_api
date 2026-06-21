"""
url_lists.py — JSON-file-backed allow/block list for scrape targets.

No database table: a single ``data/url_lists.json`` file holds both lists.
On first run (file absent) it is seeded from CONFIG.url_allowlist_seed /
CONFIG.url_blocklist_seed.  Entries match by exact host or subdomain suffix
("example.com" also matches "sub.example.com").

Allow-list semantics: an empty allow list means "allow everything except the
block list".  A non-empty allow list means "only these hosts (and their
subdomains) may be scraped".  The block list always wins.

Public API
----------
load_lists()                          Load (or seed) the lists at startup.
is_blocked(host) -> bool
is_allowed(host) -> bool
get_lists() -> dict
add_domain(list_name, domain) -> None
remove_domain(list_name, domain) -> None
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Literal

from config import CONFIG

logger = logging.getLogger(__name__)

_DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
_LIST_PATH = os.path.join(_DATA_DIR, "url_lists.json")

_allowlist: set[str] = set()
_blocklist: set[str] = set()
_lock = asyncio.Lock()


def _split_seed(value: str) -> set[str]:
    return {d.strip().lower() for d in value.split(",") if d.strip()}


def _matches(host: str, entries: set[str]) -> bool:
    host = host.lower()
    return any(host == entry or host.endswith("." + entry) for entry in entries)


def _save() -> None:
    os.makedirs(_DATA_DIR, exist_ok=True)
    with open(_LIST_PATH, "w", encoding="utf-8") as fh:
        json.dump(
            {"allowlist": sorted(_allowlist), "blocklist": sorted(_blocklist)},
            fh,
            indent=2,
        )


def load_lists() -> None:
    """Load the lists from disk, seeding from env vars on first run."""
    global _allowlist, _blocklist
    if os.path.isfile(_LIST_PATH):
        with open(_LIST_PATH, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        _allowlist = set(data.get("allowlist", []))
        _blocklist = set(data.get("blocklist", []))
        logger.info(
            "URL lists loaded from %s (allow=%d, block=%d).",
            _LIST_PATH, len(_allowlist), len(_blocklist),
        )
    else:
        _allowlist = _split_seed(CONFIG.url_allowlist_seed)
        _blocklist = _split_seed(CONFIG.url_blocklist_seed)
        _save()
        logger.info(
            "URL lists seeded from env at %s (allow=%d, block=%d).",
            _LIST_PATH, len(_allowlist), len(_blocklist),
        )


def is_blocked(host: str) -> bool:
    return _matches(host, _blocklist)


def is_allowed(host: str) -> bool:
    if not _allowlist:
        return True
    return _matches(host, _allowlist)


def get_lists() -> dict:
    return {"allowlist": sorted(_allowlist), "blocklist": sorted(_blocklist)}


async def add_domain(list_name: Literal["allow", "block"], domain: str) -> None:
    domain = domain.strip().lower()
    if not domain:
        raise ValueError("domain must not be empty")
    async with _lock:
        target = _allowlist if list_name == "allow" else _blocklist
        target.add(domain)
        _save()


async def remove_domain(list_name: Literal["allow", "block"], domain: str) -> None:
    domain = domain.strip().lower()
    async with _lock:
        target = _allowlist if list_name == "allow" else _blocklist
        target.discard(domain)
        _save()

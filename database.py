"""
database.py — Async SQLite persistence layer using aiosqlite.

Public API
----------
init_db()                         Create tables and indexes on startup.
get_url_record(url)               Fetch a URLRecord by URL; None if absent.
upsert_url_record(record)         Insert or update classification metadata.
update_scrape_result(url, ...)    Update last_scrape_status and last_success_html.
get_low_confidence_urls(thresh)   Return URLs with confidence below threshold.
get_stale_urls(max_age_hours)     Return URLs whose last_checked is too old.add_feedback(url, comment, ...)   Persist a user comment for a URL.
get_all_feedback()                Return all feedback rows, newest first.
get_feedback_for_url(url)         Return feedback rows for a specific URL.
delete_feedback(id)               Delete a single feedback row by ID."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

import aiosqlite

from config import CONFIG
from models import URLRecord

logger = logging.getLogger(__name__)

# ── Schema ────────────────────────────────────────────────────────────────────

_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS url_metadata (
    url                          TEXT PRIMARY KEY,
    content_type                 TEXT,
    antiscraping_protection      TEXT,
    tor_network_available        TEXT,
    undetected_browser_available TEXT,
    is_public_page               TEXT,
    scraping_strategy            TEXT,
    classification_confidence    REAL,
    last_checked                 TEXT,
    last_scrape_status           TEXT,
    last_success_html            TEXT
);
"""

_CREATE_IDX_SQL = (
    "CREATE INDEX IF NOT EXISTS idx_last_checked "
    "ON url_metadata(last_checked);"
)

_CREATE_IDX_CONF_SQL = (
    "CREATE INDEX IF NOT EXISTS idx_confidence "
    "ON url_metadata(classification_confidence);"
)

_CREATE_FEEDBACK_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS url_feedback (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    url            TEXT NOT NULL,
    comment        TEXT NOT NULL,
    strategy_used  TEXT,
    scrape_success INTEGER,
    created_at     TEXT NOT NULL
);
"""


# ── Helpers ───────────────────────────────────────────────────────────────────

def _parse_dt(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _row_to_record(row: aiosqlite.Row) -> URLRecord:
    return URLRecord(
        url=row["url"],
        content_type=row["content_type"],
        antiscraping_protection=row["antiscraping_protection"],
        tor_network_available=row["tor_network_available"],
        undetected_browser_available=row["undetected_browser_available"],
        is_public_page=row["is_public_page"],
        scraping_strategy=row["scraping_strategy"],
        classification_confidence=row["classification_confidence"],
        last_checked=_parse_dt(row["last_checked"]),
        last_scrape_status=row["last_scrape_status"],
        last_success_html=row["last_success_html"],
    )


# ── Public functions ──────────────────────────────────────────────────────────

async def init_db() -> None:
    """Create the schema if it does not already exist."""
    async with aiosqlite.connect(CONFIG.db_path) as db:
        # WAL mode allows concurrent readers alongside a single writer and
        # dramatically reduces "database is locked" errors under concurrent load.
        await db.execute("PRAGMA journal_mode=WAL")
        # Give writers up to 10 s to acquire the lock before raising
        # OperationalError, instead of failing instantly.
        await db.execute("PRAGMA busy_timeout=10000")
        await db.execute(_CREATE_TABLE_SQL)
        await db.execute(_CREATE_IDX_SQL)
        await db.execute(_CREATE_IDX_CONF_SQL)
        await db.execute(_CREATE_FEEDBACK_TABLE_SQL)
        await db.commit()
    logger.info("Database ready at '%s'", CONFIG.db_path)


async def get_url_record(url: str) -> Optional[URLRecord]:
    """Return the stored record for *url*, or None if not found."""
    async with aiosqlite.connect(CONFIG.db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM url_metadata WHERE url = ?", (url,)
        ) as cursor:
            row = await cursor.fetchone()
            return _row_to_record(row) if row else None


async def upsert_url_record(record: URLRecord) -> None:
    """
    Insert or update a URLRecord.

    Classification fields are always overwritten on conflict.
    last_scrape_status and last_success_html are preserved from the
    existing row when the new record does not supply them.
    """
    now_iso = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(CONFIG.db_path) as db:
        await db.execute(
            """
            INSERT INTO url_metadata (
                url, content_type, antiscraping_protection,
                tor_network_available, undetected_browser_available,
                is_public_page, scraping_strategy,
                classification_confidence, last_checked,
                last_scrape_status, last_success_html
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(url) DO UPDATE SET
                content_type                 = excluded.content_type,
                antiscraping_protection      = excluded.antiscraping_protection,
                tor_network_available        = excluded.tor_network_available,
                undetected_browser_available = excluded.undetected_browser_available,
                is_public_page               = excluded.is_public_page,
                scraping_strategy            = excluded.scraping_strategy,
                classification_confidence    = excluded.classification_confidence,
                last_checked                 = excluded.last_checked
            """,
            (
                record.url,
                record.content_type,
                record.antiscraping_protection,
                record.tor_network_available,
                record.undetected_browser_available,
                record.is_public_page,
                record.scraping_strategy,
                record.classification_confidence,
                record.last_checked.isoformat() if record.last_checked else now_iso,
                record.last_scrape_status,
                record.last_success_html,
            ),
        )
        await db.commit()


async def update_scrape_result(
    url: str,
    status: str,
    html: Optional[str],
) -> None:
    """
    Persist the outcome of a scrape attempt.

    *html* is only written when the scrape succeeded (to avoid
    overwriting good cached HTML with None on a transient failure).
    """
    async with aiosqlite.connect(CONFIG.db_path) as db:
        if html is not None:
            await db.execute(
                """UPDATE url_metadata
                   SET last_scrape_status = ?, last_success_html = ?
                   WHERE url = ?""",
                (status, html, url),
            )
        else:
            await db.execute(
                "UPDATE url_metadata SET last_scrape_status = ? WHERE url = ?",
                (status, url),
            )
        await db.commit()


async def get_low_confidence_urls(threshold: float) -> list[str]:
    """Return URLs whose classification_confidence is below *threshold*."""
    async with aiosqlite.connect(CONFIG.db_path) as db:
        async with db.execute(
            """SELECT url FROM url_metadata
               WHERE classification_confidence IS NOT NULL
                 AND classification_confidence < ?
                 AND url != '__health_check__'""",
            (threshold,),
        ) as cursor:
            rows = await cursor.fetchall()
            return [r[0] for r in rows]


async def get_stale_urls(max_age_hours: int) -> list[str]:
    """Return URLs not checked within the last *max_age_hours* hours."""
    cutoff = (
        datetime.now(timezone.utc) - timedelta(hours=max_age_hours)
    ).isoformat()
    async with aiosqlite.connect(CONFIG.db_path) as db:
        async with db.execute(
            """SELECT url FROM url_metadata
               WHERE (last_checked IS NULL OR last_checked < ?)
                 AND url != '__health_check__'""",
            (cutoff,),
        ) as cursor:
            rows = await cursor.fetchall()
            return [r[0] for r in rows]


# ── Feedback CRUD ─────────────────────────────────────────────────────────────

async def add_feedback(
    url: str,
    comment: str,
    strategy_used: Optional[str] = None,
    scrape_success: Optional[bool] = None,
) -> None:
    """Persist a user comment (and optional scrape context) for a URL."""
    now_iso = datetime.now(timezone.utc).isoformat()
    success_int = int(scrape_success) if scrape_success is not None else None
    async with aiosqlite.connect(CONFIG.db_path) as db:
        await db.execute("PRAGMA busy_timeout=10000")
        await db.execute(
            """INSERT INTO url_feedback
               (url, comment, strategy_used, scrape_success, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (url, comment, strategy_used, success_int, now_iso),
        )
        await db.commit()


def _row_to_feedback(row: aiosqlite.Row) -> dict:
    success = row["scrape_success"]
    return {
        "id": row["id"],
        "url": row["url"],
        "comment": row["comment"],
        "strategy_used": row["strategy_used"],
        "scrape_success": bool(success) if success is not None else None,
        "created_at": row["created_at"],
    }


async def get_all_feedback() -> list[dict]:
    """Return all feedback rows ordered newest-first."""
    async with aiosqlite.connect(CONFIG.db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT id, url, comment, strategy_used, scrape_success, created_at
               FROM url_feedback ORDER BY created_at DESC"""
        ) as cursor:
            rows = await cursor.fetchall()
            return [_row_to_feedback(r) for r in rows]


async def get_feedback_for_url(url: str) -> list[dict]:
    """Return feedback rows for a specific URL, newest-first."""
    async with aiosqlite.connect(CONFIG.db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT id, url, comment, strategy_used, scrape_success, created_at
               FROM url_feedback WHERE url = ? ORDER BY created_at DESC""",
            (url,),
        ) as cursor:
            rows = await cursor.fetchall()
            return [_row_to_feedback(r) for r in rows]


async def delete_feedback(feedback_id: int) -> None:
    """Delete a single feedback row by primary key."""
    async with aiosqlite.connect(CONFIG.db_path) as db:
        await db.execute("PRAGMA busy_timeout=10000")
        await db.execute("DELETE FROM url_feedback WHERE id = ?", (feedback_id,))
        await db.commit()


async def delete_all_feedback() -> int:
    """
    Delete every row in url_feedback.

    Returns the number of rows deleted.
    """
    async with aiosqlite.connect(CONFIG.db_path) as db:
        await db.execute("PRAGMA busy_timeout=10000")
        cursor = await db.execute("DELETE FROM url_feedback")
        await db.commit()
        return cursor.rowcount

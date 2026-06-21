"""
database.py — Async SQLite persistence layer using aiosqlite.

Public API
----------
init_db()                         Open the persistent connection, create tables.
close_db()                        Close the persistent connection gracefully.
get_url_record(url)               Fetch a URLRecord by URL; None if absent.
upsert_url_record(record)         Insert or update metadata.
update_scrape_result(url, status) Update last_scrape_status for a URL.
log_scrape_attempt(...)           Append one audit row to scrape_log.
add_feedback(url, comment, ...)   Persist a user comment for a URL.
get_all_feedback()                Return all feedback rows, newest first.
get_feedback_for_url(url)         Return feedback rows for a specific URL.
delete_feedback(id)               Delete a single feedback row by ID.
get_credits()                     Return {balance, granted, used}.
deduct_credit(amount)             Deduct the given cost; return new balance.

Connection strategy
-------------------
A single ``aiosqlite.Connection`` is opened at startup and reused for the
lifetime of the process.  WAL journal mode lets multiple coroutines read
simultaneously.  A single ``asyncio.Lock`` (_write_lock) serialises all
multi-step write operations to prevent TOCTOU races.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional

import aiosqlite

from config import CONFIG
from models import URLRecord

logger = logging.getLogger(__name__)

# ── Persistent connection ─────────────────────────────────────────────────────

_db: Optional[aiosqlite.Connection] = None
_write_lock: asyncio.Lock  # assigned in init_db()


def _conn() -> aiosqlite.Connection:
    """Return the open connection; raises RuntimeError if init_db() was not called."""
    if _db is None:
        raise RuntimeError("Database not initialised — call init_db() first.")
    return _db

# ── Schema ────────────────────────────────────────────────────────────────────

_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS url_metadata (
    url                TEXT PRIMARY KEY,
    scraping_strategy  TEXT,
    last_checked       TEXT,
    last_scrape_status TEXT
);
"""

_CREATE_DOMAIN_STRATEGY_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS domain_strategies (
    root_url     TEXT PRIMARY KEY,
    strategy     TEXT NOT NULL,
    last_updated TEXT NOT NULL
);
"""

_CREATE_IDX_SQL = (
    "CREATE INDEX IF NOT EXISTS idx_last_checked "
    "ON url_metadata(last_checked);"
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

_CREATE_CREDITS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS credits (
    id      INTEGER PRIMARY KEY CHECK (id = 1),
    balance REAL NOT NULL DEFAULT 0,
    granted REAL NOT NULL DEFAULT 0
);
"""

_CREATE_SCRAPE_LOG_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS scrape_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    url             TEXT NOT NULL,
    success         INTEGER NOT NULL,
    provider        TEXT,
    status          TEXT NOT NULL,
    cost            REAL NOT NULL DEFAULT 0,
    error_reason    TEXT,
    duration_ms     INTEGER,
    response_bytes  INTEGER,
    created_at      TEXT NOT NULL
);
"""

_CREATE_SCRAPE_LOG_IDX_URL_SQL = (
    "CREATE INDEX IF NOT EXISTS idx_scrape_log_url ON scrape_log(url);"
)
_CREATE_SCRAPE_LOG_IDX_CREATED_SQL = (
    "CREATE INDEX IF NOT EXISTS idx_scrape_log_created ON scrape_log(created_at);"
)

# Additive columns on url_metadata -- applied via a guarded ALTER TABLE in
# init_db() since the DB file ships with existing data and SQLite has no
# "ADD COLUMN IF NOT EXISTS" syntax.
_URL_METADATA_NEW_COLUMNS: dict[str, str] = {
    "last_provider": "TEXT",
    "last_cost": "REAL",
    "last_error_reason": "TEXT",
}


# ── Helpers ───────────────────────────────────────────────────────────────────

def _parse_dt(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _row_to_record(row: aiosqlite.Row) -> URLRecord:
    keys = row.keys()
    return URLRecord(
        url=row["url"],
        scraping_strategy=row["scraping_strategy"],
        last_checked=_parse_dt(row["last_checked"]),
        last_scrape_status=row["last_scrape_status"],
        last_provider=row["last_provider"] if "last_provider" in keys else None,
        last_cost=row["last_cost"] if "last_cost" in keys else None,
        last_error_reason=row["last_error_reason"] if "last_error_reason" in keys else None,
    )


# ── Public functions ──────────────────────────────────────────────────────────

async def init_db() -> None:
    """Open the persistent connection and create the schema if missing."""
    global _db, _write_lock
    _write_lock = asyncio.Lock()
    _db = await aiosqlite.connect(CONFIG.db_path)
    _db.row_factory = aiosqlite.Row
    # WAL mode: concurrent readers, single writer, no full-table locks.
    await _db.execute("PRAGMA journal_mode=WAL")
    # Give writers up to 10 s to acquire a lock before raising OperationalError.
    await _db.execute("PRAGMA busy_timeout=10000")
    # NORMAL is safe with WAL and roughly 2× faster than FULL.
    await _db.execute("PRAGMA synchronous=NORMAL")
    # 10 MB read cache kept in memory.
    await _db.execute("PRAGMA cache_size=10000")
    # Store temp tables / sort buffers in RAM instead of on disk.
    await _db.execute("PRAGMA temp_store=MEMORY")
    await _db.execute(_CREATE_TABLE_SQL)
    await _db.execute(_CREATE_IDX_SQL)
    await _db.execute(_CREATE_DOMAIN_STRATEGY_TABLE_SQL)
    await _db.execute(_CREATE_FEEDBACK_TABLE_SQL)
    await _db.execute(_CREATE_CREDITS_TABLE_SQL)
    await _db.execute(_CREATE_SCRAPE_LOG_TABLE_SQL)
    await _db.execute(_CREATE_SCRAPE_LOG_IDX_URL_SQL)
    await _db.execute(_CREATE_SCRAPE_LOG_IDX_CREATED_SQL)
    await _migrate_url_metadata_columns()
    await _db.execute(
        "INSERT OR IGNORE INTO credits (id, balance, granted) VALUES (1, ?, ?)",
        (CONFIG.initial_credits, CONFIG.initial_credits),
    )
    await _db.commit()
    logger.info("Database ready at '%s'", CONFIG.db_path)


async def _migrate_url_metadata_columns() -> None:
    """Additively add any missing url_metadata columns (safe on existing data)."""
    db = _conn()
    async with db.execute("PRAGMA table_info(url_metadata)") as cursor:
        existing = {row["name"] async for row in cursor}
    for column, col_type in _URL_METADATA_NEW_COLUMNS.items():
        if column not in existing:
            await db.execute(f"ALTER TABLE url_metadata ADD COLUMN {column} {col_type}")
            logger.info("Migrated url_metadata: added column '%s'.", column)


async def close_db() -> None:
    """Close the persistent connection gracefully on application shutdown."""
    global _db
    if _db is not None:
        await _db.close()
        _db = None
        logger.info("Database connection closed.")


async def get_url_record(url: str) -> Optional[URLRecord]:
    """Return the stored record for *url*, or None if not found."""
    db = _conn()
    async with db.execute(
        "SELECT * FROM url_metadata WHERE url = ?", (url,)
    ) as cursor:
        row = await cursor.fetchone()
        return _row_to_record(row) if row else None


async def upsert_url_record(record: URLRecord) -> None:
    """Insert or update a URLRecord (url, scraping_strategy, last_checked, ...)."""
    now_iso = datetime.now(timezone.utc).isoformat()
    db = _conn()
    async with _write_lock:
        await db.execute(
            """
            INSERT INTO url_metadata
                (url, scraping_strategy, last_checked, last_provider, last_cost, last_error_reason)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(url) DO UPDATE SET
                scraping_strategy = excluded.scraping_strategy,
                last_checked      = excluded.last_checked,
                last_provider     = excluded.last_provider,
                last_cost         = excluded.last_cost,
                last_error_reason = excluded.last_error_reason
            """,
            (
                record.url,
                record.scraping_strategy,
                record.last_checked.isoformat() if record.last_checked else now_iso,
                record.last_provider,
                record.last_cost,
                record.last_error_reason,
            ),
        )
        await db.commit()


async def get_domain_strategy(root_url: str) -> Optional[str]:
    """Return the stored strategy for *root_url*, or None if not found."""
    db = _conn()
    async with db.execute(
        "SELECT strategy FROM domain_strategies WHERE root_url = ?", (root_url,)
    ) as cursor:
        row = await cursor.fetchone()
        return row["strategy"] if row else None


async def upsert_domain_strategy(root_url: str, strategy: str) -> None:
    """Store the working strategy for *root_url*."""
    now_iso = datetime.now(timezone.utc).isoformat()
    db = _conn()
    async with _write_lock:
        await db.execute(
            """
            INSERT INTO domain_strategies (root_url, strategy, last_updated)
            VALUES (?, ?, ?)
            ON CONFLICT(root_url) DO UPDATE SET
                strategy     = excluded.strategy,
                last_updated = excluded.last_updated
            """,
            (root_url, strategy, now_iso),
        )
        await db.commit()


async def update_scrape_result(
    url: str,
    status: str,
    error_reason: Optional[str] = None,
) -> None:
    """
    Persist the outcome of a scrape attempt (last_scrape_status, last_checked,
    last_error_reason).  Creates the row if it doesn't exist yet -- every
    requested URL is recorded, success or failure -- without touching
    scraping_strategy / last_provider / last_cost, which only change on the
    success path (upsert_url_record).
    """
    now_iso = datetime.now(timezone.utc).isoformat()
    db = _conn()
    async with _write_lock:
        await db.execute(
            """
            INSERT INTO url_metadata (url, last_scrape_status, last_checked, last_error_reason)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(url) DO UPDATE SET
                last_scrape_status = excluded.last_scrape_status,
                last_checked       = excluded.last_checked,
                last_error_reason  = excluded.last_error_reason
            """,
            (url, status, now_iso, error_reason),
        )
        await db.commit()


async def log_scrape_attempt(
    url: str,
    success: bool,
    provider: Optional[str],
    status: str,
    cost: float,
    error_reason: Optional[str],
    duration_ms: Optional[int],
    response_bytes: Optional[int],
) -> None:
    """Append one row to scrape_log -- the full audit trail of every /scrape call."""
    now_iso = datetime.now(timezone.utc).isoformat()
    db = _conn()
    async with _write_lock:
        await db.execute(
            """INSERT INTO scrape_log
               (url, success, provider, status, cost, error_reason, duration_ms, response_bytes, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (url, int(success), provider, status, cost, error_reason, duration_ms, response_bytes, now_iso),
        )
        await db.commit()


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
    db = _conn()
    async with _write_lock:
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
    db = _conn()
    async with db.execute(
        """SELECT id, url, comment, strategy_used, scrape_success, created_at
           FROM url_feedback ORDER BY created_at DESC"""
    ) as cursor:
        rows = await cursor.fetchall()
        return [_row_to_feedback(r) for r in rows]


async def get_feedback_for_url(url: str) -> list[dict]:
    """Return feedback rows for a specific URL, newest-first."""
    db = _conn()
    async with db.execute(
        """SELECT id, url, comment, strategy_used, scrape_success, created_at
           FROM url_feedback WHERE url = ? ORDER BY created_at DESC""",
        (url,),
    ) as cursor:
        rows = await cursor.fetchall()
        return [_row_to_feedback(r) for r in rows]


async def delete_feedback(feedback_id: int) -> None:
    """Delete a single feedback row by primary key."""
    db = _conn()
    async with _write_lock:
        await db.execute("DELETE FROM url_feedback WHERE id = ?", (feedback_id,))
        await db.commit()


async def delete_all_feedback() -> int:
    """
    Delete every row in url_feedback.

    Returns the number of rows deleted.
    """
    db = _conn()
    async with _write_lock:
        cursor = await db.execute("DELETE FROM url_feedback")
        await db.commit()
        return cursor.rowcount


# ── Credits ───────────────────────────────────────────────────────────────────

async def get_credits() -> dict:
    """Return credit stats: balance, granted, and used."""
    db = _conn()
    async with db.execute(
        "SELECT balance, granted FROM credits WHERE id = 1"
    ) as cursor:
        row = await cursor.fetchone()
        if row is None:
            return {"balance": 0.0, "granted": 0.0, "used": 0.0}
        return {
            "balance": row["balance"],
            "granted": row["granted"],
            "used":    row["granted"] - row["balance"],
        }


async def deduct_credit(amount: float = 1.0) -> float:
    """
    Atomically deduct *amount* credits (the real cost of the provider that
    served the request).

    Returns the new balance.  Raises ``ValueError`` when the balance is
    already 0 so the caller can return HTTP 402 before scraping.
    """
    db = _conn()
    async with _write_lock:
        async with db.execute(
            "SELECT balance FROM credits WHERE id = 1"
        ) as cursor:
            row = await cursor.fetchone()
        if row is None or row["balance"] <= 0:
            raise ValueError("No credits remaining.")
        await db.execute(
            "UPDATE credits SET balance = balance - ? WHERE id = 1 AND balance > 0",
            (amount,),
        )
        await db.commit()
        async with db.execute(
            "SELECT balance FROM credits WHERE id = 1"
        ) as cursor:
            row = await cursor.fetchone()
        return row["balance"] if row else 0.0

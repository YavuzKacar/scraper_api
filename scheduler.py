"""
scheduler.py — Lightweight background task scheduler.

Responsibilities
----------------
- Periodically reclassify URLs whose classification_confidence is below
  CONFIG.low_confidence_threshold.
- Periodically refresh metadata for URLs not checked within
  CONFIG.metadata_max_age_hours hours.

The scheduler runs as a single asyncio background task started during
FastAPI application lifespan.  It never raises — errors are logged and
the scheduler continues on the next cycle.

Public API
----------
start_scheduler() → asyncio.Task   (call once at startup)
stop_scheduler()                    (call at shutdown)
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional

from config import CONFIG
from database import (
    get_low_confidence_urls,
    get_stale_urls,
    upsert_url_record,
)
from models import URLRecord

logger = logging.getLogger(__name__)

_scheduler_task: Optional[asyncio.Task] = None


# ── Internal worker ───────────────────────────────────────────────────────────

async def _reclassify_url(url: str) -> None:
    """Run the classifier on *url* and persist the updated record."""
    try:
        from classifier import classify_url
        from database import get_url_record

        logger.info("Scheduler: reclassifying %s", url)
        classification = await classify_url(url)
        existing = await get_url_record(url)

        updated = URLRecord(
            url=url,
            content_type=classification.content_type.value,
            antiscraping_protection=classification.antiscraping_protection.value,
            tor_network_available=classification.tor_network_available.value,
            undetected_browser_available=classification.undetected_browser_available.value,
            is_public_page=classification.is_public_page.value,
            scraping_strategy=classification.scraping_strategy.value,
            classification_confidence=classification.classification_confidence,
            last_checked=datetime.now(timezone.utc),
            last_scrape_status=existing.last_scrape_status if existing else None,
            last_success_html=existing.last_success_html if existing else None,
        )
        await upsert_url_record(updated)
        logger.info(
            "Scheduler: updated %s — strategy=%s confidence=%.2f",
            url,
            classification.scraping_strategy.value,
            classification.classification_confidence,
        )
    except Exception as exc:
        logger.warning("Scheduler: reclassification failed for %s: %s", url, exc)


async def _run_one_cycle() -> None:
    """Run a single scheduler cycle."""
    # 1. Low-confidence URLs
    low_conf_urls = await get_low_confidence_urls(CONFIG.low_confidence_threshold)
    if low_conf_urls:
        logger.info("Scheduler: %d low-confidence URLs to reclassify.", len(low_conf_urls))
        for url in low_conf_urls:
            await _reclassify_url(url)
            # Brief pause between classifications to avoid hammering sites
            await asyncio.sleep(2.0)

    # 2. Stale metadata URLs
    stale_urls = await get_stale_urls(CONFIG.metadata_max_age_hours)
    if stale_urls:
        logger.info("Scheduler: %d stale URLs to refresh.", len(stale_urls))
        for url in stale_urls:
            await _reclassify_url(url)
            await asyncio.sleep(2.0)


async def _scheduler_loop() -> None:
    """Main scheduler loop — runs indefinitely until cancelled."""
    logger.info(
        "Scheduler started (interval=%ds).", CONFIG.scheduler_interval_seconds
    )
    while True:
        try:
            await _run_one_cycle()
        except asyncio.CancelledError:
            logger.info("Scheduler: cancellation received — stopping.")
            return
        except Exception as exc:
            logger.error("Scheduler: unexpected error in cycle: %s", exc)

        try:
            await asyncio.sleep(CONFIG.scheduler_interval_seconds)
        except asyncio.CancelledError:
            logger.info("Scheduler: cancelled during sleep — stopping.")
            return


# ── Public control functions ──────────────────────────────────────────────────

def start_scheduler() -> asyncio.Task:
    """
    Start the background scheduler.

    Call exactly once during application startup.
    Returns the running asyncio.Task.
    """
    global _scheduler_task
    _scheduler_task = asyncio.create_task(_scheduler_loop(), name="scraper-scheduler")
    return _scheduler_task


def stop_scheduler() -> None:
    """Cancel the background scheduler task (idempotent)."""
    global _scheduler_task
    if _scheduler_task and not _scheduler_task.done():
        _scheduler_task.cancel()
        logger.info("Scheduler: stop requested.")
    _scheduler_task = None

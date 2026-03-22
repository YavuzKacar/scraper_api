"""
scheduler.py -- Lightweight background task scheduler.

No active reclassification tasks. The loop is kept alive so the
application lifespan contract (start_scheduler / stop_scheduler) remains
intact for future extension.

Public API
----------
start_scheduler() -> asyncio.Task   (call once at startup)
stop_scheduler()                    (call at shutdown)
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

from config import CONFIG

logger = logging.getLogger(__name__)

_scheduler_task: Optional[asyncio.Task] = None


async def _scheduler_loop() -> None:
    """Main scheduler loop -- sleeps until cancelled."""
    logger.info("Scheduler started (interval=%ds).", CONFIG.scheduler_interval_seconds)
    while True:
        try:
            await asyncio.sleep(CONFIG.scheduler_interval_seconds)
        except asyncio.CancelledError:
            logger.info("Scheduler: cancelled -- stopping.")
            return


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

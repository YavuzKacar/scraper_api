"""
app.py — FastAPI application entry point for the Scraper API.

Endpoints
---------
POST /scrape        Scrape a URL using the adaptive strategy engine.
POST /classify      Classify a URL without scraping.
GET  /status/{url}  Retrieve stored metadata for a URL.
GET  /health        Liveness + readiness check.

Security
--------
All endpoints require the X-API-KEY header.
The server binds to 127.0.0.1 only (no external exposure).
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import socket
from urllib.parse import unquote

import uvicorn
from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from classifier import classify_url
from config import CONFIG
from database import get_url_record, init_db, upsert_url_record
from models import (
    ClassifyRequest,
    ClassifyResponse,
    HealthResponse,
    ScrapingStrategy,
    ScrapeRequest,
    ScrapeResponse,
    StatusResponse,
    URLRecord,
)
from scraper import scrape
from scheduler import start_scheduler, stop_scheduler
from security import require_api_key

# ── Logging setup ─────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
)
logger = logging.getLogger(__name__)

# ── Application lifecycle ─────────────────────────────────────────────────────

@contextlib.asynccontextmanager
async def _lifespan(application: FastAPI):
    """Startup and shutdown logic."""
    logger.info("Scraper API starting on %s:%d", CONFIG.host, CONFIG.port)
    await init_db()
    start_scheduler()
    yield
    stop_scheduler()
    logger.info("Scraper API shut down cleanly.")


# ── Application instance ──────────────────────────────────────────────────────

app = FastAPI(
    title="Scraper API",
    description=(
        "Production-ready adaptive web scraping API with automatic "
        "site classification, Tor support, undetected browser rendering, "
        "and persistent URL metadata storage."
    ),
    version="1.0.0",
    lifespan=_lifespan,
    # Disable automatic redirect that would reveal the existence of routes
    redirect_slashes=False,
)

# Restrict CORS to localhost only — belt-and-suspenders alongside the
# require_api_key dependency.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://127.0.0.1:*", "http://localhost:*"],
    allow_credentials=True,
    allow_methods=["POST", "GET"],
    allow_headers=["X-API-KEY", "Content-Type"],
)


# ── Error handlers ─────────────────────────────────────────────────────────────

@app.exception_handler(Exception)
async def _generic_exception_handler(request, exc):
    logger.error("Unhandled exception: %s", exc, exc_info=True)
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={"detail": "An internal error occurred."},
    )


# ── Endpoints ─────────────────────────────────────────────────────────────────

@app.post(
    "/scrape",
    response_model=ScrapeResponse,
    summary="Scrape a URL",
    dependencies=[Depends(require_api_key)],
)
async def scrape_endpoint(request: ScrapeRequest) -> ScrapeResponse:
    """
    Scrape the given URL using the adaptive strategy engine.

    Workflow:
    1. Load or generate URL classification.
    2. Enforce public-page and blocked-strategy policies.
    3. Return cached HTML if still fresh (< 10 minutes).
    4. Apply domain rate limiting.
    5. Scrape using the selected strategy (static / browser / tor / hybrid).
    6. Retry up to 3 times with rotated fingerprints on failure.

    Returns the scraped HTML or an informative failure message.
    """
    return await scrape(request)


@app.post(
    "/classify",
    response_model=ClassifyResponse,
    summary="Classify a URL",
    dependencies=[Depends(require_api_key)],
)
async def classify_endpoint(request: ClassifyRequest) -> ClassifyResponse:
    """
    Classify a URL and persist the result.

    Always reads from the database first unless ``force=true`` is set.
    Classification includes content type, anti-scraping protection,
    Tor / browser availability, public-page status, recommended strategy,
    and a confidence score.
    """
    from_cache = False

    if not request.force:
        record = await get_url_record(request.url)
        if record and record.is_classified():
            classification = record.to_classification()
            if classification:
                return ClassifyResponse(
                    url=request.url,
                    classification=classification,
                    from_cache=True,
                )

    classification = await classify_url(request.url)

    # Persist the fresh classification
    from datetime import datetime, timezone

    record = await get_url_record(request.url)
    updated = URLRecord(
        url=request.url,
        content_type=classification.content_type.value,
        antiscraping_protection=classification.antiscraping_protection.value,
        tor_network_available=classification.tor_network_available.value,
        undetected_browser_available=classification.undetected_browser_available.value,
        is_public_page=classification.is_public_page.value,
        scraping_strategy=classification.scraping_strategy.value,
        classification_confidence=classification.classification_confidence,
        last_checked=datetime.now(timezone.utc),
        last_scrape_status=record.last_scrape_status if record else None,
        last_success_html=record.last_success_html if record else None,
    )
    await upsert_url_record(updated)

    return ClassifyResponse(
        url=request.url,
        classification=classification,
        from_cache=from_cache,
    )


@app.get(
    "/status/{url:path}",
    response_model=StatusResponse,
    summary="Get stored metadata for a URL",
    dependencies=[Depends(require_api_key)],
)
async def status_endpoint(url: str) -> StatusResponse:
    """
    Return the persisted metadata record for the given URL.

    The URL must be URL-encoded if it contains special characters.
    Returns ``found: false`` (HTTP 200) when the URL has no stored record.

    Note: ``last_success_html`` is excluded from the response to keep
    payloads manageable—use ``/scrape`` to retrieve HTML.
    """
    decoded_url = unquote(url)
    record = await get_url_record(decoded_url)

    if not record:
        return StatusResponse(url=decoded_url, found=False)

    # Exclude cached HTML from status responses
    sanitised = record.model_copy(update={"last_success_html": None})
    return StatusResponse(url=decoded_url, record=sanitised, found=True)


@app.get(
    "/health",
    response_model=HealthResponse,
    summary="Health check",
    dependencies=[Depends(require_api_key)],
)
async def health_endpoint() -> HealthResponse:
    """
    Return the liveness and readiness state of the API.

    Checks database accessibility and Tor SOCKS port reachability.
    """
    import contextlib

    # Database check
    db_status = "ok"
    try:
        await get_url_record("__health_check__")
    except Exception as exc:
        db_status = f"error: {exc}"

    # Tor check
    tor_reachable = False
    with contextlib.suppress(OSError):
        with socket.create_connection(
            (CONFIG.tor_socks_host, CONFIG.tor_socks_port), timeout=2.0
        ):
            tor_reachable = True

    return HealthResponse(
        status="ok" if db_status == "ok" else "degraded",
        database=db_status,
        tor_reachable=tor_reachable,
    )


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    uvicorn.run(
        "app:app",
        host=CONFIG.host,         # 127.0.0.1 — localhost only
        port=CONFIG.port,
        log_level="info",
        access_log=True,
        reload=False,
    )

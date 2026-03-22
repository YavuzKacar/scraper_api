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
import time
import uuid
from urllib.parse import unquote

import uvicorn
from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from classifier import classify_url
from config import CONFIG
from database import (
    add_feedback,
    delete_feedback,
    delete_all_feedback,
    get_all_feedback,
    get_feedback_for_url,
    get_url_record,
    init_db,
    upsert_url_record,
)
from logging_setup import setup_logging
from models import (
    ClassifyRequest,
    ClassifyResponse,
    FeedbackCreate,
    FeedbackItem,
    FeedbackListResponse,
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
setup_logging(log_level=CONFIG.log_level, log_dir=CONFIG.log_dir)
logger = logging.getLogger(__name__)

# ── Application lifecycle ─────────────────────────────────────────────────────

@contextlib.asynccontextmanager
async def _lifespan(application: FastAPI):
    """Startup and shutdown logic."""
    logger.info("Scraper API starting on %s:%d", CONFIG.host, CONFIG.port)
    await init_db()
    task = start_scheduler()
    yield
    stop_scheduler()
    # Await the cancelled task so in-progress coroutines can clean up
    # before the process exits.
    with contextlib.suppress(asyncio.CancelledError):
        await task
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


# ── Request logging middleware ────────────────────────────────────────────────────

_req_logger = logging.getLogger("scraper_api.requests")


@app.middleware("http")
async def _request_logging_middleware(request, call_next):
    """
    Log every request with: method, path, status code, duration, and a
    unique request ID that appears in all log lines for that request.
    """
    request_id = str(uuid.uuid4())[:8]
    start = time.perf_counter()

    _req_logger.info(
        "[%s] → %s %s (client=%s)",
        request_id,
        request.method,
        request.url.path,
        request.client.host if request.client else "unknown",
    )

    try:
        response = await call_next(request)
    except Exception as exc:
        duration_ms = (time.perf_counter() - start) * 1000
        _req_logger.error(
            "[%s] ✗ %s %s — unhandled exception after %.0fms: %s",
            request_id,
            request.method,
            request.url.path,
            duration_ms,
            exc,
            exc_info=True,
        )
        raise

    duration_ms = (time.perf_counter() - start) * 1000
    level = logging.WARNING if response.status_code >= 400 else logging.INFO
    _req_logger.log(
        level,
        "[%s] ← %s %s %d (%.0fms)",
        request_id,
        request.method,
        request.url.path,
        response.status_code,
        duration_ms,
    )

    response.headers["X-Request-ID"] = request_id
    return response


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

    Tor diagnostics
    ---------------
    tor_reachable        — any SOCKS port (9150 / 9050) is open
    tor_socks_port       — which SOCKS port responded first (None if unreachable)
    tor_control_reachable — control port (9151 / 9051) is open
    tor_circuit_ok       — a real HTTP GET through the Tor proxy succeeded
    """
    import contextlib

    # Database check
    db_status = "ok"
    try:
        await get_url_record("__health_check__")
    except Exception as exc:
        db_status = f"error: {exc}"

    # Run all Tor checks in a single executor call so the event loop never blocks.
    loop = asyncio.get_running_loop()

    def _check_tor() -> dict:
        result = {
            "reachable": False,
            "socks_port": None,
            "control_reachable": False,
            "circuit_ok": None,
        }

        # 1 — SOCKS port probe (9150 = Tor Browser, 9050 = standalone daemon)
        for port in dict.fromkeys([CONFIG.tor_socks_port, 9150, 9050]):
            with contextlib.suppress(OSError):
                with socket.create_connection(
                    (CONFIG.tor_socks_host, port), timeout=2.0
                ):
                    result["reachable"] = True
                    result["socks_port"] = port
                    break

        # 2 — Control port probe (9151 = Tor Browser, 9051 = standalone daemon)
        for ctrl_port in dict.fromkeys([CONFIG.tor_control_port, 9151, 9051]):
            with contextlib.suppress(OSError):
                with socket.create_connection(
                    (CONFIG.tor_socks_host, ctrl_port), timeout=2.0
                ):
                    result["control_reachable"] = True
                    break

        # 3 — Real HTTP circuit test through the Tor SOCKS proxy.
        #     Uses httpbin (plain HTTP, no TLS cert issues) so it's fast.
        #     Only attempted when a SOCKS port was found.
        if result["reachable"] and result["socks_port"]:
            try:
                import httpx as _httpx
                proxy = f"socks5://{CONFIG.tor_socks_host}:{result['socks_port']}"
                with _httpx.Client(
                    proxy=proxy,
                    timeout=10.0,
                    follow_redirects=True,
                    verify=False,
                ) as client:
                    resp = client.get("http://httpbin.org/ip")
                    result["circuit_ok"] = (resp.status_code == 200)
            except Exception:
                result["circuit_ok"] = False

        return result

    tor = await loop.run_in_executor(None, _check_tor)

    return HealthResponse(
        status="ok" if db_status == "ok" else "degraded",
        database=db_status,
        tor_reachable=tor["reachable"],
        tor_socks_port=tor["socks_port"],
        tor_control_reachable=tor["control_reachable"],
        tor_circuit_ok=tor["circuit_ok"],
    )


# ── Feedback endpoints ─────────────────────────────────────────────────────────

@app.post(
    "/feedback",
    summary="Save a comment for a URL",
    dependencies=[Depends(require_api_key)],
)
async def add_feedback_endpoint(body: FeedbackCreate) -> dict:
    """
    Persist a free-text comment about a tested URL, along with the
    strategy that was active and whether the scrape succeeded.
    These comments are used to guide codebase improvements.
    """
    await add_feedback(
        body.url,
        body.comment,
        body.strategy_used,
        body.scrape_success,
    )
    return {"ok": True}


@app.get(
    "/feedback",
    response_model=FeedbackListResponse,
    summary="List feedback comments",
    dependencies=[Depends(require_api_key)],
)
async def get_feedback_endpoint(url: str = None) -> FeedbackListResponse:
    """
    Return all feedback comments, or only those for a specific URL
    when the ``url`` query parameter is provided.
    """
    raw = await get_feedback_for_url(url) if url else await get_all_feedback()
    items = [FeedbackItem(**row) for row in raw]
    return FeedbackListResponse(items=items, total=len(items))


@app.delete(
    "/feedback/{feedback_id}",
    summary="Delete a feedback comment",
    dependencies=[Depends(require_api_key)],
)
async def delete_feedback_endpoint(feedback_id: int) -> dict:
    """Delete a single feedback comment by ID."""
    await delete_feedback(feedback_id)
    return {"ok": True}


@app.delete(
    "/feedback",
    summary="Delete all feedback comments",
    dependencies=[Depends(require_api_key)],
)
async def delete_all_feedback_endpoint() -> dict:
    """Delete every feedback comment."""
    deleted = await delete_all_feedback()
    return {"ok": True, "deleted": deleted}


# ── Test UI (static files, no auth required) ───────────────────────────────────
import os as _os
_static_dir = _os.path.join(_os.path.dirname(__file__), "static")
if _os.path.isdir(_static_dir):
    app.mount("/ui", StaticFiles(directory=_static_dir, html=True), name="ui")


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

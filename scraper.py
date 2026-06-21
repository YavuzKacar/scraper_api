"""
scraper.py -- Central scraping orchestrator.

Responsibilities
----------------
1. Domain rate limiting -- enforce per-domain request spacing.
2. Fixed 5-stage waterfall -- static -> browser -> tor -> scrape_do -> zyte,
   stopping at the first stage that returns usable content.
3. Retry loop -- up to CONFIG.retry_count attempts per stage.
4. Safety limits -- response-size cap and page-load timeout, enforced per stage.
5. Persistence -- write the winning provider, cost, and full audit log.

Note: the legacy "use the last learned domain strategy first" reordering has
been removed in favour of the fixed waterfall order below. The winning
strategy is still recorded in domain_strategies for analytics.

Public API
----------
scrape(request: ScrapeRequest) -> ScrapeResponse
"""
from __future__ import annotations

import asyncio
import logging
import random
import time
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlparse

import httpx

from config import CONFIG
from database import (
    get_domain_strategy,
    log_scrape_attempt,
    update_scrape_result,
    upsert_domain_strategy,
    upsert_url_record,
)
from fingerprint import FingerprintProfile, build_http_headers, get_random_profile
from models import (
    ScrapingStrategy,
    ScrapeRequest,
    ScrapeResponse,
    URLRecord,  # used by upsert_url_record
)
from utils import (
    enforce_domain_rate_limit,
    human_delay,
    is_scrape_failure,
    read_text_capped,
)

logger = logging.getLogger(__name__)

# ── Concurrency control ───────────────────────────────────────────────────────
# Cap the number of simultaneous scrape operations to prevent spawning too
# many browser tabs or Tor circuits at once.
_scrape_semaphore: asyncio.Semaphore = asyncio.Semaphore(CONFIG.max_concurrent_scrapes)

# ── Result cache ──────────────────────────────────────────────────────────────
# Simple TTL dict: url -> (ScrapeResponse, monotonic timestamp).
# Avoids redundant scraping when the same URL is requested repeatedly within
# result_cache_ttl_seconds.
_scrape_cache: dict[str, tuple[ScrapeResponse, float]] = {}
_MAX_CACHE_ENTRIES = 5000


def _cache_get(url: str) -> Optional[ScrapeResponse]:
    ttl = CONFIG.result_cache_ttl_seconds
    if ttl <= 0:
        return None
    entry = _scrape_cache.get(url)
    if entry is None:
        return None
    response, ts = entry
    if (time.monotonic() - ts) < ttl:
        return response
    del _scrape_cache[url]
    return None


def _cache_set(url: str, response: ScrapeResponse) -> None:
    if CONFIG.result_cache_ttl_seconds <= 0:
        return
    _scrape_cache[url] = (response, time.monotonic())
    # Evict the oldest entry when the cache grows too large.
    if len(_scrape_cache) > _MAX_CACHE_ENTRIES:
        oldest = min(_scrape_cache, key=lambda k: _scrape_cache[k][1])
        del _scrape_cache[oldest]


# -- Helpers ------------------------------------------------------------------

def _root_url(url: str) -> str:
    """Return scheme://host for a URL (e.g. 'https://x.com')."""
    p = urlparse(url)
    return f"{p.scheme}://{p.netloc}"


# Fixed waterfall order and the cost charged for each stage on success.
_WATERFALL: list[ScrapingStrategy] = [
    ScrapingStrategy.static,
    ScrapingStrategy.browser,
    ScrapingStrategy.tor,
    ScrapingStrategy.scrape_do,
    ScrapingStrategy.zyte,
]


def _cost_for(strategy: ScrapingStrategy) -> float:
    return {
        ScrapingStrategy.static: CONFIG.cost_static,
        ScrapingStrategy.browser: CONFIG.cost_browser,
        ScrapingStrategy.tor: CONFIG.cost_tor,
        ScrapingStrategy.scrape_do: CONFIG.cost_scrape_do,
        ScrapingStrategy.zyte: CONFIG.cost_zyte,
    }.get(strategy, 0.0)


# -- Static (httpx) scraper ---------------------------------------------------

async def _scrape_static(url: str, profile: FingerprintProfile) -> str:
    """Lightweight httpx GET with fingerprinted headers, capped response size."""
    from url_security import httpx_redirect_validator_hook

    headers = build_http_headers(profile, url)
    await human_delay(0.3, 0.8)

    async with httpx.AsyncClient(
        timeout=CONFIG.request_timeout,
        follow_redirects=True,
        verify=False,
        limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
        event_hooks={"request": [httpx_redirect_validator_hook]},
    ) as client:
        async with client.stream("GET", url, headers=headers) as response:
            response.raise_for_status()
            return await read_text_capped(response, CONFIG.max_response_size_bytes)


# -- Strategy dispatcher ------------------------------------------------------

async def _dispatch(
    url: str,
    strategy: ScrapingStrategy,
    profile: FingerprintProfile,
) -> str:
    """Route the scrape request to the matching implementation."""
    if strategy == ScrapingStrategy.static:
        return await _scrape_static(url, profile)

    if strategy in (ScrapingStrategy.browser, ScrapingStrategy.hybrid):
        # hybrid is a legacy DB value -- treat it the same as browser.
        from browser_scraper import scrape_with_browser_async
        return await scrape_with_browser_async(url, profile, headless=False)

    if strategy == ScrapingStrategy.tor:
        from tor_scraper import scrape_with_tor
        return await scrape_with_tor(url, profile)

    if strategy == ScrapingStrategy.scrape_do:
        from scrape_do_provider import scrape_with_scrape_do
        return await scrape_with_scrape_do(url)

    if strategy == ScrapingStrategy.zyte:
        from zyte_provider import scrape_with_zyte
        return await scrape_with_zyte(url)

    raise ValueError(f"Cannot dispatch strategy: {strategy}")


# -- Main orchestrator --------------------------------------------------------

async def scrape(request: ScrapeRequest) -> ScrapeResponse:
    """
    Orchestrate a full scrape lifecycle for *request.url*.

    Steps:
      1. Return a cached result if one exists and force_strategy is not set.
      2. Apply domain rate limit.
      3. Acquire the global concurrency semaphore (caps parallel provider ops).
      4. Walk the fixed waterfall: static -> browser -> tor -> scrape_do -> zyte.
      5. Persist the winning strategy, write the audit log, and cache the result.

    Callers are expected to have already run url_security.validate_url_for_scraping()
    before calling this function (see app.py) -- this function does not re-check SSRF.
    """
    url = request.url
    root = _root_url(url)
    start_ts = time.monotonic()

    # -- Step 1: cache lookup (skip when caller forces a specific strategy) ---
    if not request.force_strategy:
        cached = _cache_get(url)
        if cached is not None:
            logger.info("Cache hit for %s", url)
            return cached

    # -- Step 2: determine strategy order -------------------------------------
    if request.force_strategy:
        try:
            strategies = [ScrapingStrategy(request.force_strategy)]
        except ValueError:
            strategies = list(_WATERFALL)
    else:
        strategies = list(_WATERFALL)

    # -- Step 3: domain rate limit --------------------------------------------
    await enforce_domain_rate_limit(url, CONFIG.domain_rate_limit_seconds)

    # -- Step 4: concurrency gate ---------------------------------------------
    # Prevents the server from spawning more simultaneous provider operations
    # than MAX_CONCURRENT_SCRAPES regardless of request burst size.
    async with _scrape_semaphore:
        # -- Step 5: try each strategy in order -------------------------------
        html: str = ""
        last_error: str = ""
        winning_strategy: Optional[ScrapingStrategy] = None

        for strategy in strategies:
            for attempt in range(1, CONFIG.retry_count + 1):
                profile = get_random_profile()
                logger.info(
                    "Scrape attempt %d/%d strategy=%s url=%s",
                    attempt, CONFIG.retry_count, strategy.value, url,
                )

                try:
                    html = await _dispatch(url, strategy, profile)
                except Exception as exc:
                    last_error = f"{strategy.value}: {exc}"
                    logger.warning(
                        "Attempt %d/%d failed (%s): %s",
                        attempt, CONFIG.retry_count, strategy.value, exc,
                    )
                    html = ""

                if html and not is_scrape_failure(html, 200):
                    winning_strategy = strategy
                    break

                if attempt < CONFIG.retry_count:
                    base_wait = 2 ** (attempt - 1) * 2.0
                    jitter = random.uniform(-base_wait * 0.5, base_wait * 0.5)
                    wait = max(1.0, base_wait + jitter)
                    logger.debug("Retry backoff %.1fs before attempt %d", wait, attempt + 1)
                    await asyncio.sleep(wait)
                    if strategy == ScrapingStrategy.tor:
                        from tor_scraper import recover_tor_circuit
                        await recover_tor_circuit()

            if winning_strategy is not None:
                break

    duration_ms = int((time.monotonic() - start_ts) * 1000)

    # -- Step 6: persist and return -------------------------------------------
    if winning_strategy is not None:
        cost = _cost_for(winning_strategy)
        await upsert_domain_strategy(root, winning_strategy.value)
        await upsert_url_record(URLRecord(
            url=url,
            scraping_strategy=winning_strategy.value,
            last_checked=datetime.now(timezone.utc),
            last_provider=winning_strategy.value,
            last_cost=cost,
            last_error_reason=None,
        ))
        await update_scrape_result(url, "success")
        await log_scrape_attempt(
            url=url,
            success=True,
            provider=winning_strategy.value,
            status="success",
            cost=cost,
            error_reason=None,
            duration_ms=duration_ms,
            response_bytes=len(html.encode("utf-8", errors="replace")),
        )
        result = ScrapeResponse(
            url=url,
            scraping_success=True,
            message="Scraped successfully.",
            html=html,
            strategy_used=winning_strategy.value,
            provider=winning_strategy.value,
            status="success",
            cost_score=cost,
            error_reason=None,
        )
        _cache_set(url, result)
        return result

    tried = " -> ".join(s.value for s in strategies)
    message = f"Scraping failed after trying [{tried}]. {last_error}".strip()
    await update_scrape_result(url, "failed", error_reason=last_error or None)
    await log_scrape_attempt(
        url=url,
        success=False,
        provider=None,
        status="failed",
        cost=0.0,
        error_reason=last_error or None,
        duration_ms=duration_ms,
        response_bytes=None,
    )
    return ScrapeResponse(
        url=url,
        scraping_success=False,
        message=message,
        strategy_used=None,
        provider=None,
        status="failed",
        cost_score=0.0,
        error_reason=last_error or None,
    )

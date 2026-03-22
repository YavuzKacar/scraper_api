"""
scraper.py -- Central scraping orchestrator.

Responsibilities
----------------
1. Domain strategy lookup -- use the stored per-domain strategy as first attempt.
2. Domain rate limiting -- enforce per-domain request spacing.
3. Strategy dispatch -- try browser first, fall back to tor on failure.
4. Retry loop -- up to CONFIG.retry_count attempts per strategy.
5. Persistence -- write the working strategy to the database.

Public API
----------
scrape(request: ScrapeRequest) -> ScrapeResponse
"""
from __future__ import annotations

import asyncio
import logging
import random
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlparse

import httpx

from config import CONFIG
from database import (
    get_domain_strategy,
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
)

logger = logging.getLogger(__name__)


# -- Helpers ------------------------------------------------------------------

def _root_url(url: str) -> str:
    """Return scheme://host for a URL (e.g. 'https://x.com')."""
    p = urlparse(url)
    return f"{p.scheme}://{p.netloc}"


# -- Static (httpx) scraper ---------------------------------------------------

async def _scrape_static(url: str, profile: FingerprintProfile) -> str:
    """Lightweight httpx GET with fingerprinted headers."""
    headers = build_http_headers(profile, url)
    await human_delay(0.3, 0.8)

    async with httpx.AsyncClient(
        timeout=CONFIG.request_timeout,
        follow_redirects=True,
        verify=False,
        limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
    ) as client:
        response = await client.get(url, headers=headers)
        response.raise_for_status()
        return response.text


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

    raise ValueError(f"Cannot dispatch strategy: {strategy}")


# -- Main orchestrator --------------------------------------------------------

async def scrape(request: ScrapeRequest) -> ScrapeResponse:
    """
    Orchestrate a full scrape lifecycle for *request.url*.

    Steps:
      1. Look up the stored per-domain strategy to decide attempt order.
      2. Apply domain rate limit.
      3. Try browser (or stored strategy) first, fall back to tor.
      4. Persist the working strategy.
    """
    url = request.url
    root = _root_url(url)

    # -- Step 1: determine strategy order -------------------------------------
    if request.force_strategy:
        try:
            strategies = [ScrapingStrategy(request.force_strategy)]
        except ValueError:
            strategies = [ScrapingStrategy.browser, ScrapingStrategy.tor]
    else:
        # Use the stored domain strategy as the first attempt; try the other
        # on failure.  Default order (no prior data) is browser -> tor.
        learned = await get_domain_strategy(root)
        if learned == ScrapingStrategy.tor.value:
            strategies = [ScrapingStrategy.tor, ScrapingStrategy.browser]
        else:
            strategies = [ScrapingStrategy.browser, ScrapingStrategy.tor]

    # -- Step 3: domain rate limit --------------------------------------------
    await enforce_domain_rate_limit(url, CONFIG.domain_rate_limit_seconds)

    # -- Step 4: try each strategy --------------------------------------------
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
                last_error = str(exc)
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
                    from tor_scraper import rotate_tor_identity
                    await rotate_tor_identity()

        if winning_strategy is not None:
            break

    # -- Step 5: persist and return -------------------------------------------
    if winning_strategy is not None:
        await upsert_domain_strategy(root, winning_strategy.value)
        await upsert_url_record(URLRecord(
            url=url,
            scraping_strategy=winning_strategy.value,
            last_checked=datetime.now(timezone.utc),
        ))
        await update_scrape_result(url, "success")
        return ScrapeResponse(
            url=url,
            scraping_success=True,
            message="Scraped successfully.",
            html=html,
            strategy_used=winning_strategy.value,
        )

    await update_scrape_result(url, "failed")
    tried = " -> ".join(s.value for s in strategies)
    return ScrapeResponse(
        url=url,
        scraping_success=False,
        message=f"Scraping failed after trying [{tried}]. {last_error}".strip(),
        strategy_used=None,
    )

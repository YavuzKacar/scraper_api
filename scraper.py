"""
scraper.py — Central scraping orchestrator.

Responsibilities
----------------
1. Cache check  — return stored HTML if fresh (< CACHE_TTL_SECONDS).
2. Classification load/create — load from DB or run classifier.
3. Policy enforcement — private page and blocked-strategy guards.
4. Domain rate limiting — enforce per-domain request spacing.
5. Strategy dispatch — route to the correct scraper implementation.
6. Retry loop — up to CONFIG.retry_count attempts with rotated fingerprints.
7. Dynamic reclassification — detect when a site's behaviour changed.
8. Persistence — write outcomes back to the database.

Public API
----------
scrape(request: ScrapeRequest) → ScrapeResponse
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx

from config import CONFIG
from database import get_url_record, update_scrape_result, upsert_url_record
from fingerprint import FingerprintProfile, build_http_headers, get_random_profile
from models import (
    AntiScrapingProtection,
    Classification,
    ContentType,
    ScrapingStrategy,
    ScrapeRequest,
    ScrapeResponse,
    URLRecord,
)
from utils import (
    enforce_domain_rate_limit,
    human_delay,
    is_scrape_failure,
)

logger = logging.getLogger(__name__)


# ── Static (httpx) scraper ────────────────────────────────────────────────────

async def _scrape_static(url: str, profile: FingerprintProfile) -> str:
    """Lightweight httpx GET with fingerprinted headers."""
    headers = build_http_headers(profile, url)
    await human_delay(0.8, 2.0)

    async with httpx.AsyncClient(
        timeout=CONFIG.request_timeout,
        follow_redirects=True,
        verify=False,
        limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
    ) as client:
        response = await client.get(url, headers=headers)
        response.raise_for_status()
        return response.text


# ── Cache helper ──────────────────────────────────────────────────────────────

def _is_cache_valid(record: URLRecord) -> bool:
    """Return True if the cached HTML is still within the TTL window."""
    if not record.last_success_html or not record.last_checked:
        return False
    age = datetime.now(timezone.utc) - record.last_checked.replace(
        tzinfo=timezone.utc
    )
    return age < timedelta(seconds=CONFIG.cache_ttl_seconds)


# ── Strategy dispatcher ───────────────────────────────────────────────────────

async def _dispatch(
    url: str,
    strategy: ScrapingStrategy,
    profile: FingerprintProfile,
) -> str:
    """Route the scrape request to the matching implementation."""
    if strategy == ScrapingStrategy.static:
        return await _scrape_static(url, profile)

    if strategy == ScrapingStrategy.browser:
        from browser_scraper import scrape_with_browser_async
        return await scrape_with_browser_async(url, profile, headless=CONFIG.headless_browser)

    if strategy == ScrapingStrategy.tor:
        from tor_scraper import scrape_with_tor
        return await scrape_with_tor(url, profile)

    if strategy == ScrapingStrategy.hybrid:
        # Tor for transport anonymity; browser for JS rendering via Tor Firefox
        from tor_scraper import scrape_with_tor_browser, rotate_tor_identity
        import asyncio
        await rotate_tor_identity()
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None,
            scrape_with_tor_browser,
            url,
            profile,
            CONFIG.headless_browser,
            None,
        )

    raise ValueError(f"Cannot dispatch strategy: {strategy}")


# ── Behaviour-change detector ─────────────────────────────────────────────────

def _detect_behaviour_change(
    html: str,
    status_code: int,
    classification: Classification,
) -> bool:
    """
    Return True if the scraped response contradicts the stored classification.

    Triggers reclassification on the next request or scheduler run.
    """
    from utils import detect_challenge_page, detect_captcha

    was_unprotected = (
        classification.antiscraping_protection == AntiScrapingProtection.none
    )
    now_looks_protected = detect_challenge_page(html) or detect_captcha(html)

    if was_unprotected and now_looks_protected:
        logger.info(
            "Behaviour change detected — site now appears protected. "
            "Scheduling reclassification."
        )
        return True

    was_static = classification.content_type == ContentType.static
    now_looks_dynamic = status_code == 200 and len(html.strip()) < 300
    if was_static and now_looks_dynamic:
        logger.info(
            "Behaviour change detected — static site now returns minimal HTML. "
            "Scheduling reclassification."
        )
        return True

    return False


# ── Main orchestrator ─────────────────────────────────────────────────────────

async def scrape(request: ScrapeRequest) -> ScrapeResponse:
    """
    Orchestrate a full scrape lifecycle for *request.url*.

    Steps:
      1. Load URL record from DB.
      2. If no record, or force_reclassify, run classifier.
      3. Enforce private-page and blocked-strategy policies.
      4. Return cached HTML if fresh.
      5. Apply domain rate limit.
      6. Retry loop: dispatch → check failure → rotate fingerprint.
      7. Persist result.
    """
    url = request.url
    record: Optional[URLRecord] = await get_url_record(url)
    needs_classify = (
        record is None
        or not record.is_classified()
        or request.force_reclassify
        or (
            record.classification_confidence is not None
            and record.classification_confidence < CONFIG.low_confidence_threshold
        )
    )

    # ── Step 1: classify if needed ────────────────────────────────────────────
    if needs_classify:
        logger.info("Classifying URL: %s", url)
        from classifier import classify_url
        classification = await classify_url(url)

        new_record = URLRecord(
            url=url,
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
        await upsert_url_record(new_record)
        record = new_record
    else:
        classification = record.to_classification()

    strategy = ScrapingStrategy(record.scraping_strategy)

    # ── Step 2: private-page guard ────────────────────────────────────────────
    from models import PublicPage
    if record.is_public_page == PublicPage.no.value:
        return ScrapeResponse(
            url=url,
            scraping_success=False,
            message="This page is not public.",
            classification=classification,
        )

    # ── Step 3: blocked-strategy guard ───────────────────────────────────────
    if strategy == ScrapingStrategy.blocked:
        return ScrapeResponse(
            url=url,
            scraping_success=False,
            message="This website uses advanced anti-scraping protection.",
            classification=classification,
        )

    # ── Step 4: cache check ───────────────────────────────────────────────────
    bypass_cache = request.force_reclassify or request.force_scrape
    if not bypass_cache and _is_cache_valid(record):
        logger.info("Returning cached HTML for %s", url)
        return ScrapeResponse(
            url=url,
            scraping_success=True,
            message="Returned from cache.",
            html=record.last_success_html,
            classification=classification,
            cached=True,
            strategy_used=strategy.value,
        )

    # ── Step 5: domain rate limit ─────────────────────────────────────────────
    await enforce_domain_rate_limit(url, CONFIG.domain_rate_limit_seconds)

    # ── Step 6: retry loop ────────────────────────────────────────────────────
    html: str = ""
    last_error: str = ""

    for attempt in range(1, CONFIG.retry_count + 1):
        profile = get_random_profile()
        logger.info(
            "Scrape attempt %d/%d — strategy=%s profile=%s url=%s",
            attempt,
            CONFIG.retry_count,
            strategy.value,
            profile.name,
            url,
        )

        try:
            html = await _dispatch(url, strategy, profile)
        except Exception as exc:
            last_error = str(exc)
            logger.warning("Attempt %d failed: %s", attempt, exc)
            html = ""

        if html and not is_scrape_failure(html, 200):
            break

        if attempt < CONFIG.retry_count:
            await human_delay(1.5, 3.5)
            # Rotate Tor circuit between retries when using Tor paths
            if strategy in (ScrapingStrategy.tor, ScrapingStrategy.hybrid):
                from tor_scraper import rotate_tor_identity
                await rotate_tor_identity()

    # ── Step 7: evaluate and persist ─────────────────────────────────────────
    success = bool(html) and not is_scrape_failure(html, 200)

    if success:
        # Check for behaviour change — mark for reclassification next time
        if _detect_behaviour_change(html, 200, classification):
            await upsert_url_record(
                URLRecord(
                    url=url,
                    content_type=record.content_type,
                    antiscraping_protection=record.antiscraping_protection,
                    tor_network_available=record.tor_network_available,
                    undetected_browser_available=record.undetected_browser_available,
                    is_public_page=record.is_public_page,
                    scraping_strategy=record.scraping_strategy,
                    # Force low confidence to trigger reclassification next time
                    classification_confidence=0.3,
                    last_checked=datetime.now(timezone.utc),
                )
            )

        await update_scrape_result(url, "success", html)
        return ScrapeResponse(
            url=url,
            scraping_success=True,
            message="Scraped successfully.",
            html=html,
            classification=classification,
            cached=False,
            strategy_used=strategy.value,
        )

    await update_scrape_result(url, "failed", None)
    return ScrapeResponse(
        url=url,
        scraping_success=False,
        message=f"Scraping failed after {CONFIG.retry_count} attempts. {last_error}".strip(),
        classification=classification,
        strategy_used=strategy.value,
    )

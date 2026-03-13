"""
utils.py — Shared utilities for the Scraper API.

Covers:
  - Human-like async delay engine
  - Domain-level rate limiting
  - Challenge / CAPTCHA detection helpers
  - DOM quality checks
  - URL helpers
"""
from __future__ import annotations

import asyncio
import logging
import random
import re
import time
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

# ── Human-like delay engine ───────────────────────────────────────────────────

_DELAY_MIN = 1.2   # seconds
_DELAY_MAX = 4.5   # seconds


async def human_delay(
    min_s: float = _DELAY_MIN,
    max_s: float = _DELAY_MAX,
) -> None:
    """Async sleep for a random duration in [min_s, max_s]."""
    delay = random.uniform(min_s, max_s)
    logger.debug("Human delay: %.2fs", delay)
    await asyncio.sleep(delay)


def human_delay_sync(
    min_s: float = _DELAY_MIN,
    max_s: float = _DELAY_MAX,
) -> None:
    """Blocking sleep variant — use inside thread-pool workers (browser scraper)."""
    delay = random.uniform(min_s, max_s)
    logger.debug("Human delay (sync): %.2fs", delay)
    time.sleep(delay)


# ── Domain rate limiter ───────────────────────────────────────────────────────

# In-memory: maps domain → monotonic timestamp of last request.
# Fine for a single-process server; does not survive restarts.
_domain_last_request: dict[str, float] = {}


async def enforce_domain_rate_limit(url: str, min_delay_s: float) -> None:
    """
    Ensure at least *min_delay_s* seconds have elapsed since the last
    request to the same domain.  Awaits the remainder if needed.
    """
    domain = _extract_domain(url)
    last = _domain_last_request.get(domain, 0.0)
    elapsed = time.monotonic() - last
    if elapsed < min_delay_s:
        wait = min_delay_s - elapsed
        logger.debug("Rate limit: sleeping %.2fs for domain '%s'", wait, domain)
        await asyncio.sleep(wait)
    _domain_last_request[domain] = time.monotonic()


# ── Challenge / CAPTCHA detection ─────────────────────────────────────────────

_CF_CHALLENGE_RE = re.compile(
    r"(checking your browser|attention required.*cloudflare|"
    r"please wait.*ddos|just a moment.*cloudflare)",
    re.IGNORECASE | re.DOTALL,
)
_CAPTCHA_RE = re.compile(
    r"(recaptcha|hcaptcha|funcaptcha|turnstile|captcha|challenge-form|data-sitekey)",
    re.IGNORECASE,
)
_BLOCK_RE = re.compile(
    r"(access denied|403 forbidden|blocked|bot detected|"
    r"automated access|suspicious activity)",
    re.IGNORECASE,
)

# Application-level error pages (no size gate — these pages can be large).
_APP_ERROR_RE = re.compile(
    r"(something went wrong|try again later|we('re| are) having trouble|"
    r"an error has occurred|page (isn'?t|is not) available|service unavailable|"
    r"error loading (page|content))",
    re.IGNORECASE,
)


def detect_challenge_page(html: str, headers: dict[str, str] | None = None) -> bool:
    """Return True if *html* looks like a Cloudflare or bot-detection challenge."""
    if _CF_CHALLENGE_RE.search(html):
        return True
    if headers:
        lower_keys = {k.lower() for k in headers}
        if "cf-mitigated" in lower_keys or "cf-chl-bypass" in lower_keys:
            return True
    return False


def detect_captcha(html: str) -> bool:
    """Return True if the page contains a CAPTCHA widget."""
    return bool(_CAPTCHA_RE.search(html))


def detect_block_page(html: str, status_code: int = 0) -> bool:
    """Return True if the response appears to be a hard block response."""
    if status_code in (403, 429, 503):
        return True
    return bool(_BLOCK_RE.search(html)) and len(html.strip()) < 4096


def is_empty_dom(html: str) -> bool:
    """Return True if the HTML body carries no visible content."""
    stripped = re.sub(r"<[^>]+>", "", html).strip()
    return len(stripped) < 100


def detect_app_error_page(html: str) -> bool:
    """Return True if the page reports a generic application-level error."""
    return bool(_APP_ERROR_RE.search(html))


def is_scrape_failure(html: str, status_code: int, headers: dict[str, str] | None = None) -> bool:
    """
    Aggregate check: return True when any failure mode is detected.

    Used by the retry engine to decide whether to retry.
    """
    return (
        is_empty_dom(html)
        or detect_challenge_page(html, headers)
        or detect_captcha(html)
        or detect_block_page(html, status_code)
        or detect_app_error_page(html)
    )


# ── URL helpers ───────────────────────────────────────────────────────────────

def _extract_domain(url: str) -> str:
    """Return the netloc component (host[:port]) of *url*."""
    return urlparse(url).netloc


def domain_from_url(url: str) -> str:
    """Public alias for _extract_domain."""
    return _extract_domain(url)

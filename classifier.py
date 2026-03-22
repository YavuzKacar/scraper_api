"""
classifier.py — URL classification engine.

Analyses a URL across five dimensions and returns a Classification with a
confidence score.  Each dimension uses multiple independent signals so that
confidence accumulates rather than relying on a single heuristic.

Classification dimensions
--------------------------
1. content_type          — static | dynamic
2. antiscraping_protection — none | protected
3. tor_network_available — yes | no
4. undetected_browser_available — yes | no
5. is_public_page        — yes | no

Public API
----------
classify_url(url)  → Classification
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import socket
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlparse

import httpx

from config import CONFIG
from fingerprint import build_http_headers, get_random_profile
from models import (
    AntiScrapingProtection,
    BrowserAvailability,
    Classification,
    ContentType,
    PublicPage,
    ScrapingStrategy,
    TorAvailability,
)
from strategy import determine_strategy, get_domain_override

logger = logging.getLogger(__name__)

# ── Pattern libraries ─────────────────────────────────────────────────────────

# Signals that the JavaScript framework renders the page content
_JS_FRAMEWORK_PATTERNS = re.compile(
    r"(react|vue|angular|next\.js|nuxt|gatsby|svelte|ember|backbone|knockout)",
    re.IGNORECASE,
)
_SPA_MOUNT_PATTERNS = re.compile(
    r'(<div\s+id=["\']app["\']|<div\s+id=["\']root["\']|ng-app|v-app)',
    re.IGNORECASE,
)
# Very little meaningful content in <body>
_THIN_BODY_PATTERN = re.compile(
    r"<body[^>]*>\s*(<script[^>]*>.*?</script>|<!--.*?-->|\s)*\s*</body>",
    re.IGNORECASE | re.DOTALL,
)

# Anti-scraping signals
_CLOUDFLARE_PATTERNS = re.compile(
    r"(cf-ray|__cf_bm|cloudflare|cf-challenge|cfduid|cf_clearance)",
    re.IGNORECASE,
)
_CLOUDFLARE_HTML = re.compile(
    r"(checking your browser|attention required.*cloudflare|enable javascript and cookies)",
    re.IGNORECASE,
)
_CAPTCHA_PATTERNS = re.compile(
    r"(recaptcha|hcaptcha|funcaptcha|captcha\.js|challenge-form|"
    r"g-recaptcha|data-sitekey)",
    re.IGNORECASE,
)
_BOT_DETECTION_HEADERS = frozenset(
    {"x-datadome", "x-kasada", "x-akamai-edgescape", "x-bot-score", "x-distil-cs"}
)

# Login/paywall signals
_LOGIN_PATTERNS = re.compile(
    r"(login|sign.?in|log.?in|authenticate|paywall|subscription.?required|"
    r"members?.?only|register.?to.?view|create.?an.?account.?to)",
    re.IGNORECASE,
)
_LOGIN_REDIRECT_PATHS = re.compile(
    r"/(login|signin|sign-in|auth|account/login|user/login)",
    re.IGNORECASE,
)


# ── Individual dimension detectors ────────────────────────────────────────────

def _detect_content_type(
    html: str,
    headers: dict[str, str],
    status_code: int,
) -> tuple[ContentType, float]:
    """
    Return (ContentType, confidence).

    Confidence accumulates up to 1.0 across multiple independent signals.
    """
    signals: list[bool] = []

    # Signal 1 — JS framework fingerprints in source
    signals.append(bool(_JS_FRAMEWORK_PATTERNS.search(html)))

    # Signal 2 — SPA mount point in DOM
    signals.append(bool(_SPA_MOUNT_PATTERNS.search(html)))

    # Signal 3 — Body is nearly empty (JS renders everything)
    signals.append(bool(_THIN_BODY_PATTERN.search(html)) and len(html) < 5000)

    # Signal 4 — X-Powered-By or framework hints in response headers
    server = headers.get("x-powered-by", "").lower()
    signals.append(any(fw in server for fw in ("next.js", "nuxt", "react")))

    # Signal 5 — Page bundles obvious JS entry chunks
    signals.append(bool(re.search(r'src=["\'][^"\']*(?:bundle|chunk|main)\.[a-f0-9]+\.js', html)))

    dynamic_count = sum(signals)
    static_count = len(signals) - dynamic_count

    if dynamic_count >= 3:
        return ContentType.dynamic, min(0.5 + 0.1 * dynamic_count, 1.0)
    if static_count >= 3:
        return ContentType.static, min(0.5 + 0.1 * static_count, 1.0)
    # Ambiguous — lean static but low confidence
    return ContentType.static, 0.45


def _detect_antiscraping(
    html: str,
    headers: dict[str, str],
    status_code: int,
) -> tuple[AntiScrapingProtection, float]:
    """Return (AntiScrapingProtection, confidence)."""
    signals: list[bool] = []

    # Signal 1 — Cloudflare response headers
    signals.append(any(k.lower() in _BOT_DETECTION_HEADERS for k in headers))
    signals.append(bool(_CLOUDFLARE_PATTERNS.search(" ".join(headers.keys()))))

    # Signal 2 — Cloudflare challenge page in HTML
    signals.append(bool(_CLOUDFLARE_HTML.search(html)))

    # Signal 3 — CAPTCHA widgets in HTML
    signals.append(bool(_CAPTCHA_PATTERNS.search(html)))

    # Signal 4 — HTTP 403 / 429
    signals.append(status_code in (403, 429, 503))

    # Signal 5 — Very short HTML for a 200 response (block page)
    signals.append(status_code == 200 and len(html.strip()) < 512)

    # Signal 6 — 200 response whose content is actually a failure/error page
    # (covers custom bot detection like X.com that doesn't use Cloudflare/CAPTCHA)
    if status_code == 200 and html:
        from utils import is_scrape_failure
        signals.append(is_scrape_failure(html, status_code))
    else:
        signals.append(False)

    protected_count = sum(signals)

    if protected_count >= 2:
        return AntiScrapingProtection.protected, min(0.5 + 0.1 * protected_count, 1.0)
    if protected_count == 0:
        return AntiScrapingProtection.none, 0.85
    # One weak signal — uncertain
    return AntiScrapingProtection.none, 0.5


def _detect_public_page(
    html: str,
    headers: dict[str, str],
    status_code: int,
    final_url: str,
) -> tuple[PublicPage, float]:
    """Return (PublicPage, confidence)."""
    signals: list[bool] = []

    # Signal 1 — HTTP 401 / 403
    signals.append(status_code in (401, 403))

    # Signal 2 — Redirected to a login path
    parsed = urlparse(final_url)
    signals.append(bool(_LOGIN_REDIRECT_PATHS.search(parsed.path)))

    # Signal 3 — Login/paywall keywords in visible HTML
    signals.append(bool(_LOGIN_PATTERNS.search(html)))

    # Signal 4 — WWW-Authenticate header
    signals.append("www-authenticate" in {k.lower() for k in headers})

    private_count = sum(signals)

    if private_count >= 2:
        return PublicPage.no, min(0.5 + 0.15 * private_count, 1.0)
    if private_count == 0:
        return PublicPage.yes, 0.85
    return PublicPage.yes, 0.55


# ── Tor availability probe ────────────────────────────────────────────────────

def _tor_port_open() -> Optional[int]:
    """
    Return the first reachable Tor SOCKS port from the known candidate list,
    or None if Tor is not running.

    Tries CONFIG.tor_socks_port first (user-configured), then falls through
    to the Tor Browser port (9150) and the standalone daemon port (9050).
    """
    for port in dict.fromkeys([CONFIG.tor_socks_port, 9150, 9050]):  # deduped, ordered
        with contextlib.suppress(OSError):
            with socket.create_connection(
                (CONFIG.tor_socks_host, port), timeout=2.0
            ):
                return port
    return None


async def _probe_tor(url: str) -> tuple[TorAvailability, float]:
    """
    Attempt a lightweight httpx request through the Tor SOCKS5 proxy.
    Returns (TorAvailability, confidence).
    """
    tor_port = _tor_port_open()
    if tor_port is None:
        logger.debug("Tor SOCKS not reachable on any known port")
        return TorAvailability.no, 0.9

    proxy_url = f"socks5://{CONFIG.tor_socks_host}:{tor_port}"
    profile = get_random_profile()
    headers = build_http_headers(profile)

    try:
        async with httpx.AsyncClient(
            proxy=proxy_url,
            timeout=CONFIG.classification_timeout,
            follow_redirects=True,
            verify=False,
        ) as client:
            resp = await client.get(url, headers=headers)
            if resp.status_code >= 500:
                return TorAvailability.no, 0.7
            # Validate content quality — a 200 that is a failure page means
            # Tor alone can't bypass the site's bot detection.
            from utils import is_scrape_failure
            if is_scrape_failure(resp.text or "", resp.status_code):
                logger.debug("Tor probe returned failure-page content for %s", url)
                return TorAvailability.no, 0.75
            return TorAvailability.yes, 0.85
    except Exception as exc:
        logger.debug("Tor probe failed for %s: %s", url, exc)
        return TorAvailability.no, 0.75


# ── Undetected-browser availability probe ─────────────────────────────────────

async def _probe_browser(
    url: str,
    html_from_static: str,
) -> tuple[BrowserAvailability, float]:
    """
    Heuristic: if the static probe already returned a challenge page
    or CAPTCHA, attempt a brief undetected-browser launch to verify
    whether it can bypass protection.

    To keep classification fast we only launch the browser when the
    static probe showed protection indicators.  Otherwise we infer from
    the static result.
    """
    # Check whether protection is evident from static fetch — includes both
    # standard Cloudflare/CAPTCHA challenges AND generic failure pages
    # (e.g. X.com-style "something went wrong" bot detection).
    from utils import is_scrape_failure
    has_challenge = bool(
        _CLOUDFLARE_HTML.search(html_from_static)
        or _CAPTCHA_PATTERNS.search(html_from_static)
        or is_scrape_failure(html_from_static, 0)
    )

    if not has_challenge:
        # Static fetch looked fine — browser would work too
        return BrowserAvailability.yes, 0.8

    # Try undetected-chromedriver
    try:
        from browser_scraper import scrape_with_browser
        from fingerprint import get_random_profile as _p

        loop = asyncio.get_running_loop()
        result_html = await loop.run_in_executor(
            None, scrape_with_browser, url, _p()
        )
        if result_html and not _CLOUDFLARE_HTML.search(result_html):
            return BrowserAvailability.yes, 0.85
        return BrowserAvailability.no, 0.75
    except Exception as exc:
        logger.debug("Browser availability probe failed for %s: %s", url, exc)
        # Can't be certain — assume browser might work
        return BrowserAvailability.yes, 0.45


# ── Confidence aggregation ─────────────────────────────────────────────────────

def _aggregate_confidence(confidences: list[float]) -> float:
    """
    Combine per-dimension confidences into a single overall score.

    Uses the geometric mean so that one very uncertain dimension pulls
    the overall score down noticeably.
    """
    if not confidences:
        return 0.0
    product = 1.0
    for c in confidences:
        product *= max(0.01, min(c, 1.0))
    return round(product ** (1.0 / len(confidences)), 3)


# ── Main classifier ───────────────────────────────────────────────────────────

async def classify_url(url: str) -> Classification:
    """
    Fully classify a URL.

    1. Perform a standard httpx GET (with randomised fingerprint).
    2. Probe Tor availability concurrently.
    3. Probe browser availability when protection signals are present.
    4. Compute scraping strategy.
    5. Return Classification with aggregated confidence.
    """
    profile = get_random_profile()
    headers = build_http_headers(profile)

    # ── Step 1: standard HTTP probe ───────────────────────────────────────────
    html = ""
    response_headers: dict[str, str] = {}
    status_code = 0
    final_url = url

    try:
        async with httpx.AsyncClient(
            timeout=CONFIG.classification_timeout,
            follow_redirects=True,
            verify=False,
            limits=httpx.Limits(max_connections=5),
        ) as client:
            resp = await client.get(url, headers=headers)
            html = resp.text or ""
            response_headers = dict(resp.headers)
            status_code = resp.status_code
            final_url = str(resp.url)
    except httpx.TimeoutException:
        logger.warning("Classification HTTP probe timed out for %s", url)
        status_code = 0
    except Exception as exc:
        logger.warning("Classification HTTP probe failed for %s: %s", url, exc)
        status_code = 0

    # ── Step 2: run dimension detectors ───────────────────────────────────────
    content_type, ct_conf = _detect_content_type(html, response_headers, status_code)
    antiscraping, as_conf = _detect_antiscraping(html, response_headers, status_code)
    is_public, pp_conf = _detect_public_page(html, response_headers, status_code, final_url)

    # ── Step 3: probe Tor and browser concurrently ─────────────────────────────
    # Use gather so both tasks are always awaited even if one raises, preventing
    # an abandoned task from keeping a Chrome/Firefox process running.
    results = await asyncio.gather(
        _probe_tor(url),
        _probe_browser(url, html),
        return_exceptions=True,
    )

    tor_result, browser_result = results

    if isinstance(tor_result, Exception):
        logger.warning("Tor probe failed during classification of %s: %s", url, tor_result)
        from models import TorAvailability
        tor_availability, tor_conf = TorAvailability.no, 0.5
    else:
        tor_availability, tor_conf = tor_result

    if isinstance(browser_result, Exception):
        logger.warning("Browser probe failed during classification of %s: %s", url, browser_result)
        from models import BrowserAvailability
        browser_availability, br_conf = BrowserAvailability.no, 0.5
    else:
        browser_availability, br_conf = browser_result

    # ── Step 4: choose strategy ───────────────────────────────────────────────
    partial = Classification(
        content_type=content_type,
        antiscraping_protection=antiscraping,
        tor_network_available=tor_availability,
        undetected_browser_available=browser_availability,
        is_public_page=is_public,
        scraping_strategy=ScrapingStrategy.static,     # placeholder
        classification_confidence=0.0,                 # placeholder
    )
    strategy = determine_strategy(partial)
    # Apply domain override: certain well-known domains require a fixed
    # strategy regardless of what the probes returned (e.g. X.com hybrid,
    # Amazon browser).
    domain_override = get_domain_override(url)
    if domain_override is not None:
        logger.info(
            "Domain override applied for %s: %s → %s",
            url, strategy.value, domain_override.value,
        )
        strategy = domain_override

    # ── Step 5: aggregate confidence ─────────────────────────────────────────
    confidence = _aggregate_confidence([ct_conf, as_conf, pp_conf, tor_conf, br_conf])

    return Classification(
        content_type=content_type,
        antiscraping_protection=antiscraping,
        tor_network_available=tor_availability,
        undetected_browser_available=browser_availability,
        is_public_page=is_public,
        scraping_strategy=strategy,
        classification_confidence=confidence,
    )

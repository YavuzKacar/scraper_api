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
_MAX_TRACKED_DOMAINS = 2000
_domain_last_request: dict[str, float] = {}
# Per-domain asyncio locks prevent the race where two concurrent coroutines
# both read the same stale timestamp before either writes back.
_domain_locks: dict[str, asyncio.Lock] = {}


def _get_domain_lock(domain: str) -> asyncio.Lock:
    """Return (creating if needed) the per-domain rate-limit lock."""
    if domain not in _domain_locks:
        _domain_locks[domain] = asyncio.Lock()
    return _domain_locks[domain]


async def enforce_domain_rate_limit(url: str, min_delay_s: float) -> None:
    """
    Ensure at least *min_delay_s* seconds have elapsed since the last
    request to the same domain.  Awaits the remainder if needed.

    A per-domain lock prevents two concurrent coroutines from both seeing
    the same stale timestamp and bypassing the rate limit.
    """
    domain = _extract_domain(url)
    lock = _get_domain_lock(domain)
    async with lock:
        last = _domain_last_request.get(domain, 0.0)
        elapsed = time.monotonic() - last
        if elapsed < min_delay_s:
            wait = min_delay_s - elapsed
            logger.debug("Rate limit: sleeping %.2fs for domain '%s'", wait, domain)
            await asyncio.sleep(wait)
        _domain_last_request[domain] = time.monotonic()
        # Evict the oldest entry when the tracking dict grows too large to
        # prevent unbounded memory growth over a long-running session.
        if len(_domain_last_request) > _MAX_TRACKED_DOMAINS:
            oldest = min(_domain_last_request, key=_domain_last_request.__getitem__)
            del _domain_last_request[oldest]


# ── Challenge / CAPTCHA detection ─────────────────────────────────────────────

_CF_CHALLENGE_RE = re.compile(
    r"(checking your browser|attention required.*cloudflare|"
    r"please wait.*ddos|just a moment.*cloudflare)",
    re.IGNORECASE | re.DOTALL,
)
# Standard CAPTCHA widgets + Amazon robot-check + generic human-verification pages.
_CAPTCHA_RE = re.compile(
    r"(recaptcha|hcaptcha|funcaptcha|turnstile|captcha|challenge-form|data-sitekey|"
    r"robot.?check|verify.?you.?are.?human|not.?a.?robot|validateCaptcha|"
    r"press.?and.?hold|i am not a robot|verify you'?re human|"
    r"enter the characters you see|"
    # Amazon-specific — present on the robot-check redirect page.
    r"<title>\s*robot check\s*</title>|"
    r"to discuss automated access to amazon|"
    # Turkish phrases (Amazon.com.tr and other Turkish sites).
    r"insan olduğunuzu doğrula|robot olmadığınızı|"
    r"aşağıdaki karakterleri girin|karakterleri yazın|güvenlik doğrulaması)",
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

# Strong login-wall signals — phrases that indicate the page IS the login form
# rather than just having a login link in the navigation.
_LOGIN_WALL_RE = re.compile(
    r"(sign in to (?:x|twitter|continue|view|access|see)|"
    r"log in to (?:x|twitter|continue|view|access|see)|"
    r"you need to (?:be )?logged in|"
    r"please (?:log|sign) in to (?:view|access|see|continue)|"
    r"create an account to (?:see|view|access|continue)|"
    r"join .{0,30} to (?:see|view|access|continue))",
    re.IGNORECASE,
)

# Unambiguous login-wall phrases that are never present on real content pages.
# These trigger regardless of page size (no stripped-text length check).
_STRONG_LOGIN_WALL_RE = re.compile(
    r"(sign in to x to see|"
    r"sign in to x\b|"
    r"log in to x\b|"
    r"sign in to twitter\b|"
    r"log in to twitter\b|"
    r"these tweets are protected|"
    r"join x today|"
    r"x'e giriş yap|twitter'a giriş yap)",  # Turkish X/Twitter login prompts
    re.IGNORECASE,
)
# Strip inline <script> and <style> block contents so that server-side JSON
# blobs (e.g. Next.js __NEXT_DATA__) that contain translated UI strings such
# as "Sign in to X" do not cause false-positive login-wall detections.
_SCRIPT_TAG_RE = re.compile(r"<script[^>]*>.*?</script>", re.DOTALL | re.IGNORECASE)
_STYLE_TAG_RE  = re.compile(r"<style[^>]*>.*?</style>",  re.DOTALL | re.IGNORECASE)


def _strip_embedded_code(html: str) -> str:
    """Remove <script> and <style> block contents from *html*."""
    html = _SCRIPT_TAG_RE.sub("", html)
    html = _STYLE_TAG_RE.sub("", html)
    return html

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
    # Use a generous size cap so that large bot-detection pages (e.g. full-page
    # Amazon/Cloudflare blocks) are still detected, not quietly passed through.
    return bool(_BLOCK_RE.search(html)) and len(html.strip()) < 50_000


def detect_login_wall(html: str) -> bool:
    """
    Return True when the page is primarily a login/signup gate with no
    real content — as opposed to a page that merely has a login link in
    the navigation bar.

    Two tiers:
    1. Strong patterns (e.g. "Sign in to X") — unambiguous; trigger
       unconditionally regardless of page size.
    2. General patterns — require short stripped text (< 800 chars) to
       avoid false positives on content pages that mention logging in.

    Script and style block contents are stripped before matching so that
    server-side JSON blobs (e.g. Next.js __NEXT_DATA__) containing
    translated UI strings do not trigger false positives.
    """
    # Work on tag-only HTML: drop inline JS/CSS that may contain
    # translated strings like "Sign in to X" as dictionary values.
    visible = _strip_embedded_code(html)
    if _STRONG_LOGIN_WALL_RE.search(visible):
        return True
    if not _LOGIN_WALL_RE.search(visible):
        return False
    stripped = re.sub(r"<[^>]+>", "", visible).strip()
    return len(stripped) < 800


def is_empty_dom(html: str) -> bool:
    """Return True if the HTML body carries no visible content."""
    stripped = re.sub(r"<[^>]+>", "", html).strip()
    return len(stripped) < 100


def detect_app_error_page(html: str) -> bool:
    """Return True if the page reports a generic application-level error."""
    return bool(_APP_ERROR_RE.search(html))


# Pages that require JavaScript to render — returned when using a static HTTP
# client against a JS-gated site (e.g. Amazon deals, React SPAs with no SSR).
_JS_REQUIRED_RE = re.compile(
    r"(this (page |site |widget |app |application )?(requires|needs) javascript|"
    r"please enable javascript|"
    r"javascript is (disabled|required|not enabled)|"
    r"enable javascript (to |and )?(continue|interact|use|view|access)|"
    r"you need to enable javascript|"
    r"your browser (does not support|has disabled) javascript|"
    r"to discuss automated access to amazon|"
    r"<noscript>[^<]{0,200}javascript)",
    re.IGNORECASE | re.DOTALL,
)


def detect_js_required(html: str) -> bool:
    """Return True when the page is a JavaScript-required gate (no real content)."""
    return bool(_JS_REQUIRED_RE.search(html))


def is_scrape_failure(html: str, status_code: int, headers: dict[str, str] | None = None) -> bool:
    """
    Return True only when the page has no usable content or contains an
    active CAPTCHA / bot-challenge that is blocking access to the content.

    Captcha is only treated as blocking when the page also has minimal visible
    content (i.e., the captcha IS the page, not just incidentally present on a
    rich page such as in a login form or a script import like reCAPTCHA).
    Login walls, block pages, app errors, and JS-required messages are treated
    as successful scrapes — the caller received real page content.
    """
    if is_empty_dom(html):
        return True
    if detect_challenge_page(html, headers):
        return True
    # Gate captcha on visible content size.  Real captcha challenge pages have
    # very little visible text (< 1500 chars stripped).  A successfully loaded
    # rich page (forum, news site, etc.) will have far more, even if it happens
    # to include reCAPTCHA scripts or captcha form fields for its login form.
    visible = re.sub(r"<[^>]+>", "", _strip_embedded_code(html)).strip()
    if len(visible) < 1500 and detect_captcha(html):
        return True
    return False


# ── URL helpers ───────────────────────────────────────────────────────────────

def _extract_domain(url: str) -> str:
    """Return the netloc component (host[:port]) of *url*."""
    return urlparse(url).netloc


def domain_from_url(url: str) -> str:
    """Public alias for _extract_domain."""
    return _extract_domain(url)

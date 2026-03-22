"""
strategy.py — Pure function mapping a Classification to a ScrapingStrategy.

Decision logic
--------------
Private page           → blocked  (never scrape private pages)
No protection + static → static
No protection + dynamic → browser
Protected + tor only   → tor
Protected + browser only → browser
Protected + both       → hybrid   (Tor transport + undetected browser rendering)
Protected + neither    → blocked

Domain overrides
----------------
Certain well-known domains are assigned a fixed strategy regardless of what
the classifier probes return.  These override tables encode hard-won empirical
knowledge (e.g. X.com's login wall defeats any plain browser attempt).
"""
from __future__ import annotations

from typing import Optional
from urllib.parse import urlparse

from models import (
    AntiScrapingProtection,
    BrowserAvailability,
    Classification,
    ContentType,
    PublicPage,
    ScrapingStrategy,
    TorAvailability,
)


# ── Domain override table ─────────────────────────────────────────────────────
# Maps normalised hostname (no www., lowercased) → forced ScrapingStrategy.
# These take precedence over everything the classifier probes.

_DOMAIN_STRATEGY_OVERRIDES: dict[str, ScrapingStrategy] = {
    # X / Twitter — login wall defeats any plain browser attempt; Tor + browser
    # is required for access without an account.
    "x.com":          ScrapingStrategy.hybrid,
    "twitter.com":    ScrapingStrategy.hybrid,
    # Amazon — actively blocks plain HTTP requests with CAPTCHA regardless of
    # locale.  An undetected browser is required.
    "amazon.com":     ScrapingStrategy.browser,
    "amazon.co.uk":   ScrapingStrategy.browser,
    "amazon.com.tr":  ScrapingStrategy.browser,
    "amazon.de":      ScrapingStrategy.browser,
    "amazon.fr":      ScrapingStrategy.browser,
    "amazon.co.jp":   ScrapingStrategy.browser,
    "amazon.ca":      ScrapingStrategy.browser,
    "amazon.com.au":  ScrapingStrategy.browser,
    "amazon.es":      ScrapingStrategy.browser,
    "amazon.it":      ScrapingStrategy.browser,
    "amazon.nl":      ScrapingStrategy.browser,
    "amazon.pl":      ScrapingStrategy.browser,
    "amazon.se":      ScrapingStrategy.browser,
    "amazon.in":      ScrapingStrategy.browser,
    "amazon.sg":      ScrapingStrategy.browser,
    "amazon.ae":      ScrapingStrategy.browser,
    "amazon.sa":      ScrapingStrategy.browser,
}


def _normalise_host(host: str) -> str:
    """Strip 'www.' prefix and lowercase a hostname."""
    return host.lower().removeprefix("www.")


def get_domain_override(url: str) -> Optional[ScrapingStrategy]:
    """
    Return the hardcoded ScrapingStrategy for *url*'s domain,
    or None if no override is registered for it.
    """
    host = _normalise_host(urlparse(url).netloc)
    return _DOMAIN_STRATEGY_OVERRIDES.get(host)


# ── Strategy determination ────────────────────────────────────────────────────


def determine_strategy(classification: Classification) -> ScrapingStrategy:
    """
    Return the most appropriate ScrapingStrategy for *classification*.

    This is a pure function — it has no side effects and depends only on
    the classification fields passed in.
    """
    # Rule 1 — Private pages are never scraped
    if classification.is_public_page == PublicPage.no:
        return ScrapingStrategy.blocked

    tor_works = classification.tor_network_available == TorAvailability.yes
    browser_works = classification.undetected_browser_available == BrowserAvailability.yes
    is_protected = classification.antiscraping_protection == AntiScrapingProtection.protected
    is_dynamic = classification.content_type == ContentType.dynamic

    # Rule 2 — Unprotected sites
    if not is_protected:
        if is_dynamic:
            return ScrapingStrategy.browser
        return ScrapingStrategy.static

    # Rule 3 — Protected sites
    if tor_works and browser_works:
        # Both channels available → hybrid for maximum stealth
        return ScrapingStrategy.hybrid
    if tor_works:
        return ScrapingStrategy.tor
    if browser_works:
        return ScrapingStrategy.browser

    # Rule 4 — Nothing works
    return ScrapingStrategy.blocked

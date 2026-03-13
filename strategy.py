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
"""
from __future__ import annotations

from models import (
    AntiScrapingProtection,
    BrowserAvailability,
    Classification,
    ContentType,
    PublicPage,
    ScrapingStrategy,
    TorAvailability,
)


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

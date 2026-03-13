"""
models.py — Pydantic models and enumerations for the Scraper API.

Defines:
    - Enums for each classification field
    - Classification (aggregate of all classification fields)
    - URLRecord (database row representation)
    - Request / Response models for all API endpoints
"""
from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel, field_validator


# ── Enumerations ──────────────────────────────────────────────────────────────

class ContentType(str, Enum):
    static = "static"
    dynamic = "dynamic"


class AntiScrapingProtection(str, Enum):
    none = "none"
    protected = "protected"


class TorAvailability(str, Enum):
    yes = "yes"
    no = "no"


class BrowserAvailability(str, Enum):
    yes = "yes"
    no = "no"


class PublicPage(str, Enum):
    yes = "yes"
    no = "no"


class ScrapingStrategy(str, Enum):
    static = "static"
    browser = "browser"
    tor = "tor"
    hybrid = "hybrid"
    blocked = "blocked"


# ── Core domain models ────────────────────────────────────────────────────────

class Classification(BaseModel):
    """Complete classification result for a URL."""
    content_type: ContentType
    antiscraping_protection: AntiScrapingProtection
    tor_network_available: TorAvailability
    undetected_browser_available: BrowserAvailability
    is_public_page: PublicPage
    scraping_strategy: ScrapingStrategy
    classification_confidence: float   # 0.0 – 1.0


class URLRecord(BaseModel):
    """Mirrors the url_metadata table row."""
    url: str
    content_type: Optional[str] = None
    antiscraping_protection: Optional[str] = None
    tor_network_available: Optional[str] = None
    undetected_browser_available: Optional[str] = None
    is_public_page: Optional[str] = None
    scraping_strategy: Optional[str] = None
    classification_confidence: Optional[float] = None
    last_checked: Optional[datetime] = None
    last_scrape_status: Optional[str] = None
    last_success_html: Optional[str] = None

    def is_classified(self) -> bool:
        """Return True if all classification fields are populated."""
        return all([
            self.content_type,
            self.antiscraping_protection,
            self.tor_network_available,
            self.undetected_browser_available,
            self.is_public_page,
            self.scraping_strategy,
        ])

    def to_classification(self) -> Optional[Classification]:
        """Convert database record to Classification model; None if not classified."""
        if not self.is_classified():
            return None
        return Classification(
            content_type=ContentType(self.content_type),
            antiscraping_protection=AntiScrapingProtection(self.antiscraping_protection),
            tor_network_available=TorAvailability(self.tor_network_available),
            undetected_browser_available=BrowserAvailability(self.undetected_browser_available),
            is_public_page=PublicPage(self.is_public_page),
            scraping_strategy=ScrapingStrategy(self.scraping_strategy),
            classification_confidence=self.classification_confidence or 0.0,
        )


# ── API request / response models ─────────────────────────────────────────────

class ScrapeRequest(BaseModel):
    url: str
    force_reclassify: bool = False

    @field_validator("url")
    @classmethod
    def validate_url(cls, v: str) -> str:
        v = v.strip()
        if not v.startswith(("http://", "https://")):
            raise ValueError("URL must start with http:// or https://")
        return v


class ScrapeResponse(BaseModel):
    url: str
    scraping_success: bool
    message: str
    html: Optional[str] = None
    classification: Optional[Classification] = None
    cached: bool = False
    strategy_used: Optional[str] = None


class ClassifyRequest(BaseModel):
    url: str
    force: bool = False

    @field_validator("url")
    @classmethod
    def validate_url(cls, v: str) -> str:
        v = v.strip()
        if not v.startswith(("http://", "https://")):
            raise ValueError("URL must start with http:// or https://")
        return v


class ClassifyResponse(BaseModel):
    url: str
    classification: Classification
    from_cache: bool


class StatusResponse(BaseModel):
    url: str
    record: Optional[URLRecord] = None
    found: bool


class HealthResponse(BaseModel):
    status: str
    database: str
    tor_reachable: bool

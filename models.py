"""
models.py — Pydantic models and enumerations for the Scraper API.

Defines:
    - ScrapingStrategy enum
    - URLRecord (database row representation)
    - Request / Response models for all API endpoints
"""
from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel, field_validator


# ── Enumerations ──────────────────────────────────────────────────────────────

class ScrapingStrategy(str, Enum):
    browser = "browser"
    tor     = "tor"
    static  = "static"   # available via force_strategy only
    hybrid  = "hybrid"   # legacy DB value, treated as browser
    blocked = "blocked"  # sentinel: do not scrape


# ── Core domain models ────────────────────────────────────────────────────────


class URLRecord(BaseModel):
    """Mirrors the url_metadata table row."""
    url: str
    scraping_strategy: Optional[str] = None
    last_checked: Optional[datetime] = None
    last_scrape_status: Optional[str] = None


# ── API request / response models ─────────────────────────────────────────────

class ScrapeRequest(BaseModel):
    url: str
    force_strategy: Optional[str] = None   # override strategy: browser|tor|static

    @field_validator("url")
    @classmethod
    def validate_url(cls, v: str) -> str:
        v = v.strip()
        if not v.startswith(("http://", "https://")):
            raise ValueError("URL must start with http:// or https://")
        return v

    @field_validator("force_strategy")
    @classmethod
    def validate_force_strategy(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        allowed = {"browser", "tor", "static"}
        if v not in allowed:
            raise ValueError(f"force_strategy must be one of: {', '.join(sorted(allowed))}")
        return v


class ScrapeResponse(BaseModel):
    url: str
    scraping_success: bool
    message: str
    html: Optional[str] = None
    strategy_used: Optional[str] = None


class StatusResponse(BaseModel):
    url: str
    record: Optional[URLRecord] = None
    found: bool


class HealthResponse(BaseModel):
    status: str
    database: str
    tor_reachable: bool
    tor_socks_port: Optional[int] = None       # which SOCKS port responded
    tor_control_reachable: bool = False         # control port open?
    tor_circuit_ok: Optional[bool] = None       # test HTTP request through Tor succeeded?


# ── Feedback models ────────────────────────────────────────────────────────────

class FeedbackCreate(BaseModel):
    """Payload for submitting a comment about a tested URL."""
    url: str
    comment: str
    strategy_used: Optional[str] = None    # strategy that was active when comment was written
    scrape_success: Optional[bool] = None  # whether the scrape succeeded at that time


class FeedbackItem(BaseModel):
    """Single feedback row returned from the API."""
    id: int
    url: str
    comment: str
    strategy_used: Optional[str] = None
    scrape_success: Optional[bool] = None
    created_at: str


class FeedbackListResponse(BaseModel):
    items: list[FeedbackItem]
    total: int

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
    static     = "static"      # 1st: plain httpx GET
    browser    = "browser"     # 2nd: Chrome via CDP
    tor        = "tor"         # 3rd: httpx over Tor SOCKS5
    scrape_do  = "scrape_do"   # 4th: Scrape.do proxy API
    zyte       = "zyte"        # 5th: Zyte Extract API
    hybrid     = "hybrid"      # legacy DB value, treated as browser
    blocked    = "blocked"     # sentinel: do not scrape


# ── Core domain models ────────────────────────────────────────────────────────


class URLRecord(BaseModel):
    """Mirrors the url_metadata table row."""
    url: str
    scraping_strategy: Optional[str] = None
    last_checked: Optional[datetime] = None
    last_scrape_status: Optional[str] = None
    last_provider: Optional[str] = None
    last_cost: Optional[float] = None
    last_error_reason: Optional[str] = None


# ── API request / response models ─────────────────────────────────────────────

class ScrapeRequest(BaseModel):
    url: str
    force_strategy: Optional[str] = None   # override strategy: static|browser|tor|scrape_do|zyte

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
        allowed = {"static", "browser", "tor", "scrape_do", "zyte"}
        if v not in allowed:
            raise ValueError(f"force_strategy must be one of: {', '.join(sorted(allowed))}")
        return v


class ScrapeResponse(BaseModel):
    url: str
    scraping_success: bool
    message: str
    html: Optional[str] = None
    strategy_used: Optional[str] = None        # mirrors `provider`; kept for backward compat
    provider: Optional[str] = None             # which provider produced the result (or None)
    status: str = "failed"                     # success | blocked | timeout | failed
    cost_score: float = 0.0                    # USD-equivalent cost charged for this scrape
    error_reason: Optional[str] = None         # last error message, when scraping_success is False
    credits_remaining: Optional[float] = None


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


class CreditsResponse(BaseModel):
    balance: float
    granted: float
    used: float


# ── URL allow/block list models ─────────────────────────────────────────────

class URLListResponse(BaseModel):
    allowlist: list[str]
    blocklist: list[str]


class URLListMutation(BaseModel):
    domain: str


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

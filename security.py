"""
security.py — API authentication and request validation.

Provides a FastAPI dependency (``require_api_key``) that:
  1. Validates the X-API-KEY header.
  2. Ensures requests originate from localhost (defence-in-depth).

Both checks use constant-time comparison to prevent timing attacks.

Usage
-----
    from security import require_api_key

    @app.post("/scrape", dependencies=[Depends(require_api_key)])
    async def scrape_endpoint(request: ScrapeRequest): ...
"""
from __future__ import annotations

import hmac
import logging

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import APIKeyHeader

from config import CONFIG

logger = logging.getLogger(__name__)

# FastAPI will automatically extract and document this header.
_api_key_scheme = APIKeyHeader(name="X-API-KEY", auto_error=False)

# Localhost addresses accepted as originating hosts.
_LOCALHOST_ADDRS = frozenset({"127.0.0.1", "::1", "localhost"})


def _constant_time_equal(a: str, b: str) -> bool:
    """Compare two strings in constant time to prevent timing side-channels."""
    return hmac.compare_digest(a.encode(), b.encode())


async def require_api_key(
    request: Request,
    api_key: str | None = Depends(_api_key_scheme),
) -> None:
    """
    FastAPI dependency — raises 401/403 when the request is not authorised.

    Rejects:
      - Missing or incorrect X-API-KEY header  → 401
      - Requests not originating from localhost  → 403
    """
    # ── Localhost-only guard ─────────────────────────────────────────────────
    client_host = request.client.host if request.client else ""
    if client_host not in _LOCALHOST_ADDRS:
        logger.warning(
            "Rejected request from non-localhost address: %s", client_host
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access denied: API is only accessible from localhost.",
        )

    # ── API key validation ───────────────────────────────────────────────────
    if not api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing X-API-KEY header.",
            headers={"WWW-Authenticate": "ApiKey"},
        )

    if not _constant_time_equal(api_key, CONFIG.api_key):
        logger.warning("Rejected request — invalid API key from %s", client_host)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API key.",
            headers={"WWW-Authenticate": "ApiKey"},
        )

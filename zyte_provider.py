"""
zyte_provider.py — Zyte fallback provider.

Fifth and final stage of the scraping waterfall.  Calls Zyte's Extract API,
which fetches *url* from its own infrastructure (with anti-bot handling) and
returns the raw HTTP response body, base64-encoded.

Public API
----------
scrape_with_zyte(url) -> str
"""
from __future__ import annotations

import base64
import json
import logging

import httpx

from config import CONFIG
from utils import read_text_capped

logger = logging.getLogger(__name__)


async def scrape_with_zyte(url: str) -> str:
    """Fetch *url* via the Zyte Extract API. Raises on failure or missing config."""
    if not CONFIG.zyte_api_key:
        raise RuntimeError("ZYTE_API_KEY not configured.")

    async with httpx.AsyncClient(timeout=CONFIG.request_timeout, verify=False) as client:
        async with client.stream(
            "POST",
            CONFIG.zyte_base_url,
            auth=(CONFIG.zyte_api_key, ""),
            json={"url": url, "httpResponseBody": True},
        ) as response:
            response.raise_for_status()
            raw = await read_text_capped(response, CONFIG.max_response_size_bytes)

    payload = json.loads(raw)
    body_b64 = payload.get("httpResponseBody")
    if not body_b64:
        raise RuntimeError("Zyte response had no httpResponseBody.")
    return base64.b64decode(body_b64).decode("utf-8", errors="replace")

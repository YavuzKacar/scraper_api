"""
scrape_do_provider.py — Scrape.do fallback provider.

Fourth stage of the scraping waterfall (static -> browser -> tor ->
scrape_do -> zyte).  Calls the Scrape.do proxy API, which fetches *url* from
its own infrastructure and returns the rendered HTML.

Public API
----------
scrape_with_scrape_do(url) -> str
"""
from __future__ import annotations

import logging
from urllib.parse import quote

import httpx

from config import CONFIG
from utils import read_text_capped

logger = logging.getLogger(__name__)


async def scrape_with_scrape_do(url: str) -> str:
    """Fetch *url* via the Scrape.do API. Raises on failure or missing config."""
    if not CONFIG.scrape_do_api_key:
        raise RuntimeError("SCRAPE_DO_API_KEY not configured.")

    target = f"{CONFIG.scrape_do_base_url.rstrip('/')}/?token={CONFIG.scrape_do_api_key}&url={quote(url, safe='')}"

    async with httpx.AsyncClient(timeout=CONFIG.request_timeout, verify=False) as client:
        async with client.stream("GET", target) as response:
            response.raise_for_status()
            return await read_text_capped(response, CONFIG.max_response_size_bytes)

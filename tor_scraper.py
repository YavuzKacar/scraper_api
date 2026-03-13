"""
tor_scraper.py — Tor-based scraping implementations.

Adapted from BUSS-90 (etsy_get_listing_data_tor.py) and extended with:
  - httpx SOCKS5 lightweight path (fast, no browser overhead)
  - Selenium Firefox SOCKS5 path (full JS rendering via Tor)
  - Identity rotation via Stem NEWNYM signal

Public API
----------
scrape_with_tor(url, profile)           → str   (async, httpx path)
scrape_with_tor_browser(url, profile)   → str   (sync, Selenium path; run in executor)
rotate_tor_identity()                   → bool  (async)
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import socket
from typing import Optional

import httpx

from config import CONFIG
from fingerprint import FingerprintProfile, build_http_headers
from utils import human_delay, human_delay_sync

logger = logging.getLogger(__name__)


# ── Tor connectivity helpers ──────────────────────────────────────────────────

def _find_tor_port() -> Optional[int]:
    """
    Return the first open Tor SOCKS port from [9050, 9150],
    or None if Tor is not reachable.
    """
    for port in [CONFIG.tor_socks_port, 9050, 9150]:
        with contextlib.suppress(OSError):
            with socket.create_connection(
                (CONFIG.tor_socks_host, port), timeout=2.0
            ):
                return port
    return None


# ── Lightweight path: httpx through Tor SOCKS5 ───────────────────────────────

async def scrape_with_tor(url: str, profile: FingerprintProfile) -> str:
    """
    Fetch *url* through the Tor SOCKS5 proxy using httpx.

    This is the preferred Tor path: no browser process, low resource usage.
    Falls back gracefully when Tor is not running.

    Raises RuntimeError if Tor is unreachable or the request fails.
    """
    tor_port = _find_tor_port()
    if tor_port is None:
        raise RuntimeError("Tor SOCKS proxy is not reachable on any known port.")

    proxy_url = f"socks5://{CONFIG.tor_socks_host}:{tor_port}"
    headers = build_http_headers(profile, url)

    await human_delay()

    async with httpx.AsyncClient(
        proxy=proxy_url,
        timeout=CONFIG.request_timeout,
        follow_redirects=True,
        verify=False,
        limits=httpx.Limits(max_connections=3, max_keepalive_connections=2),
    ) as client:
        response = await client.get(url, headers=headers)
        response.raise_for_status()
        return response.text


# ── Full-browser path: Selenium Firefox through Tor SOCKS5 ───────────────────
# Adapted from BUSS-90 / etsy_get_listing_data_tor.py

def _build_tor_firefox_driver(
    tor_socks_port: int,
    headless: bool = True,
    geckodriver_path: Optional[str] = None,
):
    """
    Build a Selenium Firefox WebDriver that tunnels all traffic via Tor.

    Returns a ``selenium.webdriver.Firefox`` instance.
    Raises ImportError if selenium is not installed.
    """
    from selenium import webdriver
    from selenium.webdriver.firefox.options import Options as FirefoxOptions
    from selenium.webdriver.firefox.service import Service as FirefoxService

    options = FirefoxOptions()
    if headless:
        options.add_argument("-headless")

    # Configure SOCKS5 proxy pointing at Tor
    profile = webdriver.FirefoxProfile()
    profile.set_preference("network.proxy.type", 1)
    profile.set_preference("network.proxy.socks", CONFIG.tor_socks_host)
    profile.set_preference("network.proxy.socks_port", tor_socks_port)
    profile.set_preference("network.proxy.socks_version", 5)
    profile.set_preference("network.proxy.socks_remote_dns", True)   # DNS via Tor
    profile.set_preference("places.history.enabled", False)
    profile.set_preference("privacy.trackingprotection.enabled", True)
    profile.update_preferences()
    options.profile = profile

    service = (
        FirefoxService(executable_path=geckodriver_path)
        if geckodriver_path
        else FirefoxService()
    )
    return webdriver.Firefox(options=options, service=service)


def scrape_with_tor_browser(
    url: str,
    fingerprint: FingerprintProfile,
    headless: bool = True,
    geckodriver_path: Optional[str] = None,
) -> str:
    """
    Fetch *url* using a Firefox browser that tunnels through Tor.

    Intended to run in a thread-pool executor (blocking).
    Returns the fully rendered page source.

    Raises RuntimeError if Tor or geckodriver is not available.
    """
    tor_port = _find_tor_port()
    if tor_port is None:
        raise RuntimeError("Tor SOCKS proxy is not reachable.")

    driver = _build_tor_firefox_driver(
        tor_socks_port=tor_port,
        headless=headless,
        geckodriver_path=geckodriver_path,
    )

    try:
        human_delay_sync(1.5, 3.0)
        driver.get(url)
        human_delay_sync(2.0, 4.0)

        # Scroll to trigger lazy-loaded content
        driver.execute_script("window.scrollTo(0, document.body.scrollHeight * 0.3);")
        human_delay_sync(1.0, 2.5)
        driver.execute_script("window.scrollTo(0, document.body.scrollHeight * 0.7);")
        human_delay_sync(0.8, 2.0)

        return driver.page_source or ""
    finally:
        with contextlib.suppress(Exception):
            driver.quit()


# ── Identity rotation ─────────────────────────────────────────────────────────

async def rotate_tor_identity() -> bool:
    """
    Request a new Tor circuit via the control port using Stem.

    Returns True on success, False if Stem is unavailable or rotation fails.
    """
    try:
        from stem import Signal
        from stem.control import Controller
    except ImportError:
        logger.warning("'stem' is not installed — Tor identity rotation unavailable.")
        return False

    def _send_newnym() -> bool:
        try:
            with Controller.from_port(
                address=CONFIG.tor_socks_host,
                port=CONFIG.tor_control_port,
            ) as ctrl:
                if CONFIG.tor_control_password:
                    ctrl.authenticate(password=CONFIG.tor_control_password)
                else:
                    ctrl.authenticate()
                ctrl.signal(Signal.NEWNYM)
                logger.info("Tor: NEWNYM signal sent — new circuit established.")
                return True
        except Exception as exc:
            logger.warning("Tor identity rotation failed: %s", exc)
            return False

    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _send_newnym)

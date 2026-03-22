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
import os
import socket
import subprocess
import time
from pathlib import Path
from typing import Optional

import httpx

from config import CONFIG
from fingerprint import FingerprintProfile, build_http_headers
from utils import human_delay, human_delay_sync

logger = logging.getLogger(__name__)


# ── Tor connectivity helpers ──────────────────────────────────────────────────

# Subprocess handle for a tor.exe we launched ourselves.
_tor_process: Optional[subprocess.Popen] = None  # type: ignore[type-arg]


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


def _ensure_tor_running() -> int:
    """
    Return an open Tor SOCKS port.  If no port is reachable, launch
    ``CONFIG.tor_exe_path`` (tor.exe) as a background subprocess and
    wait up to 30 s for it to become ready.

    Raises RuntimeError if Tor cannot be started or does not respond in time.
    """
    global _tor_process

    # Fast path — Tor is already up (e.g. Tor Browser is open).
    port = _find_tor_port()
    if port is not None:
        return port

    exe = CONFIG.tor_exe_path
    if not exe or not os.path.isfile(exe):
        raise RuntimeError(
            f"Tor is not running and tor.exe not found at: {exe!r}.  "
            "Open Tor Browser or set the TOR_EXE_PATH environment variable."
        )

    # Resolve companion paths relative to the exe location.
    tor_dir = Path(exe).parent
    data_dir = tor_dir.parent / "Data" / "Tor"
    geoip = data_dir / "geoip"
    geoip6 = data_dir / "geoip6"

    cmd = [
        str(exe),
        "--SocksPort",    "9150",
        "--ControlPort",  "9151",
        "--DisableNetwork", "0",       # override the browser torrc default
        "--DataDirectory", str(data_dir),
    ]
    if geoip.exists():
        cmd += ["--GeoIPFile", str(geoip)]
    if geoip6.exists():
        cmd += ["--GeoIPv6File", str(geoip6)]

    logger.info("Tor not running — launching: %s", exe)
    _tor_process = subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        # Keep the process alive independently of this process on Windows.
        creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
    )

    # Poll for up to 30 seconds.
    for _ in range(30):
        time.sleep(1)
        port = _find_tor_port()
        if port is not None:
            logger.info("Tor started — SOCKS port %d is open.", port)
            return port

    _tor_process.kill()
    _tor_process = None
    raise RuntimeError("tor.exe was launched but did not open a SOCKS port within 30 s.")


# ── Lightweight path: httpx through Tor SOCKS5 ───────────────────────────────

async def scrape_with_tor(url: str, profile: FingerprintProfile) -> str:
    """
    Fetch *url* through the Tor SOCKS5 proxy using httpx.

    This is the preferred Tor path: no browser process, low resource usage.
    Falls back gracefully when Tor is not running.

    Raises RuntimeError if Tor is unreachable or the request fails.
    """
    loop = asyncio.get_running_loop()
    try:
        tor_port = await loop.run_in_executor(None, _ensure_tor_running)
    except RuntimeError:
        raise

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
):
    """
    Build a Selenium Firefox WebDriver that tunnels all traffic via Tor.

    Uses the Firefox binary from CONFIG.firefox_binary_path (defaults to
    the bundled Tor Browser Firefox).  Relies on Selenium 4's built-in
    selenium-manager to download geckodriver automatically if it is not
    already on PATH.

    Returns a ``selenium.webdriver.Firefox`` instance.
    Raises ImportError if selenium is not installed.
    """
    import os as _os
    from selenium import webdriver
    from selenium.webdriver.firefox.options import Options as FirefoxOptions
    from selenium.webdriver.firefox.service import Service as FirefoxService

    options = FirefoxOptions()
    if headless:
        options.add_argument("-headless")

    # Point Selenium at the Tor Browser's Firefox binary.
    # Falls back gracefully if the path doesn't exist (system Firefox).
    ff_bin = CONFIG.firefox_binary_path
    if ff_bin and _os.path.isfile(ff_bin):
        options.binary_location = ff_bin
        logger.debug("Using Firefox binary: %s", ff_bin)
    else:
        logger.warning(
            "Firefox binary not found at %r — using system Firefox. "
            "Set FIREFOX_BINARY_PATH to point to Tor Browser's firefox.exe.",
            ff_bin,
        )

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

    # selenium-manager (Selenium 4.6+) will auto-download geckodriver
    # if it is not already available on PATH.
    return webdriver.Firefox(options=options, service=FirefoxService())


def scrape_with_tor_browser(
    url: str,
    fingerprint: FingerprintProfile,
    headless: bool = True,
    geckodriver_path: Optional[str] = None,  # kept for API compat; ignored (selenium-manager handles it)
) -> str:
    """
    Fetch *url* using a Firefox browser that tunnels through Tor.

    Intended to run in a thread-pool executor (blocking).
    Returns the fully rendered page source.

    Raises RuntimeError if Tor or Firefox/geckodriver is not available.
    """
    tor_port = _ensure_tor_running()

    driver = _build_tor_firefox_driver(
        tor_socks_port=tor_port,
        headless=headless,
    )

    try:
        # Prevent indefinite hang on pages that never finish loading.
        driver.set_page_load_timeout(30)
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

    Tries CONFIG.tor_control_port first, then 9151 (Tor Browser) and
    9051 (standalone daemon).  Handles unauthenticated and cookie-based
    authentication automatically.

    Returns True on success, False if Stem is unavailable or rotation fails.
    """
    def _send_newnym() -> bool:
        try:
            from stem import Signal
            from stem.control import Controller

            # Try the configured control port first, then the Tor Browser port (9151)
            # and standalone daemon port (9051).
            control_ports = list(dict.fromkeys([
                CONFIG.tor_control_port, 9151, 9051
            ]))
            last_exc = None
            for ctrl_port in control_ports:
                try:
                    with Controller.from_port(
                        address=CONFIG.tor_socks_host,
                        port=ctrl_port,
                    ) as ctrl:
                        if CONFIG.tor_control_password:
                            ctrl.authenticate(password=CONFIG.tor_control_password)
                        else:
                            # Try unauthenticated first, then cookie auth
                            # (Tor Browser uses cookie-based authentication).
                            try:
                                ctrl.authenticate()
                            except Exception:
                                ctrl.authenticate(chroot_path="")
                        ctrl.signal(Signal.NEWNYM)
                        logger.info(
                            "Tor: NEWNYM signal sent via control port %d.",
                            ctrl_port,
                        )
                        return True
                except Exception as exc:
                    last_exc = exc
                    continue
            logger.warning("Tor identity rotation failed on all control ports: %s", last_exc)
            return False
        except ImportError:
            logger.warning("'stem' is not installed — Tor identity rotation unavailable.")
            return False

    loop = asyncio.get_running_loop()
    success = await loop.run_in_executor(None, _send_newnym)
    if success:
        # Tor requires ~10 seconds to build a new circuit after NEWNYM.
        # Proceeding immediately would still use the old circuit.
        await asyncio.sleep(10)
    return success

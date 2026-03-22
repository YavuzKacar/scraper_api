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
    Return an open Tor SOCKS port.  Starts Tor if it is not already running.

    Strategy (in order):
    1. Fast path — return immediately if a SOCKS port is already reachable.
    2. Launch the full Tor Browser (firefox.exe with the TorBrowser profile).
       The bundled Tor Launcher extension starts tor.exe and connects to the
       Tor network automatically, honouring the "Connect automatically"
       preference, any configured bridges, and pluggable transports.
    3. Fallback — start tor.exe directly with the TorBrowser torrc so that
       relative paths (pluggable transports) resolve correctly.

    Raises RuntimeError if Tor cannot be started within the timeout.
    """
    # 1. Fast path
    port = _find_tor_port()
    if port is not None:
        return port

    # 2. Preferred: launch full Tor Browser
    ff_bin = CONFIG.firefox_binary_path
    if ff_bin and os.path.isfile(ff_bin):
        return _launch_tor_browser(Path(ff_bin))

    # 3. Fallback: launch tor.exe directly
    exe = CONFIG.tor_exe_path
    if exe and os.path.isfile(exe):
        return _launch_tor_daemon(Path(exe))

    raise RuntimeError(
        "Tor is not running and neither the Tor Browser binary nor tor.exe "
        "could be found.  Set FIREFOX_BINARY_PATH or TOR_EXE_PATH."
    )


def _launch_tor_browser(ff_bin: Path) -> int:
    """
    Start the full Tor Browser application.

    Tor Browser's built-in Tor Launcher extension starts tor.exe and bootstraps
    the circuit automatically when 'Connect automatically' is enabled.  Any
    bridge or pluggable-transport configuration stored in the profile is
    respected without any extra work on our side.

    Clears a stale Firefox profile lock (left over from a crashed session)
    before launching so the new instance can acquire the profile.

    Returns the SOCKS port once it becomes reachable (polls up to 60 s).
    """
    global _tor_process

    browser_dir = ff_bin.parent
    profile_dir = browser_dir / "TorBrowser" / "Data" / "Browser" / "profile.default"

    # Remove any stale lock files from a previous crash so Firefox can start.
    for lock_name in ("lock", ".parentlock"):
        lock_file = profile_dir / lock_name
        if lock_file.exists():
            with contextlib.suppress(OSError):
                lock_file.unlink()
                logger.debug("Removed stale Tor Browser profile lock: %s", lock_file)

    cmd = [str(ff_bin), "-profile", str(profile_dir), "-no-remote"]
    logger.info("Starting Tor Browser: %s", str(ff_bin))
    _tor_process = subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        cwd=str(browser_dir),
        creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
    )

    # Tor Browser needs 20–60 s to bootstrap a circuit and open the SOCKS port.
    for i in range(90):
        time.sleep(1)
        port = _find_tor_port()
        if port is not None:
            logger.info(
                "Tor Browser SOCKS port %d open after %ds — waiting for circuit…",
                port, i + 1,
            )
            # SOCKS port is open, but Tor may still be bootstrapping a circuit.
            # Verify by making a real request through the proxy.
            if _wait_for_circuit(port, timeout=60):
                logger.info("Tor circuit ready — fully connected.")
                return port
            # Circuit never came up even though SOCKS was open.
            break

    if _tor_process is not None:
        _tor_process.kill()
        _tor_process = None
    raise RuntimeError(
        "Tor Browser was launched but could not establish a circuit within 90 s.  "
        "Make sure 'Connect automatically' is enabled in Tor Browser settings "
        "(Settings → Connection → Connect automatically)."
    )


def _launch_tor_daemon(exe: Path) -> int:
    """
    Start tor.exe directly as a background daemon using the TorBrowser torrc.

    Sets the working directory to the Browser folder so that relative paths in
    torrc-defaults (pluggable-transport binaries) resolve correctly, and
    overrides DisableNetwork to 0 so Tor actually connects.

    Returns the SOCKS port once it becomes reachable (polls up to 30 s).
    """
    global _tor_process

    tor_dir = exe.parent               # .../TorBrowser/Tor/
    browser_dir = tor_dir.parent.parent  # .../Browser/
    data_dir = tor_dir.parent / "Data" / "Tor"
    torrc = data_dir / "torrc"
    torrc_defaults = data_dir / "torrc-defaults"

    cmd = [str(exe)]
    if torrc.exists():
        cmd += ["-f", str(torrc)]
    if torrc_defaults.exists():
        cmd += ["--defaults-torrc", str(torrc_defaults)]
    # Override the torrc's DisableNetwork 1 so Tor actually connects.
    cmd += ["--DisableNetwork", "0", "--SocksPort", "9150", "--ControlPort", "9151"]

    logger.info("Starting tor.exe daemon: %s", str(exe))
    _tor_process = subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        cwd=str(browser_dir),   # required: torrc-defaults uses relative PTP paths
        creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
    )

    for i in range(30):
        time.sleep(1)
        port = _find_tor_port()
        if port is not None:
            logger.info(
                "tor.exe SOCKS port %d open after %ds — waiting for circuit…",
                port, i + 1,
            )
            if _wait_for_circuit(port, timeout=60):
                logger.info("Tor daemon circuit ready — fully connected.")
                return port
            break

    if _tor_process is not None:
        _tor_process.kill()
        _tor_process = None
    raise RuntimeError("tor.exe was launched but could not establish a circuit within 90 s.")


# ── Circuit readiness check ───────────────────────────────────────────────────

def _wait_for_circuit(tor_port: int, timeout: int = 60) -> bool:
    """
    Block until a real HTTP request succeeds through the Tor SOCKS proxy,
    proving that a circuit is fully established.

    Polls every 3 seconds for up to *timeout* seconds.
    Returns True once the circuit is confirmed working, False on timeout.
    """
    import urllib.request
    import urllib.error
    import socks  # PySocks — already a dependency of httpx[socks]

    proxy_host = CONFIG.tor_socks_host
    deadline = time.monotonic() + timeout

    while time.monotonic() < deadline:
        try:
            # Use a lightweight endpoint that returns very little data.
            s = socks.socksocket()
            s.set_proxy(socks.SOCKS5, proxy_host, tor_port)
            s.settimeout(10)
            s.connect(("check.torproject.org", 80))
            s.sendall(b"HEAD / HTTP/1.0\r\nHost: check.torproject.org\r\n\r\n")
            resp = s.recv(128)
            s.close()
            if b"HTTP/" in resp:
                return True
        except Exception as exc:
            logger.debug("Circuit not ready yet: %s", exc)
        time.sleep(3)
    return False


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
    options.set_preference("network.proxy.type", 1)
    options.set_preference("network.proxy.socks", CONFIG.tor_socks_host)
    options.set_preference("network.proxy.socks_port", tor_socks_port)
    options.set_preference("network.proxy.socks_version", 5)
    options.set_preference("network.proxy.socks_remote_dns", True)   # DNS via Tor
    options.set_preference("places.history.enabled", False)
    options.set_preference("privacy.trackingprotection.enabled", True)
    # Force English locale — prevents sites from serving Turkish pages
    options.set_preference("intl.accept_languages", "en-US, en")
    options.set_preference("general.useragent.locale", "en-US")
    # Suppress Firefox's default browser / data-reporting prompts
    options.set_preference("datareporting.policy.dataSubmissionEnabled", False)
    options.set_preference("toolkit.telemetry.reportingpolicy.firstRun", False)

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
        # Tor is slow — give pages more time to load through the circuit.
        driver.set_page_load_timeout(60)
        human_delay_sync(1.5, 3.0)
        driver.get(url)
        human_delay_sync(3.0, 6.0)

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

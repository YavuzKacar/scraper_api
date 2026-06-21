"""
tor_scraper.py — Tor-based scraping implementations.

Public API
----------
scrape_with_tor(url, profile)   -> str   (async, httpx path)
rotate_tor_identity()           -> bool  (async, cheap NEWNYM-only rotation)
recover_tor_circuit()           -> bool  (async, robust: health-checks the
                                           circuit and kills + relaunches Tor
                                           from scratch if it's actually dead)
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
from url_security import httpx_redirect_validator_hook
from utils import human_delay, read_text_capped

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

def _check_circuit_once(tor_port: int, timeout: float = 10.0) -> bool:
    """
    Single attempt: does a real HTTP request succeed through the Tor SOCKS
    proxy right now? Used both by the startup poll loop (_wait_for_circuit)
    and by recover_tor_circuit() for a cheap one-shot health check.
    """
    import socks  # PySocks — already a dependency of httpx[socks]

    try:
        s = socks.socksocket()
        s.set_proxy(socks.SOCKS5, CONFIG.tor_socks_host, tor_port)
        s.settimeout(timeout)
        s.connect(("check.torproject.org", 80))
        s.sendall(b"HEAD / HTTP/1.0\r\nHost: check.torproject.org\r\n\r\n")
        resp = s.recv(128)
        s.close()
        return b"HTTP/" in resp
    except Exception as exc:
        logger.debug("Circuit check failed: %s", exc)
        return False


def _wait_for_circuit(tor_port: int, timeout: int = 60) -> bool:
    """
    Block until a real HTTP request succeeds through the Tor SOCKS proxy,
    proving that a circuit is fully established.

    Polls every 3 seconds for up to *timeout* seconds.
    Returns True once the circuit is confirmed working, False on timeout.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _check_circuit_once(tor_port, timeout=10.0):
            return True
        time.sleep(3)
    return False


# ── Crash recovery: kill whatever owns a Tor port ─────────────────────────────

def _kill_process_on_port(port: int) -> None:
    """
    Forcefully terminate whatever process is listening on *port* (Windows).

    Used for recovery when the circuit is confirmed dead: the listening
    process may belong to a Tor Browser instance this server process has no
    subprocess handle for (e.g. left over from a previous run that this
    process never spawned), so we find it by port ownership via `netstat`
    instead of relying on the in-memory _tor_process handle.
    """
    try:
        out = subprocess.run(
            ["netstat", "-ano"], capture_output=True, text=True, timeout=10
        ).stdout
    except Exception as exc:
        logger.debug("netstat failed while hunting for port %d: %s", port, exc)
        return

    pids: set[str] = set()
    for line in out.splitlines():
        parts = line.split()
        if len(parts) < 5 or parts[0] != "TCP" or parts[3] != "LISTENING":
            continue
        if parts[1].endswith(f":{port}") and parts[-1].isdigit():
            pids.add(parts[-1])

    for pid in pids:
        logger.warning("Killing stale process on Tor port %d (PID %s).", port, pid)
        with contextlib.suppress(Exception):
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", pid],
                capture_output=True, timeout=10,
            )


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
        event_hooks={"request": [httpx_redirect_validator_hook]},
    ) as client:
        async with client.stream("GET", url, headers=headers) as response:
            response.raise_for_status()
            return await read_text_capped(response, CONFIG.max_response_size_bytes)


# ── Full-browser path: Chrome through Tor SOCKS5 ─────────────────────────────

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
                    # Don't use the context manager: on Windows, stem raises
                    # WinError 10038 ("not a socket") when closing the control
                    # socket inside __exit__, which would swallow our return True.
                    # Instead, close manually and suppress that benign error.
                    ctrl = Controller.from_port(
                        address=CONFIG.tor_socks_host,
                        port=ctrl_port,
                    )
                    try:
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
                    finally:
                        with contextlib.suppress(Exception):
                            ctrl.close()
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


async def recover_tor_circuit() -> bool:
    """
    Robust recovery for a possibly-broken Tor circuit -- call this between
    retries instead of rotate_tor_identity() alone.

    1. Single-shot health check on whatever port is currently reachable.
    2. Healthy -> just rotate identity (NEWNYM), cheap, gets a fresh exit
       node without a full restart.
    3. Unhealthy or unreachable -> kill whatever is bound to every known
       Tor port (covers a process this server spawned AND one left over
       from a previous run we have no handle for), then relaunch Tor
       Browser from scratch via _ensure_tor_running(), which blocks until
       a fresh circuit is verified working (or raises).

    Returns True once a healthy circuit is confirmed, False if recovery
    failed entirely -- the caller should treat the tor stage as failed and
    fall through to the next provider.
    """
    loop = asyncio.get_running_loop()

    def _is_healthy() -> bool:
        port = _find_tor_port()
        return port is not None and _check_circuit_once(port, timeout=8.0)

    if await loop.run_in_executor(None, _is_healthy):
        return await rotate_tor_identity()

    logger.warning("Tor circuit appears broken -- killing and relaunching Tor.")

    def _kill_and_relaunch() -> bool:
        global _tor_process
        for p in dict.fromkeys([CONFIG.tor_socks_port, 9150, 9050]):
            _kill_process_on_port(p)
        if _tor_process is not None:
            with contextlib.suppress(Exception):
                _tor_process.kill()
            _tor_process = None
        time.sleep(2)  # let Windows release the port before rebinding
        try:
            _ensure_tor_running()  # raises RuntimeError on failure; success implies a verified circuit
            return True
        except RuntimeError as exc:
            logger.error("Tor relaunch failed during recovery: %s", exc)
            return False

    return await loop.run_in_executor(None, _kill_and_relaunch)


async def ensure_tor_started_async() -> None:
    """
    Pre-start Tor Browser at server startup so the first scrape request is instant.
    Safe to call multiple times; a no-op when Tor is already running.
    Logs a warning instead of raising if Tor cannot be started.
    """
    loop = asyncio.get_running_loop()
    try:
        await loop.run_in_executor(None, _ensure_tor_running)
        logger.info("Tor Browser pre-start complete.")
    except RuntimeError as exc:
        logger.warning("Tor Browser pre-start failed (will retry on first request): %s", exc)

"""
browser_scraper.py â€” Chrome-based parallel scraper using CDP tab pooling.

Architecture
------------
A single undetected-chromedriver Chrome process lives for the server lifetime.
Parallel scrape requests each get a dedicated, short-lived browser tab managed
through the Chrome DevTools Protocol (CDP) directly â€” bypassing Selenium's
sequential WebDriver command queue.

Up to MAX_TABS requests are handled simultaneously; additional callers wait in
an asyncio semaphore queue.  Chrome shuts down automatically IDLE_TIMEOUT
seconds after the last request completes and restarts transparently on the
next request.

Public API
----------
scrape_with_browser_async(url, profile, headless=False) -> str
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import random
import re
import shutil

import httpx

from fingerprint import FingerprintProfile, build_browser_js_overrides

logger = logging.getLogger(__name__)

# â”€â”€ Constants â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

_UC_DEFAULT_DATA_DIR = os.path.join(os.environ.get("APPDATA", ""), "undetected_chromedriver")

MAX_TABS: int = 10          # max simultaneous open tabs
IDLE_TIMEOUT: float = 60.0  # seconds before Chrome shuts down when idle

_CF_CHALLENGE_MARKERS: tuple[str, ...] = (
    "Just a moment",
    "Checking your browser",
    "challenge-error-text",
    "_cf_chl_opt",
    "cf-spinner",
)

# â”€â”€ UC cache helpers â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

def _clear_uc_cache() -> None:
    if os.path.isdir(_UC_DEFAULT_DATA_DIR):
        with contextlib.suppress(Exception):
            shutil.rmtree(_UC_DEFAULT_DATA_DIR)
            logger.info("Cleared uc cache at %s.", _UC_DEFAULT_DATA_DIR)


def _is_driver_crash(exc: Exception) -> bool:
    msg = str(exc).lower()
    if "3221225477" in msg or "0xc0000005" in msg:
        return True
    return any(kw in msg for kw in (
        "unexpectedly exited", "failed to start",
        "chrome not reachable", "cannot connect to chrome", "session not created",
    ))


# â”€â”€ CDP helpers â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

async def _cdp_get_source(send) -> str:
    """
    Return the live page HTML via CDP's native DOM serialisation.

    DOM.getDocument + DOM.getOuterHTML reads directly from the browser's
    internal DOM tree, bypassing any JavaScript property-getter overrides
    (e.g. x.com overrides document.documentElement.outerHTML to return a
    decoy page while visually rendering the real React content).

    Requires DOM.enable to have been called on the session first.
    """
    try:
        doc = await send("DOM.getDocument", {"depth": 0})
        node_id = (doc.get("result") or {}).get("root", {}).get("nodeId")
        if node_id:
            res = await send("DOM.getOuterHTML", {"nodeId": node_id})
            html = (res.get("result") or {}).get("outerHTML", "")
            if html:
                return html
            logger.warning("DOM.getOuterHTML returned empty — falling back to JS eval.")
        else:
            logger.warning("DOM.getDocument returned no nodeId — falling back to JS eval.")
    except Exception as exc:
        logger.warning("DOM source capture failed (%s) — falling back to JS eval.", exc)
    # Fallback: call the original Element.prototype getter directly so that
    # per-instance or per-object prototype overrides (like x.com's decoy)
    # are bypassed at the JS level too.
    result = await send("Runtime.evaluate", {
        "expression": (
            "Object.getOwnPropertyDescriptor(Element.prototype, 'outerHTML')"
            ".get.call(document.documentElement)"
        ),
        "returnByValue": True,
    })
    return (result.get("result", {}).get("result", {}).get("value", "")) or ""


async def _cdp_simulate_human(send) -> None:
    await asyncio.sleep(random.uniform(1.5, 2.5))
    await send("Runtime.evaluate", {
        "expression":
            "window.scrollTo({top: document.body.scrollHeight * 0.3, behavior: 'smooth'});",
        "returnByValue": False,
    })
    await asyncio.sleep(random.uniform(0.5, 1.2))
    await send("Runtime.evaluate", {
        "expression":
            "window.scrollTo({top: document.body.scrollHeight * 0.7, behavior: 'smooth'});",
        "returnByValue": False,
    })
    await asyncio.sleep(random.uniform(0.3, 0.7))


# Strips <noscript> blocks from browser-captured HTML.  Once JavaScript has
# fully rendered the page, noscript content is irrelevant and misleading
# (e.g. x.com embeds a "JavaScript is not available" block that is hidden by
# CSS in a live browser but appears prominently in the raw serialised HTML).
_NOSCRIPT_RE = re.compile(r"<noscript[^>]*>.*?</noscript>", re.DOTALL | re.IGNORECASE)


def _strip_noscript(html: str) -> str:
    return _NOSCRIPT_RE.sub("", html)


# â”€â”€ Chrome manager â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

class _ChromeManager:
    """
    Manages a single Chrome process with a CDP-based tab pool.

    All methods are async and must run on the same asyncio event loop.

    The uc.Chrome WebDriver is used solely to launch Chrome with anti-detection
    patches; actual page navigation and content extraction go through Chrome's
    DevTools Protocol over HTTP + WebSocket, enabling up to MAX_TABS truly
    parallel tab operations on one Chrome window.
    """

    def __init__(self) -> None:
        self._driver = None           # uc.Chrome â€” holds the process alive
        self._debug_host: str = ""    # "127.0.0.1:PORT"
        self._fp_js: str = ""         # fingerprint JS injected into every new doc
        self._headless: bool = False
        self._semaphore: asyncio.Semaphore = asyncio.Semaphore(MAX_TABS)
        self._start_lock: asyncio.Lock = asyncio.Lock()
        self._active: int = 0         # scrapes currently in progress
        self._idle_task: asyncio.Task | None = None

    # â”€â”€ Lifecycle â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

    async def _start(self, profile: FingerprintProfile, headless: bool) -> None:
        loop = asyncio.get_running_loop()
        for attempt in range(2):
            try:
                self._driver = await loop.run_in_executor(
                    None, _launch_uc_chrome, profile, headless
                )
                break
            except Exception as exc:
                if attempt == 0 and _is_driver_crash(exc):
                    logger.warning(
                        "Chrome crash on launch (%s) -- clearing cache, retry.", exc
                    )
                    _clear_uc_cache()
                    continue
                raise

        caps = self._driver.capabilities
        raw_addr: str = (
            caps.get("goog:chromeOptions", {}).get("debuggerAddress")
            or caps.get("debuggerAddress", "")
        )
        if not raw_addr:
            raise RuntimeError("CDP debuggerAddress not found in Chrome capabilities.")

        # Chrome sometimes reports 'localhost'; use the numeric loopback for reliability.
        self._debug_host = raw_addr.replace("localhost", "127.0.0.1")
        self._fp_js = build_browser_js_overrides(profile)
        self._headless = headless
        logger.info(
            "Chrome started; CDP at %s (headless=%s).", self._debug_host, headless
        )

    async def _stop(self) -> None:
        driver, self._driver = self._driver, None
        self._debug_host = ""
        if driver is not None:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, _quit_driver, driver)
            logger.info("Chrome stopped (idle timeout).")

    def _is_alive(self) -> bool:
        if self._driver is None:
            return False
        try:
            _ = self._driver.current_url
            return True
        except Exception:
            return False

    # â”€â”€ Idle management â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

    def _cancel_idle(self) -> None:
        if self._idle_task is not None and not self._idle_task.done():
            self._idle_task.cancel()
        self._idle_task = None

    def _arm_idle(self) -> None:
        self._cancel_idle()
        self._idle_task = asyncio.ensure_future(self._idle_shutdown())

    async def _idle_shutdown(self) -> None:
        try:
            await asyncio.sleep(IDLE_TIMEOUT)
        except asyncio.CancelledError:
            return
        # asyncio is single-threaded: no lock needed for the _active check.
        if self._active == 0 and self._driver is not None:
            await self._stop()

    # â”€â”€ Public entry point â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

    async def scrape(self, url: str, profile: FingerprintProfile, headless: bool = False) -> str:
        # Cancel any pending idle shutdown and mark this request as active
        # before any await so the idle timer cannot fire between sign-up and
        # the semaphore acquisition.
        self._cancel_idle()
        self._active += 1

        try:
            # Serialise Chrome start-up: the first caller creates the process;
            # subsequent concurrent callers return immediately once it is ready.
            async with self._start_lock:
                if not self._is_alive():
                    await self._start(profile, headless)

            # Limit concurrent tabs to MAX_TABS; callers beyond the limit wait here.
            async with self._semaphore:
                return await self._scrape_in_tab(url)

        except Exception:
            # If Chrome has died mid-scrape, reset state so the next request
            # gets a fresh process.
            if self._driver is not None and not self._is_alive():
                driver, self._driver = self._driver, None
                self._debug_host = ""
                asyncio.ensure_future(
                    asyncio.get_running_loop().run_in_executor(None, _quit_driver, driver)
                )
            raise

        finally:
            self._active -= 1
            if self._active == 0:
                self._arm_idle()

    # â”€â”€ CDP tab scraping â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

    async def _scrape_in_tab(self, url: str) -> str:
        """Open a CDP tab, navigate to *url*, extract page source, then close tab."""
        try:
            # websockets 12+ moved the API; fall back to the legacy path.
            from websockets.asyncio.client import connect as ws_connect
        except ImportError:
            from websockets import connect as ws_connect  # type: ignore[no-redef]

        debug_host = self._debug_host
        fp_js = self._fp_js

        # Ask Chrome to open a blank new tab via the DevTools HTTP endpoint.
        async with httpx.AsyncClient(timeout=10.0) as http:
            resp = await http.put(f"http://{debug_host}/json/new")
            resp.raise_for_status()
            tab: dict = resp.json()

        ws_url: str = tab["webSocketDebuggerUrl"].replace("localhost", "127.0.0.1")
        tab_id: str = tab["id"]

        try:
            async with ws_connect(ws_url, max_size=10 * 1024 * 1024) as ws:
                return await self._run_cdp_session(ws, url, fp_js)
        finally:
            # Close the tab regardless of how the session ended.
            with contextlib.suppress(Exception):
                async with httpx.AsyncClient(timeout=5.0) as http:
                    await http.get(f"http://{debug_host}/json/close/{tab_id}")

    async def _run_cdp_session(self, ws, url: str, fp_js: str) -> str:
        """
        Full CDP lifecycle for one tab: enable domains -> inject fingerprint ->
        navigate -> wait for load -> wait for network idle -> resolve CF challenge
        -> simulate human -> return page source.
        """
        cmd_id = 0
        pending: dict[int, asyncio.Future] = {}
        event_subs: dict[str, list[asyncio.Future]] = {}  # one-shot per-method futures
        event_cbs: dict[str, list] = {}                   # persistent callbacks per method

        async def recv_loop() -> None:
            try:
                async for raw in ws:
                    msg: dict = json.loads(raw)
                    # Command responses have an "id" field.
                    mid = msg.get("id")
                    if mid is not None:
                        fut = pending.pop(mid, None)
                        if fut and not fut.done():
                            fut.set_result(msg)
                    # Domain events have a "method" field.
                    method = msg.get("method")
                    if method:
                        for fut in event_subs.get(method, []):
                            if not fut.done():
                                fut.set_result(msg)
                        for cb in event_cbs.get(method, []):
                            cb(msg)
            except Exception:
                pass
            finally:
                # Unblock any coroutines waiting on responses or events.
                for fut in pending.values():
                    if not fut.done():
                        fut.cancel()
                for futs in event_subs.values():
                    for fut in futs:
                        if not fut.done():
                            fut.cancel()

        recv_task = asyncio.create_task(recv_loop())

        def _next_id() -> int:
            nonlocal cmd_id
            cmd_id += 1
            return cmd_id

        async def send(method: str, params: dict | None = None) -> dict:
            cid = _next_id()
            fut: asyncio.Future = asyncio.get_running_loop().create_future()
            pending[cid] = fut
            await ws.send(json.dumps({"id": cid, "method": method, "params": params or {}}))
            return await asyncio.wait_for(fut, timeout=30.0)

        def on_event(method: str) -> asyncio.Future:
            """Register a one-shot future that resolves when *method* fires."""
            fut: asyncio.Future = asyncio.get_running_loop().create_future()
            event_subs.setdefault(method, []).append(fut)
            return fut

        # -- Network-idle tracking ------------------------------------------------
        # Many SPAs (e.g. x.com / Twitter) fire XHR/fetch calls after
        # Page.loadEventFired to load the actual timeline content.  We track
        # in-flight requests so we can wait until they all settle before
        # capturing the page source.
        _in_flight: set[str] = set()

        def _req_start(msg: dict) -> None:
            rid = (msg.get("params") or {}).get("requestId")
            if rid:
                _in_flight.add(rid)

        def _req_end(msg: dict) -> None:
            rid = (msg.get("params") or {}).get("requestId")
            if rid:
                _in_flight.discard(rid)

        async def _wait_network_idle(
            max_wait: float = 15.0,
            stable: float = 0.75,
        ) -> None:
            """
            Block until no network requests have been in flight for *stable*
            seconds, or until *max_wait* seconds elapse -- whichever comes first.
            """
            loop = asyncio.get_running_loop()
            deadline = loop.time() + max_wait
            while loop.time() < deadline:
                if not _in_flight:
                    await asyncio.sleep(stable)
                    if not _in_flight:
                        return
                else:
                    await asyncio.sleep(0.2)

        try:
            await send("Page.enable")
            await send("Runtime.enable")
            await send("Network.enable")
            await send("DOM.enable")  # required before DOM.getDocument / DOM.getOuterHTML

            # Register persistent network callbacks for idle tracking.
            event_cbs.setdefault("Network.requestWillBeSent", []).append(_req_start)
            event_cbs.setdefault("Network.responseReceived", []).append(_req_end)
            event_cbs.setdefault("Network.loadingFailed",    []).append(_req_end)
            event_cbs.setdefault("Network.loadingFinished",  []).append(_req_end)

            if fp_js:
                await send("Page.addScriptToEvaluateOnNewDocument", {"source": fp_js})

            # Register for the load event BEFORE navigating so we never miss a
            # fast (e.g. cached) page load.
            load_evt = on_event("Page.loadEventFired")

            await asyncio.sleep(random.uniform(0.2, 0.5))
            await send("Page.navigate", {"url": url})

            # Wait up to 45 s for the page to fully load.
            try:
                await asyncio.wait_for(asyncio.shield(load_evt), timeout=45.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                logger.warning(
                    "Page load timed out for %s -- continuing with partial content.", url
                )

            # Cloudflare managed-challenge: wait up to 30 s for the JS puzzle to
            # auto-solve and the real page to appear.
            src = await _cdp_get_source(send)
            if any(m in src for m in _CF_CHALLENGE_MARKERS):
                deadline = asyncio.get_running_loop().time() + 30.0
                while asyncio.get_running_loop().time() < deadline:
                    await asyncio.sleep(1.0)
                    src = await _cdp_get_source(send)
                    if not any(m in src for m in _CF_CHALLENGE_MARKERS):
                        break

            # Wait for SPA-initiated XHR/fetch calls (e.g. x.com timeline API)
            # to complete before capturing the rendered DOM.
            await _wait_network_idle()

            # Fixed 10-second hold to allow lazy-loaded content (infinite scroll,
            # deferred images, async widgets) to finish rendering.
            await asyncio.sleep(10.0)

            html = await _cdp_get_source(send)
            html = _strip_noscript(html)
            logger.debug("CDP tab: got %d chars from %s.", len(html), url)
            return html

        finally:
            recv_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await recv_task


# â”€â”€ Module-level helpers â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

def _launch_uc_chrome(profile: FingerprintProfile, headless: bool):
    """Blocking â€” create the undetected Chrome instance (runs in thread pool)."""
    import undetected_chromedriver as uc

    options = uc.ChromeOptions()
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_argument(f"--window-size={profile.viewport_width},{profile.viewport_height}")
    options.add_argument(f"--lang={profile.locale}")
    options.add_argument("--no-first-run")
    options.add_argument("--no-service-autorun")
    if headless:
        options.add_argument("--headless=new")
    return uc.Chrome(options=options, version_main=None)


def _quit_driver(driver) -> None:
    with contextlib.suppress(Exception):
        driver.quit()


# â”€â”€ Module-level singleton and public API â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

_manager: _ChromeManager | None = None


async def scrape_with_browser_async(
    url: str,
    profile: FingerprintProfile,
    headless: bool = False,
) -> str:
    """
    Async entry point for the browser strategy.

    Dispatches *url* to a new CDP tab in the shared Chrome instance.
    Chrome is started on the first call and shuts down automatically after
    IDLE_TIMEOUT seconds of inactivity, restarting transparently on the next
    request.  Up to MAX_TABS requests run in parallel browser tabs; further
    callers wait in the asyncio semaphore queue.
    """
    global _manager
    if _manager is None:
        _manager = _ChromeManager()
    return await _manager.scrape(url, profile, headless)



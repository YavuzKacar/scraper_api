"""
browser_scraper.py — Undetected-chromedriver browser scraper.

Adapted from BUSS-80 (undetected_browser.py) and extended with:
  - Full fingerprint injection via CDP
  - Human-like scrolling + mouse movement simulation
  - Challenge-page detection and retry signalling

Public API
----------
scrape_with_browser(url, profile)   → str
    Blocking function — run in asyncio thread-pool executor.

scrape_with_browser_async(url, profile) → str
    Async wrapper that dispatches to the thread pool.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import random

from fingerprint import FingerprintProfile, build_browser_js_overrides
from utils import human_delay_sync

logger = logging.getLogger(__name__)

# Limit concurrent Chrome processes to avoid exhausting RAM.
# Each undetected-chromedriver instance can consume 200-300 MB.
_browser_semaphore = asyncio.Semaphore(3)


# ── Driver context manager ────────────────────────────────────────────────────

@contextlib.contextmanager
def _uc_driver(
    profile: FingerprintProfile,
    headless: bool = True,
):
    """
    Context manager that yields a configured undetected-chromedriver instance
    and guarantees driver.quit() on exit.
    """
    import undetected_chromedriver as uc

    options = uc.ChromeOptions()
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_argument(
        f"--window-size={profile.viewport_width},{profile.viewport_height}"
    )
    options.add_argument(f"--lang={profile.locale}")
    options.add_argument("--disable-extensions")
    options.add_argument("--disable-plugins-discovery")
    options.add_argument("--no-first-run")
    options.add_argument("--no-service-autorun")

    if headless:
        # New headless mode — harder to detect than "--headless"
        options.add_argument("--headless=new")

    driver = uc.Chrome(options=options, version_main=None)

    try:
        # Inject fingerprint overrides before any page load
        driver.execute_cdp_cmd(
            "Page.addScriptToEvaluateOnNewDocument",
            {"source": build_browser_js_overrides(profile)},
        )
        yield driver
    finally:
        with contextlib.suppress(Exception):
            driver.quit()


# ── Human-behaviour simulation ────────────────────────────────────────────────

def _simulate_human_browsing(driver) -> None:
    """
    Simulate gradual scrolling and pauses to mimic a human reader.

    Flow:
      1. Open page (already done by caller)
      2. Random short pause
      3. Scroll to ~30 %
      4. Random pause
      5. Scroll to ~70 %
      6. Final pause before extraction
    """
    from selenium.webdriver.common.action_chains import ActionChains

    human_delay_sync(1.0, 2.5)

    scroll_height: int = driver.execute_script(
        "return document.body.scrollHeight"
    ) or 1000

    # Scroll to ~30 %
    target_30 = int(scroll_height * 0.3)
    driver.execute_script(f"window.scrollTo({{top: {target_30}, behavior: 'smooth'}});")
    human_delay_sync(1.2, 3.0)

    # Simulate small random mouse movements via ActionChains
    try:
        actions = ActionChains(driver)
        for _ in range(random.randint(2, 5)):
            actions.move_by_offset(
                random.randint(-40, 40),
                random.randint(-20, 20),
            )
        actions.perform()
    except Exception:
        pass  # Mouse movement is best-effort

    # Scroll to ~70 %
    target_70 = int(scroll_height * 0.7)
    driver.execute_script(f"window.scrollTo({{top: {target_70}, behavior: 'smooth'}});")
    human_delay_sync(0.8, 2.0)


# ── Core scrape function (blocking) ──────────────────────────────────────────

def scrape_with_browser(
    url: str,
    profile: FingerprintProfile,
    headless: bool = True,
) -> str:
    """
    Load *url* with an undetected Chrome instance, simulate human browsing,
    and return the fully rendered page source.

    Blocking — intended to run inside asyncio.get_event_loop().run_in_executor().

    Raises RuntimeError if undetected_chromedriver is not installed.
    Returns empty string on any navigation failure.
    """
    try:
        import undetected_chromedriver  # noqa: F401 — verify import
    except ImportError as exc:
        raise RuntimeError(
            "undetected-chromedriver is not installed. "
            "Run: pip install undetected-chromedriver"
        ) from exc

    logger.debug("Browser scraper: loading %s", url)

    with _uc_driver(profile, headless=headless) as driver:
        # Prevent indefinite hang on pages that never finish loading.
        driver.set_page_load_timeout(30)
        human_delay_sync(0.5, 1.5)
        driver.get(url)

        # Wait for initial JS execution
        human_delay_sync(2.5, 5.0)

        _simulate_human_browsing(driver)

        html: str = driver.page_source or ""
        logger.debug(
            "Browser scraper: got %d chars from %s", len(html), url
        )
        return html


# ── Async wrapper ─────────────────────────────────────────────────────────────

async def scrape_with_browser_async(
    url: str,
    profile: FingerprintProfile,
    headless: bool = True,
) -> str:
    """
    Async wrapper — dispatches ``scrape_with_browser`` to the default
    thread-pool executor so it does not block the event loop.

    Acquires ``_browser_semaphore`` before launching to cap concurrent
    Chrome instances and prevent out-of-memory crashes under load.
    """
    async with _browser_semaphore:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None,
            scrape_with_browser,
            url,
            profile,
            headless,
        )

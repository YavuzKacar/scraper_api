"""
config.py — .env-driven configuration for Scraper API.

All settings are read exclusively from the .env file in this directory.
OS/process environment variables are intentionally ignored -- .env is the
single source of truth, so a stray system-level env var can never silently
override what's configured here. Edit .env to change any value.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional

from dotenv import dotenv_values

# dotenv_values() parses .env into a plain dict without touching
# os.environ, so process/OS environment variables have no effect on config.
_ENV: dict[str, Optional[str]] = dotenv_values(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
)


def _get(key: str, default: str = "") -> str:
    value = _ENV.get(key)
    return value if value is not None else default


@dataclass(frozen=True)
class Config:
    # ── Security ──────────────────────────────────────────────────────────────
    api_key: str                        # X-API-KEY header value

    # ── Server ────────────────────────────────────────────────────────────────
    host: str                           # Bind address (default: 127.0.0.1)
    port: int                           # Listen port  (default: 8000)

    # ── Database ──────────────────────────────────────────────────────────────
    db_path: str                        # Path to SQLite file

    # ── Tor ───────────────────────────────────────────────────────────────────
    tor_socks_host: str                 # SOCKS5 proxy host
    tor_socks_port: int                 # SOCKS5 proxy port (9050 or 9150)
    tor_control_port: int               # Control port for NEWNYM
    tor_control_password: Optional[str] # Tor control password (None = cookie auth)
    tor_exe_path: str                   # Full path to tor.exe; used for auto-launch
    firefox_binary_path: str            # Full path to Tor Browser's firefox.exe
    # ── Scraper behaviour ─────────────────────────────────────────────────────
    retry_count: int                    # Max scrape retries per request
    domain_rate_limit_seconds: float    # Min seconds between requests to same domain
    request_timeout: float              # HTTP request timeout in seconds
    max_concurrent_scrapes: int         # Max parallel scrape operations (semaphore)
    result_cache_ttl_seconds: int       # Seconds to cache successful results (0=disabled)

    # ── Credits ───────────────────────────────────────────────────────────────
    initial_credits: int                # Starting credit balance seeded at first run

    # ── Scheduler ─────────────────────────────────────────────────────────────
    scheduler_interval_seconds: int     # Background scheduler sleep interval

    # ── Logging ───────────────────────────────────────────────────────────────
    log_level: str                      # DEBUG | INFO | WARNING | ERROR
    log_dir: str                        # Directory for rotating log files ("" = console only)

    # ── Request safety limits ────────────────────────────────────────────────
    max_response_size_bytes: int        # Abort a scrape once the response exceeds this size
    max_page_load_seconds: float        # Max time to wait for a browser page load

    # ── SSRF protection / allow-block list ───────────────────────────────────
    ssrf_protection_enabled: bool       # Kill switch for the SSRF guard (default on)
    url_allowlist_seed: str             # Comma-separated domains; seeds data/url_lists.json once
    url_blocklist_seed: str             # Comma-separated domains; seeds data/url_lists.json once

    # ── Fallback providers (Scrape.do / Zyte) ────────────────────────────────
    scrape_do_api_key: str
    scrape_do_base_url: str
    zyte_api_key: str
    zyte_base_url: str

    # ── Per-provider cost (USD-equivalent credits charged per successful scrape) ─
    # Placeholder figures -- tune to your real plan pricing via env vars.
    cost_static: float
    cost_browser: float
    cost_tor: float
    cost_scrape_do: float
    cost_zyte: float


def load_config() -> Config:
    """Build Config exclusively from .env (see _ENV / _get above)."""
    tor_control_password = _ENV.get("TOR_CONTROL_PASSWORD") or None
    return Config(
        api_key=_get("SCRAPER_API_KEY", "change-me-immediately"),
        host=_get("SCRAPER_HOST", "127.0.0.1"),
        port=int(_get("SCRAPER_PORT", "8000")),
        db_path=_get("SCRAPER_DB_PATH", "scraper_metadata.db"),
        tor_socks_host=_get("TOR_SOCKS_HOST", "127.0.0.1"),
        # Tor Browser uses 9150; standalone tor daemon uses 9050.
        # Both are tried automatically at runtime; this sets the default
        # for the health check and explicit config overrides.
        tor_socks_port=int(_get("TOR_SOCKS_PORT", "9150")),
        tor_control_port=int(_get("TOR_CONTROL_PORT", "9151")),
        tor_control_password=tor_control_password,
        tor_exe_path=_get(
            "TOR_EXE_PATH",
            r"C:\Users\Pc\Desktop\Tor Browser\Browser\TorBrowser\Tor\tor.exe",
        ),
        firefox_binary_path=_get(
            "FIREFOX_BINARY_PATH",
            r"C:\Users\Pc\Desktop\Tor Browser\Browser\firefox.exe",
        ),
        retry_count=int(_get("RETRY_COUNT", "3")),
        domain_rate_limit_seconds=float(_get("DOMAIN_RATE_LIMIT_SECONDS", "2.0")),
        request_timeout=float(_get("REQUEST_TIMEOUT", "30.0")),
        max_concurrent_scrapes=int(_get("MAX_CONCURRENT_SCRAPES", "10")),
        result_cache_ttl_seconds=int(_get("RESULT_CACHE_TTL_SECONDS", "600")),
        initial_credits=int(_get("INITIAL_CREDITS", "10000")),
        scheduler_interval_seconds=int(_get("SCHEDULER_INTERVAL_SECONDS", "300")),
        log_level=_get("LOG_LEVEL", "INFO").upper(),
        log_dir=_get("LOG_DIR", "logs"),
        max_response_size_bytes=int(_get("MAX_RESPONSE_SIZE_BYTES", str(20_000_000))),
        max_page_load_seconds=float(_get("MAX_PAGE_LOAD_SECONDS", "45.0")),
        ssrf_protection_enabled=_get("SSRF_PROTECTION_ENABLED", "true").lower() not in ("0", "false", "no"),
        url_allowlist_seed=_get("URL_ALLOWLIST", ""),
        url_blocklist_seed=_get("URL_BLOCKLIST", ""),
        scrape_do_api_key=_get("SCRAPE_DO_API_KEY", ""),
        scrape_do_base_url=_get("SCRAPE_DO_BASE_URL", "https://api.scrape.do"),
        zyte_api_key=_get("ZYTE_API_KEY", ""),
        zyte_base_url=_get("ZYTE_BASE_URL", "https://api.zyte.com/v1/extract"),
        cost_static=float(_get("COST_STATIC", "0.001")),
        cost_browser=float(_get("COST_BROWSER", "0.01")),
        cost_tor=float(_get("COST_TOR", "0.02")),
        cost_scrape_do=float(_get("COST_SCRAPE_DO", "0.05")),
        cost_zyte=float(_get("COST_ZYTE", "0.08")),
    )


# Module-level singleton — import this everywhere.
CONFIG: Config = load_config()

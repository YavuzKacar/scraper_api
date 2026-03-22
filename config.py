"""
config.py — Environment-driven configuration for Scraper API.

All settings are read from environment variables with sensible defaults.
Override any value by setting the corresponding environment variable before
starting the server.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional


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

    # ── Scraper behaviour ─────────────────────────────────────────────────────
    retry_count: int                    # Max scrape retries per request
    domain_rate_limit_seconds: float    # Min seconds between requests to same domain
    request_timeout: float              # HTTP request timeout in seconds

    # ── Logging ───────────────────────────────────────────────────────────────
    log_level: str                      # DEBUG | INFO | WARNING | ERROR
    log_dir: str                        # Directory for rotating log files ("" = console only)


def load_config() -> Config:
    """Build Config from environment variables."""
    return Config(
        api_key=os.environ.get("SCRAPER_API_KEY", "change-me-immediately"),
        host=os.environ.get("SCRAPER_HOST", "127.0.0.1"),
        port=int(os.environ.get("SCRAPER_PORT", "8000")),
        db_path=os.environ.get("SCRAPER_DB_PATH", "scraper_metadata.db"),
        tor_socks_host=os.environ.get("TOR_SOCKS_HOST", "127.0.0.1"),
        # Tor Browser uses 9150; standalone tor daemon uses 9050.
        # Both are tried automatically at runtime; this sets the default
        # for the health check and explicit config overrides.
        tor_socks_port=int(os.environ.get("TOR_SOCKS_PORT", "9150")),
        tor_control_port=int(os.environ.get("TOR_CONTROL_PORT", "9151")),
        tor_control_password=os.environ.get("TOR_CONTROL_PASSWORD"),
        tor_exe_path=os.environ.get(
            "TOR_EXE_PATH",
            r"C:\Users\Pc\Desktop\Tor Browser\Browser\TorBrowser\Tor\tor.exe",
        ),
        retry_count=int(os.environ.get("RETRY_COUNT", "3")),
        domain_rate_limit_seconds=float(os.environ.get("DOMAIN_RATE_LIMIT_SECONDS", "2.0")),
        request_timeout=float(os.environ.get("REQUEST_TIMEOUT", "30.0")),
        log_level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        log_dir=os.environ.get("LOG_DIR", "logs"),
    )


# Module-level singleton — import this everywhere.
CONFIG: Config = load_config()

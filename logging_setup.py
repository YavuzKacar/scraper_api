"""
logging_setup.py — Centralised logging configuration for Scraper API.

Sets up:
  - Console handler (always on)
  - Rotating file handler (when LOG_DIR is set, default: logs/)
    - scraper_api.log   — INFO and above, rotates at 5 MB, keeps 5 backups
    - scraper_errors.log — ERROR and above, rotates at 2 MB, keeps 5 backups

Log level is controlled by the LOG_LEVEL env var (default: INFO).

Call setup_logging() once at startup before any other module logs anything.
"""
from __future__ import annotations

import logging
import logging.handlers
import os
import sys


_LOG_FORMAT = "%(asctime)s [%(levelname)-8s] %(name)s — %(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
_REQUEST_FORMAT = (
    "%(asctime)s [%(levelname)-8s] REQUEST — "
    "%(message)s"
)


def setup_logging(log_level: str = "INFO", log_dir: str = "logs") -> None:
    """
    Configure the root logger with console + optional rotating file handlers.

    Parameters
    ----------
    log_level : str
        Minimum log level for all handlers (DEBUG/INFO/WARNING/ERROR).
    log_dir : str
        Directory to write log files. Pass empty string to disable file logging.
    """
    numeric_level = getattr(logging, log_level.upper(), logging.INFO)

    root = logging.getLogger()
    root.setLevel(numeric_level)

    # Remove any handlers already attached (e.g. from basicConfig in tests)
    root.handlers.clear()

    formatter = logging.Formatter(_LOG_FORMAT, datefmt=_DATE_FORMAT)

    # ── Console handler ───────────────────────────────────────────────────────
    console = logging.StreamHandler(sys.stdout)
    console.setLevel(numeric_level)
    console.setFormatter(formatter)
    root.addHandler(console)

    # ── File handlers ─────────────────────────────────────────────────────────
    if log_dir:
        os.makedirs(log_dir, exist_ok=True)

        # All messages at the configured level
        main_handler = logging.handlers.RotatingFileHandler(
            filename=os.path.join(log_dir, "scraper_api.log"),
            maxBytes=5 * 1024 * 1024,   # 5 MB
            backupCount=5,
            encoding="utf-8",
        )
        main_handler.setLevel(numeric_level)
        main_handler.setFormatter(formatter)
        root.addHandler(main_handler)

        # Errors only — separate file for quick triage
        error_handler = logging.handlers.RotatingFileHandler(
            filename=os.path.join(log_dir, "scraper_errors.log"),
            maxBytes=2 * 1024 * 1024,   # 2 MB
            backupCount=5,
            encoding="utf-8",
        )
        error_handler.setLevel(logging.ERROR)
        error_handler.setFormatter(formatter)
        root.addHandler(error_handler)

    # Quieten noisy third-party loggers
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("selenium").setLevel(logging.WARNING)
    logging.getLogger("undetected_chromedriver").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)

    logging.getLogger(__name__).info(
        "Logging initialised — level=%s dir=%s",
        log_level.upper(),
        os.path.abspath(log_dir) if log_dir else "(console only)",
    )

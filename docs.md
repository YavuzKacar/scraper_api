# Scraper API — Documentation

## Table of Contents

1. [Architecture](#1-architecture)
2. [Scraping Strategy](#2-scraping-strategy)
3. [Fingerprint System](#3-fingerprint-system)
4. [Database Schema](#4-database-schema)
5. [API Reference](#5-api-reference)
6. [Security Model](#6-security-model)
7. [Setup Instructions](#7-setup-instructions)
8. [Configuration Reference](#8-configuration-reference)

---

## 1. Architecture

```
Internal App
    |  HTTP  X-API-KEY
    v
FastAPI  (app.py)
  /scrape   /status/{url}   /health   /feedback
    |
 scraper.py  (orchestrator)
    |
 database.py (SQLite/aiosqlite)  fingerprint.py (profiles)
    |
  browser_scraper.py    tor_scraper.py
  (uc + CDP tabs)       (httpx + SOCKS5)
```

### Module responsibilities

| Module | Responsibility |
|---|---|
| `app.py` | FastAPI routes, lifespan, CORS |
| `config.py` | Env-var config singleton |
| `models.py` | Pydantic models |
| `database.py` | Async SQLite CRUD |
| `fingerprint.py` | Browser fingerprint profiles and header builder |
| `scraper.py` | Cache check, strategy selection, retry loop, persistence |
| `browser_scraper.py` | Chrome via CDP (single process, parallel tabs) |
| `tor_scraper.py` | httpx over Tor SOCKS5 |
| `security.py` | API-key + localhost validation |
| `utils.py` | Delays, rate limiter, failure detectors |
| `logging_setup.py` | Console + rotating file logging |

---

## 2. Scraping Strategy

Every request follows the same simple flow — no pre-classification needed.

### Default order (no prior data)

1. **Browser** — single Chrome process, parallel CDP tabs, undetected-chromedriver patches
2. **Tor** — httpx over SOCKS5, randomised fingerprint headers

### Learned strategy

After any successful scrape the working strategy is stored in the
`domain_strategies` table keyed by **root URL** (e.g. `https://x.com`).
The next request for any URL on that domain starts with the stored
strategy rather than always defaulting to browser-first.

If the stored strategy fails, the other is tried automatically.

### force_strategy override

Pass `force_strategy: "browser" | "tor" | "static"` in the request body
to skip strategy selection entirely.

### Retry logic

Each strategy is retried up to `RETRY_COUNT` (default: 3) times with
exponential backoff (2 s base ± 50 % jitter). Tor identity is rotated
(NEWNYM) between Tor retries.

---

## 3. Fingerprint System

Five predefined profiles in `fingerprint.py`:

| Profile | Browser | OS | Viewport |
|---|---|---|---|
| `desktop_chrome_windows` | Chrome 122 | Windows 10 | 1920×1080 |
| `desktop_chrome_linux` | Chrome 122 | Linux x86_64 | 1920×1080 |
| `desktop_firefox_windows` | Firefox 123 | Windows 10 | 1440×900 |
| `mobile_chrome_android` | Chrome 122 Mobile | Android 14 | 412×915 |
| `mobile_safari_ios` | Safari 17 | iOS 17.3 | 390×844 |

Each request selects a profile at random. HTTP headers and CDP navigator
overrides (platform, WebGL, plugins) are applied from the profile.

---

## 4. Database Schema

```sql
CREATE TABLE url_metadata (
    url                TEXT PRIMARY KEY,
    scraping_strategy  TEXT,          -- last winning strategy
    last_checked       TEXT,          -- ISO 8601 UTC
    last_scrape_status TEXT,          -- 'success' | 'failed'
    last_success_html  TEXT           -- cached HTML body
);

CREATE TABLE domain_strategies (
    root_url     TEXT PRIMARY KEY,    -- e.g. 'https://x.com'
    strategy     TEXT NOT NULL,       -- 'browser' | 'tor' | 'static'
    last_updated TEXT NOT NULL        -- ISO 8601 UTC
);

CREATE TABLE url_feedback (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    url            TEXT NOT NULL,
    comment        TEXT NOT NULL,
    strategy_used  TEXT,
    scrape_success INTEGER,
    created_at     TEXT NOT NULL
);
```

---

## 5. API Reference

All endpoints require `X-API-KEY` header and must originate from `127.0.0.1`.

---

### POST /scrape

Scrape a URL. Returns cached HTML when still within the TTL.

**Request body**

```json
{
  "url": "https://example.com/page",
  "force_scrape": false,
  "force_strategy": null
}
```

| Field | Type | Default | Description |
|---|---|---|---|
| `url` | string | required | Must start with `http://` or `https://` |
| `force_scrape` | boolean | false | Bypass HTML cache, always fetch fresh |
| `force_strategy` | string | null | One of `browser`, `tor`, `static` |

**Success response**

```json
{
  "url": "https://example.com/page",
  "scraping_success": true,
  "message": "Scraped successfully.",
  "html": "<!DOCTYPE html>...",
  "cached": false,
  "strategy_used": "browser"
}
```

**Failure response**

```json
{
  "url": "https://example.com/page",
  "scraping_success": false,
  "message": "Scraping failed after trying [browser -> tor]. <detail>",
  "strategy_used": null
}
```

---

### GET /status/{url}

Return stored metadata for a URL. `last_success_html` is excluded.

```
GET /status/https%3A%2F%2Fexample.com
```

```json
{
  "url": "https://example.com",
  "found": true,
  "record": {
    "url": "https://example.com",
    "scraping_strategy": "browser",
    "last_checked": "2026-03-23T10:00:00+00:00",
    "last_scrape_status": "success",
    "last_success_html": null
  }
}
```

---

### GET /health

```json
{
  "status": "ok",
  "database": "ok",
  "tor_reachable": true,
  "tor_socks_port": 9150,
  "tor_control_reachable": true,
  "tor_circuit_ok": true
}
```

---

### POST /feedback

Save a comment for a URL.

```json
{ "url": "https://example.com", "comment": "always needs browser", "strategy_used": "browser", "scrape_success": true }
```

### GET /feedback

List all comments, or filter by `?url=https://example.com`.

### DELETE /feedback/{id}

Delete a comment by ID.

### DELETE /feedback

Delete all comments.

---

## 6. Security Model

- Server binds to **127.0.0.1 only** — never externally accessible.
- CORS restricted to `http://127.0.0.1:*` and `http://localhost:*`.
- Every request requires `X-API-KEY` header (constant-time comparison).
- Missing key → HTTP 401. Non-localhost origin → HTTP 403.

Set the key before starting:

```powershell
$env:SCRAPER_API_KEY = "your-long-random-secret"
python app.py
```

---

## 7. Setup Instructions

### Prerequisites

- Python 3.11+
- Google Chrome (for `browser` strategy)
- Tor running locally (for `tor` strategy — Tor Browser or standalone daemon)

### Install

```powershell
pip install -r requirements.txt
```

### Run

```powershell
$env:SCRAPER_API_KEY = "my-secret-key"
python app.py
```

The API is now available at `http://127.0.0.1:8000`.
Test UI: `http://127.0.0.1:8000/ui`

### Run tests

```powershell
$env:SCRAPER_API_KEY = "my-secret-key"
pytest test_api.py -v
```

---

## 8. Configuration Reference

All values read from environment variables with the defaults shown.

| Variable | Default | Description |
|---|---|---|
| `SCRAPER_API_KEY` | `change-me-immediately` | API key for `X-API-KEY` header |
| `SCRAPER_HOST` | `127.0.0.1` | Bind address |
| `SCRAPER_PORT` | `8000` | Listen port |
| `SCRAPER_DB_PATH` | `scraper_metadata.db` | SQLite file path |
| ~~`CACHE_TTL_SECONDS`~~ | `600` | Seconds to serve cached HTML |
| `RETRY_COUNT` | `3` | Max attempts per strategy |
| `DOMAIN_RATE_LIMIT_SECONDS` | `2.0` | Min gap between same-domain requests |
| `REQUEST_TIMEOUT` | `30.0` | HTTP request timeout (seconds) |
| `TOR_SOCKS_HOST` | `127.0.0.1` | Tor SOCKS5 host |
| `TOR_SOCKS_PORT` | `9150` | Tor SOCKS5 port (9150 = Tor Browser, 9050 = daemon) |
| `TOR_CONTROL_PORT` | `9151` | Tor control port |
| `TOR_CONTROL_PASSWORD` | _(none)_ | Tor control password; unset = cookie auth |
| `TOR_EXE_PATH` | _(Windows default)_ | Path to `tor.exe` for auto-launch |
| `LOG_LEVEL` | `INFO` | `DEBUG` / `INFO` / `WARNING` / `ERROR` |
| `LOG_DIR` | `logs` | Log file directory; empty string = console only |

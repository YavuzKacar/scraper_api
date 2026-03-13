# Scraper API — Documentation

## Table of Contents

1. [System Architecture](#1-system-architecture)
2. [Classification Logic](#2-classification-logic)
3. [Strategy Engine](#3-strategy-engine)
4. [Fingerprint System](#4-fingerprint-system)
5. [Scraper Implementations](#5-scraper-implementations)
6. [Database Schema](#6-database-schema)
7. [API Reference](#7-api-reference)
8. [Security Model](#8-security-model)
9. [Background Scheduler](#9-background-scheduler)
10. [Setup Instructions](#10-setup-instructions)
11. [Configuration Reference](#11-configuration-reference)
12. [Extension Guide](#12-extension-guide)

---

## 1. System Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                     Internal Python App                     │
│                  (calls API via localhost)                   │
└───────────────────────────┬─────────────────────────────────┘
                            │ HTTP  X-API-KEY
                            ▼
┌─────────────────────────────────────────────────────────────┐
│                   FastAPI  (app.py)                         │
│   /scrape  /classify  /status/{url}  /health               │
│              └── security.py (API key + localhost check)    │
└────────────────────────────┬────────────────────────────────┘
                             │
          ┌──────────────────┼───────────────────┐
          ▼                  ▼                   ▼
   classifier.py        scraper.py          scheduler.py
   (classify_url)    (orchestrator)    (background tasks)
          │                  │
          │        ┌─────────┴──────────┐
          │        │                    │
          ▼        ▼                    ▼
    strategy.py  database.py      fingerprint.py
    (pure fn)   (SQLite/aiosqlite) (profiles)
                                         │
                             ┌───────────┼───────────┐
                             ▼           ▼           ▼
                         scraper.py  tor_scraper  browser_
                         (static/    .py (httpx   scraper.py
                          httpx)      + SOCKS5)   (uc + CDP)
```

### Module responsibilities

| Module | Responsibility |
|---|---|
| `app.py` | FastAPI routes, lifespan management, CORS |
| `config.py` | Env-var config, single CONFIG singleton |
| `models.py` | Pydantic models, enumerations |
| `database.py` | Async SQLite CRUD via aiosqlite |
| `classifier.py` | Multi-dimensional URL classification |
| `strategy.py` | Pure strategy selection function |
| `fingerprint.py` | Browser fingerprint profiles and header builder |
| `scraper.py` | Scrape orchestration: cache, rate-limit, retry, dispatch |
| `tor_scraper.py` | httpx-over-SOCKS5 and Selenium-Firefox-over-Tor |
| `browser_scraper.py` | undetected-chromedriver with CDP fingerprint injection |
| `scheduler.py` | Background reclassification of low-confidence/stale URLs |
| `security.py` | FastAPI dependency: API-key + localhost validation |
| `utils.py` | Delays, rate limiter, challenge/CAPTCHA detectors |

---

## 2. Classification Logic

Every URL is classified across five independent dimensions before scraping.
Results are cached in SQLite and re-used across requests.

### 2.1 Content Type (`static` | `dynamic`)

Five independent signals detected from the raw HTTP response:

| Signal | Description |
|---|---|
| JS framework name in HTML | React, Vue, Angular, Next.js, Nuxt, Svelte, etc. |
| SPA mount points | `<div id="app">`, `<div id="root">`, `ng-app` |
| Thin body with scripts | Body contains only `<script>` tags, minimal text |
| X-Powered-By header | Next.js/Nuxt declared in response headers |
| Bundle filenames | `bundle.[hash].js`, `chunk.[hash].js` in `<script src>` |

**Confidence** — 0.5 base + 0.1 per confirming signal, capped at 1.0.
Three or more signals in either direction lock the classification.

### 2.2 Anti-Scraping Protection (`none` | `protected`)

| Signal | Description |
|---|---|
| Bot-detection headers | `x-datadome`, `x-kasada`, `x-akamai-edgescape` |
| Cloudflare headers | `cf-ray`, `__cf_bm`, `cf-mitigated` in response headers |
| Cloudflare HTML | "Checking your browser", "Just a moment" page text |
| CAPTCHA widgets | reCAPTCHA, hCaptcha, FunCAPTCHA, Turnstile |
| HTTP 403/429/503 | Server-side rate limiting or hard block |
| Very short 200 response | <512 chars — likely a redirect/block page |

Two or more signals → `protected`.

### 2.3 Tor Network Available (`yes` | `no`)

1. TCP connect check on Tor SOCKS port (9050 or 9150).
2. If reachable, make a GET request through `socks5://127.0.0.1:PORT` via httpx.
3. `yes` if HTTP status < 500, `no` otherwise.

### 2.4 Undetected Browser Available (`yes` | `no`)

- If static probe shows no challenge: `yes` (inference, high confidence).
- If static probe shows Cloudflare/CAPTCHA: launch undetected-chromedriver and
  check whether the resulting page source is still a challenge. Result reflects
  actual browser capability.

### 2.5 Public Page (`yes` | `no`)

| Signal | Description |
|---|---|
| HTTP 401/403 | Server explicitly blocks access |
| Login redirect | Final URL path matches `/login`, `/signin`, `/auth` |
| Login keywords in HTML | "sign in to view", "members only", "subscription required" |
| WWW-Authenticate header | HTTP authentication required |

Two or more signals → `no` (private).

### 2.6 Confidence Score

Overall confidence is the **geometric mean** of the five per-dimension confidences:

```
confidence = (c1 × c2 × c3 × c4 × c5) ^ (1/5)
```

A single very uncertain dimension pulls the total score down significantly,
triggering reclassification on the next scrape or scheduler cycle.

**Reclassification threshold**: `LOW_CONFIDENCE_THRESHOLD` (default: **0.6**)

---

## 3. Strategy Engine

`strategy.py` is a pure function (no side effects):

```
determine_strategy(classification) → ScrapingStrategy
```

### Decision Tree

```
is_public_page == no
    → blocked   (never scrape private pages)

antiscraping_protection == none
    content_type == static  → static
    content_type == dynamic → browser

antiscraping_protection == protected
    tor_works AND browser_works  → hybrid
    tor_works only               → tor
    browser_works only           → browser
    neither                      → blocked
```

### Scraping strategy behaviour

| Strategy | Transport | Rendering | Use case |
|---|---|---|---|
| `static` | httpx direct | None | Plain HTML sites |
| `browser` | Direct TCP | Undetected Chrome | JS-heavy or lightly protected |
| `tor` | Tor SOCKS5 | httpx | Protected, no JS needed |
| `hybrid` | Tor SOCKS5 | Firefox via Tor | Heavily protected + JS needed |
| `blocked` | — | — | No viable path found |

---

## 4. Fingerprint System

### Profiles

Five predefined profiles in `fingerprint.py`:

| Profile | Browser | OS | Viewport |
|---|---|---|---|
| `desktop_chrome_windows` | Chrome 122 | Windows 10 | 1920×1080 |
| `desktop_chrome_linux` | Chrome 122 | Linux x86_64 | 1920×1080 |
| `desktop_firefox_windows` | Firefox 123 | Windows 10 | 1440×900 |
| `mobile_chrome_android` | Chrome 122 Mobile | Android 14 | 412×915 |
| `mobile_safari_ios` | Safari 17 | iOS 17.3 | 390×844 |

Each request selects a profile at random via `get_random_profile()`.

### Randomised HTTP headers

For each request:
- `User-Agent` — from selected profile
- `Accept-Language` — from profile locale
- `Accept-Encoding` — randomly `gzip, deflate, br`, `gzip, deflate, br, zstd`, etc.
- `Referer` — randomly Google, Bing, DuckDuckGo, Yahoo, or none
- `Connection` — `keep-alive`
- Chromium profiles additionally send `sec-ch-ua`, `Sec-Fetch-*` headers

### Browser fingerprint overrides (CDP injection)

Injected via `Page.addScriptToEvaluateOnNewDocument` before any page loads:

- `navigator.webdriver` → `undefined`
- `navigator.platform` → profile platform
- `navigator.deviceMemory` → profile value
- `navigator.hardwareConcurrency` → profile value
- `navigator.language` / `.languages` → profile locale
- `WebGLRenderingContext.getParameter` — spoofs vendor and renderer strings
- `navigator.plugins` — non-empty realistic list

---

## 5. Scraper Implementations

### Static scraper (`scraper.py` → `_scrape_static`)

- Uses `httpx.AsyncClient` with fingerprinted headers
- Follows redirects, configurable timeout
- Raises on HTTP errors (4xx/5xx)

### Browser scraper (`browser_scraper.py`)

- `scrape_with_browser(url, profile)` — blocking, run in thread-pool executor
- Launches `undetected_chromedriver.Chrome` with `--headless=new`
- Injects CDP fingerprint overrides before page load
- Simulates human browsing:
  1. Random pre-navigation pause (0.5–1.5 s)
  2. Page load + JS execution wait (2.5–5 s)
  3. Scroll to 30 % + random mouse movements
  4. Scroll to 70 % + pause
  5. DOM extraction

### Tor scraper (`tor_scraper.py`)

Two paths:

**Lightweight** (`scrape_with_tor`) — httpx over SOCKS5  
Port auto-detection: tries 9050 → 9150. Randomised fingerprint headers.

**Full browser** (`scrape_with_tor_browser`) — Selenium Firefox via Tor  
Firefox profile configured for SOCKS5 + remote DNS. Suitable for `hybrid`
strategy where both Tor anonymity and JS rendering are required.

**Identity rotation** (`rotate_tor_identity`)  
Sends NEWNYM to Tor control port via Stem. Triggered between retries when 
using Tor-based strategies.

### Hybrid strategy

Uses `scrape_with_tor_browser`: a real Firefox browser whose traffic is
routed entirely through the Tor network. Provides maximum stealth while
supporting full JavaScript rendering.

---

## 6. Database Schema

```sql
CREATE TABLE url_metadata (
    url                          TEXT PRIMARY KEY,
    content_type                 TEXT,          -- 'static' | 'dynamic'
    antiscraping_protection      TEXT,          -- 'none' | 'protected'
    tor_network_available        TEXT,          -- 'yes' | 'no'
    undetected_browser_available TEXT,          -- 'yes' | 'no'
    is_public_page               TEXT,          -- 'yes' | 'no'
    scraping_strategy            TEXT,          -- 'static' | 'browser' | 'tor'
                                                --   | 'hybrid' | 'blocked'
    classification_confidence    REAL,          -- 0.0 – 1.0
    last_checked                 TEXT,          -- ISO 8601 UTC datetime
    last_scrape_status           TEXT,          -- 'success' | 'failed'
    last_success_html            TEXT           -- cached HTML body
);

CREATE INDEX idx_last_checked  ON url_metadata(last_checked);
CREATE INDEX idx_confidence    ON url_metadata(classification_confidence);
```

### Field notes

| Field | Notes |
|---|---|
| `url` | Full URL including scheme, path, and query string |
| `classification_confidence` | Geometric mean of per-dimension scores |
| `last_checked` | Updated on every classify or scrape attempt |
| `last_success_html` | Only overwritten on successful scrape |
| `last_scrape_status` | Updated on every scrape attempt (success or failure) |

---

## 7. API Reference

All endpoints require `X-API-KEY` header and must originate from `127.0.0.1`.

### POST /scrape

Scrape a URL using the adaptive strategy engine.

**Request body**

```json
{
  "url": "https://example.com/page",
  "force_reclassify": false
}
```

| Field | Type | Required | Description |
|---|---|---|---|
| `url` | string | yes | Must start with `http://` or `https://` |
| `force_reclassify` | boolean | no | Re-run classification even if cached (default: false) |

**Response**

```json
{
  "url": "https://example.com/page",
  "scraping_success": true,
  "message": "Scraped successfully.",
  "html": "<!DOCTYPE html>...",
  "classification": {
    "content_type": "static",
    "antiscraping_protection": "none",
    "tor_network_available": "yes",
    "undetected_browser_available": "yes",
    "is_public_page": "yes",
    "scraping_strategy": "static",
    "classification_confidence": 0.82
  },
  "cached": false,
  "strategy_used": "static"
}
```

**Failure responses**

```json
{ "scraping_success": false, "message": "This page is not public." }
{ "scraping_success": false, "message": "This website uses advanced anti-scraping protection." }
{ "scraping_success": false, "message": "Scraping failed after 3 attempts. <detail>" }
```

---

### POST /classify

Classify a URL and persist the result.

**Request body**

```json
{
  "url": "https://example.com",
  "force": false
}
```

**Response**

```json
{
  "url": "https://example.com",
  "classification": {
    "content_type": "dynamic",
    "antiscraping_protection": "protected",
    "tor_network_available": "yes",
    "undetected_browser_available": "no",
    "is_public_page": "yes",
    "scraping_strategy": "tor",
    "classification_confidence": 0.71
  },
  "from_cache": false
}
```

---

### GET /status/{url}

Retrieve stored metadata for a URL.  URL-encode the URL parameter.

**Example**

```
GET /status/https%3A%2F%2Fexample.com
```

**Response**

```json
{
  "url": "https://example.com",
  "found": true,
  "record": {
    "url": "https://example.com",
    "content_type": "static",
    "antiscraping_protection": "none",
    "scraping_strategy": "static",
    "classification_confidence": 0.85,
    "last_checked": "2025-03-13T10:00:00+00:00",
    "last_scrape_status": "success",
    "last_success_html": null
  }
}
```

(`last_success_html` is always `null` in status responses; use `/scrape` to get HTML.)

---

### GET /health

```json
{
  "status": "ok",
  "database": "ok",
  "tor_reachable": true
}
```

---

## 8. Security Model

### Transport security

- The server **binds to `127.0.0.1` only**. It is never accessible over the network.
- `CORS` is restricted to `http://127.0.0.1:*` and `http://localhost:*`.

### Authentication

Every request must include:

```
X-API-KEY: <your-key>
```

The dependency (`security.py`) performs:
1. **Localhost origin check** — rejects any request from a non-loopback IP with HTTP 403.
2. **Constant-time key comparison** — uses `hmac.compare_digest` to prevent timing attacks.
3. **Missing key → HTTP 401** with `WWW-Authenticate: ApiKey`.

### Key management

Set `SCRAPER_API_KEY` in the environment before starting:

```powershell
$env:SCRAPER_API_KEY = "your-long-random-secret"
python app.py
```

The default value `change-me-immediately` will work but is insecure for production use.

---

## 9. Background Scheduler

`scheduler.py` runs as a persistent asyncio background task.

### Cycle (default: every 3600 seconds)

1. **Low-confidence URLs** — fetch all URLs with `classification_confidence < 0.6`.
   Re-classify each one and persist updated metadata.
   
2. **Stale metadata** — fetch all URLs with `last_checked` older than 24 hours.
   Re-classify each one to detect site behaviour changes.

A 2-second pause between individual URL classifications prevents the scheduler
from acting like a scraper itself.

### Configuration

| Env var | Default | Description |
|---|---|---|
| `SCHEDULER_INTERVAL_SECONDS` | `3600` | Seconds between scheduler cycles |
| `METADATA_MAX_AGE_HOURS` | `24` | Re-classify records older than this |
| `LOW_CONFIDENCE_THRESHOLD` | `0.6` | Reclassify below this confidence |

---

## 10. Setup Instructions

### Prerequisites

- Python 3.11+
- Google Chrome (for `browser` / `hybrid` strategies)
- Geckodriver + Firefox (for `hybrid` Tor-Firefox strategy)
- Tor running locally (for `tor` / `hybrid` strategies)

### Install

```powershell
cd "C:\Users\ayavu\OneDrive\Belgeler\My Repos\scraper_api"
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

### Configure environment

```powershell
# Required — change this to a strong random string
$env:SCRAPER_API_KEY = "my-secret-key-32chars-min"

# Optional — paths and ports
$env:SCRAPER_DB_PATH = "scraper_metadata.db"
$env:SCRAPER_HOST    = "127.0.0.1"
$env:SCRAPER_PORT    = "8000"

# Optional — Tor
$env:TOR_SOCKS_PORT         = "9050"
$env:TOR_CONTROL_PORT       = "9051"
$env:TOR_CONTROL_PASSWORD   = ""         # leave empty for cookie auth
```

### Start Tor (optional, for tor/hybrid strategies)

Install Tor Browser or the Tor system package, then start it:

```powershell
# System Tor (if installed via chocolatey: choco install tor)
tor

# Or start Tor Browser manually — it exposes SOCKS on 9150
```

### Run the API

```powershell
python app.py
```

The API will listen on `http://127.0.0.1:8000`.

### Verify

```powershell
$headers = @{ "X-API-KEY" = "my-secret-key-32chars-min" }
Invoke-RestMethod -Uri "http://127.0.0.1:8000/health" -Headers $headers
```

---

## Calling from your Python application

```python
import httpx

API_BASE = "http://127.0.0.1:8000"
API_KEY  = "my-secret-key-32chars-min"
HEADERS  = {"X-API-KEY": API_KEY}

# Scrape a URL
with httpx.Client(base_url=API_BASE, headers=HEADERS, timeout=120) as client:
    response = client.post("/scrape", json={"url": "https://example.com"})
    result = response.json()

    if result["scraping_success"]:
        html = result["html"]
        strategy = result["strategy_used"]
        print(f"Scraped with strategy: {strategy}")
    else:
        print(f"Failed: {result['message']}")

# Classify only
with httpx.Client(base_url=API_BASE, headers=HEADERS, timeout=60) as client:
    response = client.post("/classify", json={"url": "https://example.com"})
    classification = response.json()["classification"]
    print(f"Strategy: {classification['scraping_strategy']}")
    print(f"Confidence: {classification['classification_confidence']}")
```

### Example scrape response (full)

```json
{
  "url": "https://example.com",
  "scraping_success": true,
  "message": "Scraped successfully.",
  "html": "<!doctype html><html>...</html>",
  "classification": {
    "content_type": "static",
    "antiscraping_protection": "none",
    "tor_network_available": "yes",
    "undetected_browser_available": "yes",
    "is_public_page": "yes",
    "scraping_strategy": "static",
    "classification_confidence": 0.867
  },
  "cached": false,
  "strategy_used": "static"
}
```

---

## 11. Configuration Reference

All settings are controlled by environment variables.

| Variable | Default | Description |
|---|---|---|
| `SCRAPER_API_KEY` | `change-me-immediately` | X-API-KEY value required on every request |
| `SCRAPER_HOST` | `127.0.0.1` | Bind address — **do not change to 0.0.0.0** |
| `SCRAPER_PORT` | `8000` | Listen port |
| `SCRAPER_DB_PATH` | `scraper_metadata.db` | SQLite database file path |
| `TOR_SOCKS_HOST` | `127.0.0.1` | Tor SOCKS5 proxy host |
| `TOR_SOCKS_PORT` | `9050` | Tor SOCKS5 proxy port |
| `TOR_CONTROL_PORT` | `9051` | Tor control port for NEWNYM |
| `TOR_CONTROL_PASSWORD` | _(empty)_ | Tor control password; empty = cookie auth |
| `CACHE_TTL_SECONDS` | `600` | Cache freshness window (10 minutes) |
| `LOW_CONFIDENCE_THRESHOLD` | `0.6` | Trigger reclassification below this score |
| `RETRY_COUNT` | `3` | Scrape attempts per request |
| `DOMAIN_RATE_LIMIT_SECONDS` | `2.0` | Min seconds between requests to same domain |
| `HEADLESS_BROWSER` | `true` | Run Chrome/Firefox headless |
| `REQUEST_TIMEOUT` | `30.0` | HTTP request timeout (seconds) |
| `CLASSIFICATION_TIMEOUT` | `20.0` | Per-probe classification timeout (seconds) |
| `SCHEDULER_INTERVAL_SECONDS` | `3600` | Background scheduler cycle interval |
| `METADATA_MAX_AGE_HOURS` | `24` | Refresh classification older than this |

---

## 12. Extension Guide

### Adding a new fingerprint profile

Add a `FingerprintProfile` entry to `_PROFILES` in `fingerprint.py`:

```python
FingerprintProfile(
    name="desktop_edge_windows",
    user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36 Edg/122.0.0.0",
    platform="Win32",
    locale="en-US",
    timezone="America/Denver",
    viewport_width=1680,
    viewport_height=1050,
    webgl_vendor="Google Inc. (Intel)",
    webgl_renderer="ANGLE (Intel, Intel(R) Iris(R) Xe Graphics, D3D11)",
    device_memory=16,
    hardware_concurrency=12,
    accept_language="en-US,en;q=0.9",
    sec_ch_ua='"Microsoft Edge";v="122", "Chromium";v="122", "Not:A-Brand";v="99"',
    sec_ch_ua_platform='"Windows"',
    mobile=False,
)
```

### Adding a new classification signal

1. Add a detection function in `classifier.py` following the pattern of existing
   `_detect_*` functions.  Return `(value, confidence_float)`.
2. Call it in `classify_url()` and include the confidence in `_aggregate_confidence()`.

### Adding a new scraping strategy

1. Add the enum value to `ScrapingStrategy` in `models.py`.
2. Add the dispatch branch in `scraper.py → _dispatch()`.
3. Update the decision tree in `strategy.py → determine_strategy()`.

### Replacing SQLite with PostgreSQL

1. Replace `aiosqlite` with `asyncpg` or `databases[asyncpg]`.
2. Rewrite the SQL in `database.py` (minor dialect differences: `ON CONFLICT` → `ON CONFLICT DO UPDATE`, same syntax in PostgreSQL 9.5+).
3. Update `SCRAPER_DB_PATH` → `DATABASE_URL` and adjust `init_db()` accordingly.

### Running under a process manager (Windows)

```powershell
# Install NSSM (Non-Sucking Service Manager)
choco install nssm

nssm install ScraperAPI "C:\path\to\.venv\Scripts\python.exe" "app.py"
nssm set ScraperAPI AppDirectory "C:\Users\ayavu\OneDrive\Belgeler\My Repos\scraper_api"
nssm set ScraperAPI AppEnvironmentExtra "SCRAPER_API_KEY=my-secret-key"
nssm start ScraperAPI
```

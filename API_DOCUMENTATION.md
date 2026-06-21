# Scraper API — Integration Guide

This document is the contract for any service that calls the Scraper API. It covers authentication, the security model, every endpoint, and the scraping fallback chain.

## 1. Overview

- **Base URL**: `http://127.0.0.1:8000` (the server only binds to localhost; it is not reachable from outside the host).
- **Auth**: every endpoint requires the `X-API-KEY` header.
- **Content type**: all requests/responses are JSON.
- **Version**: 2.0.0.

```
X-API-KEY: <your-key>
Content-Type: application/json
```

## 2. Security model

The API is hardened against being used as an SSRF (Server-Side Request Forgery) pivot. Every `/scrape` call is checked **before** any provider is dispatched and **before** any credit is charged.

### 2.1 Allowed schemes

Only `http://` and `https://` are accepted. `file://`, `ftp://`, `data:`, `javascript:`, and any other scheme are rejected.

### 2.2 Blocked hosts and IP ranges

The following are always blocked, regardless of the allow list:

| Target | Note |
|---|---|
| `localhost` | literal hostname |
| `127.0.0.0/8` | loopback (covers `127.0.0.1`) |
| `0.0.0.0/8` | unspecified |
| `10.0.0.0/8` | RFC1918 private |
| `172.16.0.0/12` | RFC1918 private |
| `192.168.0.0/16` | RFC1918 private |
| `169.254.0.0/16` | link-local (covers the cloud metadata IP `169.254.169.254`) |
| `::1/128`, `fc00::/7`, `fe80::/10` | IPv6 loopback / unique-local / link-local equivalents |

The hostname is also DNS-resolved and **every** returned address is checked — a domain that resolves to a private IP is rejected even if the hostname itself looks public.

### 2.3 Allow / block list

A configurable domain-level allow/block list, stored in `data/url_lists.json` and managed via the `/admin/url-lists` endpoints (see §4.6) or by editing the file directly.

- **Block list**: always wins. A blocked domain (and its subdomains) is rejected even if it would otherwise be allowed.
- **Allow list**: if non-empty, *only* domains on it (and their subdomains) may be scraped. If empty, every domain is allowed except those on the block list.
- Matching is by exact host or subdomain suffix: an entry `example.com` also matches `sub.example.com`.
- Seeded once from `URL_ALLOWLIST` / `URL_BLOCKLIST` env vars on first startup (comma-separated domains); after that, the JSON file is the source of truth.

### 2.4 Redirect validation

Redirects are re-validated at every hop, not just the initial URL:
- The static and Tor providers (httpx-based) re-run the scheme/host/list check on every redirect request via an httpx request hook.
- The Chrome provider intercepts every document-level navigation (including server-side redirects of the main frame) via the Chrome DevTools `Fetch` domain and aborts any hop that fails the check.

### 2.5 Response size and timeout limits

| Limit | Default | Env var |
|---|---|---|
| Max response size | 20,000,000 bytes (20 MB) | `MAX_RESPONSE_SIZE_BYTES` |
| Max page load time (Chrome) | 45 s | `MAX_PAGE_LOAD_SECONDS` |
| Request timeout (static/Tor/Scrape.do/Zyte) | 30 s | `REQUEST_TIMEOUT` |

A response that exceeds the size cap aborts the attempt and the orchestrator moves to the next provider in the waterfall.

A kill switch, `SSRF_PROTECTION_ENABLED` (default `true`), exists for ops emergencies; it should not be disabled in normal operation.

## 3. Scraping fallback chain

`POST /scrape` walks a fixed 5-stage waterfall, stopping at the first stage that returns usable content:

```
1. static     -- plain HTTP GET (httpx), no JS rendering
   |  fails
   v
2. browser    -- Chrome via CDP, full JS rendering
   |  fails
   v
3. tor        -- HTTP GET over the Tor SOCKS5 proxy
   |  fails
   v
4. scrape_do  -- Scrape.do proxy API
   |  fails
   v
5. zyte       -- Zyte Extract API
   |  fails
   v
   failure
```

Each stage is retried up to `RETRY_COUNT` times (default 3) with exponential backoff before falling through to the next stage. A caller can skip the waterfall and force a specific stage via `force_strategy`.

Every attempt — success or failure — is written to the database (`scrape_log` table): URL, success flag, provider, status, cost, error reason, duration, and response size.

### 3.1 Cost per provider

`cost_score` in the response is the real cost (USD-equivalent) charged for the request, deducted from the credit balance. These are placeholder figures — tune them in `config.py` / via env vars to your actual provider pricing.

| Provider | Cost | Env var |
|---|---|---|
| static | $0.001 | `COST_STATIC` |
| browser | $0.01 | `COST_BROWSER` |
| tor | $0.02 | `COST_TOR` |
| scrape_do | $0.05 | `COST_SCRAPE_DO` |
| zyte | $0.08 | `COST_ZYTE` |

A **failed** request (every stage exhausted) is not charged.

## 4. Endpoints

### 4.1 `POST /scrape`

Scrape a URL through the fallback waterfall.

**Request**
```json
{
  "url": "https://example.com",
  "force_strategy": null
}
```
- `url` (string, required) — must start with `http://` or `https://`.
- `force_strategy` (string, optional) — one of `static`, `browser`, `tor`, `scrape_do`, `zyte`. Skips the waterfall and uses only this stage.

**Response — `200 OK`**
```json
{
  "url": "https://example.com",
  "scraping_success": true,
  "message": "Scraped successfully.",
  "html": "<html>...</html>",
  "strategy_used": "browser",
  "provider": "browser",
  "status": "success",
  "cost_score": 0.01,
  "error_reason": null,
  "credits_remaining": 9999.99
}
```

| Field | Type | Meaning |
|---|---|---|
| `scraping_success` | bool | Whether any stage returned usable content |
| `html` | string\|null | Page HTML, present only on success |
| `provider` | string\|null | Which stage produced the result (`static`/`browser`/`tor`/`scrape_do`/`zyte`), or `null` on total failure |
| `strategy_used` | string\|null | Same value as `provider`, kept for backward compatibility |
| `status` | string | `success` \| `failed` |
| `cost_score` | number | Real cost charged for this request (0 on failure) |
| `error_reason` | string\|null | Last error encountered, present when `scraping_success` is false |
| `credits_remaining` | number | Credit balance after this request |

**Error responses**

| Status | When |
|---|---|
| `400` | URL fails the SSRF guard (blocked scheme, blocked IP/host, or not on the allow list / on the block list) |
| `401` | Missing or invalid `X-API-KEY` |
| `402` | Credit balance is 0 |
| `403` | Request did not originate from localhost |
| `422` | Malformed request body (e.g. URL doesn't start with `http(s)://`, invalid `force_strategy`) |
| `500` | Unhandled server error |

A `400` response never deducts credits and never reaches a provider.

### 4.2 `GET /status/{url}`

Return the last stored metadata for a URL (URL must be percent-encoded in the path).

**Response**
```json
{
  "url": "https://example.com",
  "found": true,
  "record": {
    "url": "https://example.com",
    "scraping_strategy": "browser",
    "last_checked": "2026-06-20T12:00:00+00:00",
    "last_scrape_status": "success",
    "last_provider": "browser",
    "last_cost": 0.01,
    "last_error_reason": null
  }
}
```
`found: false` (still `200 OK`) when the URL has no record. Cacheable for 60 s (`Cache-Control: private, max-age=60`).

### 4.3 `GET /health`

Liveness/readiness probe — database status and Tor connectivity diagnostics. No SSRF/credit logic involved.

### 4.4 `GET /credits`

```json
{ "balance": 9999.99, "granted": 10000.0, "used": 0.01 }
```

### 4.5 Feedback endpoints

- `POST /feedback` — `{ "url", "comment", "strategy_used"?, "scrape_success"? }`
- `GET /feedback?url=...` — list all, or filtered by URL
- `DELETE /feedback/{id}` — delete one
- `DELETE /feedback` — delete all

### 4.6 Allow/block list admin endpoints

- `GET /admin/url-lists` → `{ "allowlist": [...], "blocklist": [...] }`
- `POST /admin/url-lists/{list_name}` (`list_name` = `allow` or `block`), body `{ "domain": "example.com" }` → adds the domain, returns the updated lists.
- `DELETE /admin/url-lists/{list_name}/{domain}` → removes the domain, returns the updated lists.

Changes take effect immediately and persist to `data/url_lists.json`; no restart required.

## 5. Example usage

```bash
curl -X POST http://127.0.0.1:8000/scrape \
  -H "X-API-KEY: $SCRAPER_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"url": "https://example.com"}'

curl -X POST http://127.0.0.1:8000/admin/url-lists/block \
  -H "X-API-KEY: $SCRAPER_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"domain": "spam-site.example"}'
```

```python
import httpx

client = httpx.Client(base_url="http://127.0.0.1:8000", headers={"X-API-KEY": "..."})
resp = client.post("/scrape", json={"url": "https://example.com"})
data = resp.json()
print(data["provider"], data["status"], data["cost_score"])
```

## 6. Configuration reference (env vars)

| Var | Default | Purpose |
|---|---|---|
| `SCRAPER_API_KEY` | `change-me-immediately` | `X-API-KEY` value |
| `MAX_RESPONSE_SIZE_BYTES` | `20000000` | Response size cap |
| `MAX_PAGE_LOAD_SECONDS` | `45.0` | Chrome page-load timeout |
| `SSRF_PROTECTION_ENABLED` | `true` | Kill switch for the SSRF guard |
| `URL_ALLOWLIST` / `URL_BLOCKLIST` | empty | Comma-separated seed domains (first run only) |
| `SCRAPE_DO_API_KEY` / `SCRAPE_DO_BASE_URL` | empty / `https://api.scrape.do` | Scrape.do provider |
| `ZYTE_API_KEY` / `ZYTE_BASE_URL` | empty / `https://api.zyte.com/v1/extract` | Zyte provider |
| `COST_STATIC` / `COST_BROWSER` / `COST_TOR` / `COST_SCRAPE_DO` / `COST_ZYTE` | see §3.1 | Per-provider cost |

See `config.py` for the full list (server, database, Tor, rate limiting, etc.).

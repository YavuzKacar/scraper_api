"""
test_api.py -- Integration tests for the Scraper API.

Requirements:
    pip install pytest requests

Usage:
    # Start the server first:
    #   python app.py
    #
    # Then run tests:
    #   pytest test_api.py -v
    #
    # Override settings via env vars:
    #   SCRAPER_API_KEY=my-key BASE_URL=http://127.0.0.1:8000 pytest test_api.py -v
"""

import os
import urllib.parse

import pytest
import requests

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

BASE_URL = os.getenv("BASE_URL", "http://127.0.0.1:8000")
API_KEY  = os.getenv("SCRAPER_API_KEY", "change-me-immediately")
TEST_URL = "http://example.com"
HEADERS  = {"X-API-KEY": API_KEY}


def url(path: str) -> str:
    return BASE_URL.rstrip("/") + path


# ---------------------------------------------------------------------------
# Startup fixture
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session", autouse=True)
def server_is_up():
    """Abort the whole session if the server is not reachable."""
    try:
        r = requests.get(url("/health"), headers=HEADERS, timeout=5)
        r.raise_for_status()
    except Exception as exc:
        pytest.exit(
            f"Server at {BASE_URL} is not reachable -- start it before running tests.\n"
            f"Error: {exc}",
            returncode=1,
        )


# ===========================================================================
# GET /health
# ===========================================================================

class TestHealth:
    def test_returns_200(self):
        r = requests.get(url("/health"), headers=HEADERS)
        assert r.status_code == 200

    def test_response_schema(self):
        body = requests.get(url("/health"), headers=HEADERS).json()
        for field in ("status", "database", "tor_reachable"):
            assert field in body

    def test_status_is_string(self):
        assert isinstance(requests.get(url("/health"), headers=HEADERS).json()["status"], str)

    def test_database_ok(self):
        assert requests.get(url("/health"), headers=HEADERS).json()["database"] == "ok"

    def test_tor_reachable_is_bool(self):
        assert isinstance(requests.get(url("/health"), headers=HEADERS).json()["tor_reachable"], bool)

    def test_missing_key_returns_401(self):
        assert requests.get(url("/health")).status_code == 401

    def test_wrong_key_returns_401(self):
        assert requests.get(url("/health"), headers={"X-API-KEY": "wrong"}).status_code == 401

    def test_401_has_www_authenticate(self):
        assert "WWW-Authenticate" in requests.get(url("/health")).headers


# ===========================================================================
# POST /scrape
# ===========================================================================

class TestScrape:
    def test_returns_200(self):
        r = requests.post(url("/scrape"), headers=HEADERS, json={"url": TEST_URL})
        assert r.status_code == 200

    def test_response_schema(self):
        body = requests.post(url("/scrape"), headers=HEADERS, json={"url": TEST_URL}).json()
        for field in ("url", "scraping_success", "message"):
            assert field in body

    def test_url_echoed_back(self):
        r = requests.post(url("/scrape"), headers=HEADERS, json={"url": TEST_URL})
        assert r.json()["url"] == TEST_URL

    def test_scraping_success_is_bool(self):
        r = requests.post(url("/scrape"), headers=HEADERS, json={"url": TEST_URL})
        assert isinstance(r.json()["scraping_success"], bool)

    def test_message_is_string(self):
        r = requests.post(url("/scrape"), headers=HEADERS, json={"url": TEST_URL})
        assert isinstance(r.json()["message"], str)

    def test_successful_scrape_returns_html(self):
        r = requests.post(url("/scrape"), headers=HEADERS, json={"url": TEST_URL})
        body = r.json()
        if body["scraping_success"]:
            assert body.get("html") and len(body["html"]) > 0

    def test_strategy_used_valid_when_present(self):
        body = requests.post(url("/scrape"), headers=HEADERS, json={"url": TEST_URL}).json()
        if body.get("strategy_used"):
            assert body["strategy_used"] in {"static", "browser", "tor"}

    def test_invalid_url_returns_422(self):
        assert requests.post(url("/scrape"), headers=HEADERS, json={"url": "not-a-url"}).status_code == 422

    def test_non_http_scheme_returns_422(self):
        assert requests.post(url("/scrape"), headers=HEADERS, json={"url": "ftp://example.com"}).status_code == 422

    def test_missing_url_returns_422(self):
        assert requests.post(url("/scrape"), headers=HEADERS, json={}).status_code == 422

    def test_missing_key_returns_401(self):
        assert requests.post(url("/scrape"), json={"url": TEST_URL}).status_code == 401

    def test_wrong_key_returns_401(self):
        assert requests.post(url("/scrape"), headers={"X-API-KEY": "bad"}, json={"url": TEST_URL}).status_code == 401

    def test_force_strategy_browser(self):
        r = requests.post(url("/scrape"), headers=HEADERS, json={"url": TEST_URL, "force_strategy": "browser"})
        assert r.status_code == 200

    def test_force_strategy_invalid_returns_422(self):
        r = requests.post(url("/scrape"), headers=HEADERS, json={"url": TEST_URL, "force_strategy": "hybrid"})
        assert r.status_code == 422


# ===========================================================================
# GET /status/{url}
# ===========================================================================

class TestStatus:
    @pytest.fixture(autouse=True)
    def ensure_record(self):
        requests.post(url("/scrape"), headers=HEADERS, json={"url": TEST_URL})

    def test_returns_200_for_known_url(self):
        encoded = urllib.parse.quote(TEST_URL, safe="")
        r = requests.get(url(f"/status/{encoded}"), headers=HEADERS)
        assert r.status_code == 200

    def test_found_true_for_known_url(self):
        encoded = urllib.parse.quote(TEST_URL, safe="")
        assert requests.get(url(f"/status/{encoded}"), headers=HEADERS).json()["found"] is True

    def test_record_returned(self):
        encoded = urllib.parse.quote(TEST_URL, safe="")
        body = requests.get(url(f"/status/{encoded}"), headers=HEADERS).json()
        assert body.get("record") is not None

    def test_no_html_in_record(self):
        encoded = urllib.parse.quote(TEST_URL, safe="")
        record = requests.get(url(f"/status/{encoded}"), headers=HEADERS).json().get("record", {})
        assert record.get("last_success_html") is None

    def test_url_echoed_back(self):
        encoded = urllib.parse.quote(TEST_URL, safe="")
        assert requests.get(url(f"/status/{encoded}"), headers=HEADERS).json()["url"] == TEST_URL

    def test_unknown_url_found_false(self):
        ghost = urllib.parse.quote("http://this-url-does-not-exist-xyz.example.com", safe="")
        r = requests.get(url(f"/status/{ghost}"), headers=HEADERS)
        assert r.status_code == 200
        assert r.json()["found"] is False

    def test_missing_key_returns_401(self):
        encoded = urllib.parse.quote(TEST_URL, safe="")
        assert requests.get(url(f"/status/{encoded}")).status_code == 401


# ===========================================================================
# General HTTP behaviour
# ===========================================================================

class TestGeneral:
    def test_health_returns_json(self):
        r = requests.get(url("/health"), headers=HEADERS)
        assert "application/json" in r.headers.get("content-type", "")

    def test_scrape_returns_json(self):
        r = requests.post(url("/scrape"), headers=HEADERS, json={"url": TEST_URL})
        assert "application/json" in r.headers.get("content-type", "")

    def test_unknown_route_returns_404(self):
        assert requests.get(url("/does-not-exist"), headers=HEADERS).status_code == 404

    def test_get_scrape_returns_405(self):
        assert requests.get(url("/scrape"), headers=HEADERS).status_code == 405

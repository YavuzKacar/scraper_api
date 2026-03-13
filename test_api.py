"""
test_api.py — Integration tests for the Scraper API.

Requirements:
    pip install pytest requests

Usage:
    # Start the server first:
    #   python app.py   (or: uvicorn app:app --host 127.0.0.1 --port 8000)
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
API_KEY = os.getenv("SCRAPER_API_KEY", "change-me-immediately")

# A stable, publicly accessible URL used for live scrape / classify tests.
TEST_URL = "http://example.com"

HEADERS = {"X-API-KEY": API_KEY}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def url(path: str) -> str:
    return BASE_URL.rstrip("/") + path


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session", autouse=True)
def server_is_up():
    """Abort the whole session if the server is not reachable."""
    try:
        r = requests.get(url("/health"), headers=HEADERS, timeout=5)
        r.raise_for_status()
    except Exception as exc:
        pytest.exit(
            f"Server at {BASE_URL} is not reachable — start it before running tests.\n"
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
        r = requests.get(url("/health"), headers=HEADERS)
        body = r.json()
        assert "status" in body
        assert "database" in body
        assert "tor_reachable" in body

    def test_status_field_is_string(self):
        r = requests.get(url("/health"), headers=HEADERS)
        assert isinstance(r.json()["status"], str)

    def test_database_field_is_string(self):
        r = requests.get(url("/health"), headers=HEADERS)
        assert isinstance(r.json()["database"], str)

    def test_database_ok_when_healthy(self):
        r = requests.get(url("/health"), headers=HEADERS)
        assert r.json()["database"] == "ok"

    def test_tor_reachable_field_is_bool(self):
        r = requests.get(url("/health"), headers=HEADERS)
        assert isinstance(r.json()["tor_reachable"], bool)

    # --- Auth ---

    def test_missing_key_returns_401(self):
        r = requests.get(url("/health"))
        assert r.status_code == 401

    def test_wrong_key_returns_401(self):
        r = requests.get(url("/health"), headers={"X-API-KEY": "wrong-key"})
        assert r.status_code == 401

    def test_401_has_www_authenticate_header(self):
        r = requests.get(url("/health"))
        assert "WWW-Authenticate" in r.headers


# ===========================================================================
# POST /classify
# ===========================================================================

class TestClassify:
    def test_returns_200(self):
        r = requests.post(url("/classify"), headers=HEADERS, json={"url": TEST_URL})
        assert r.status_code == 200

    def test_response_schema(self):
        r = requests.post(url("/classify"), headers=HEADERS, json={"url": TEST_URL})
        body = r.json()
        assert "url" in body
        assert "classification" in body
        assert "from_cache" in body

    def test_url_echoed_back(self):
        r = requests.post(url("/classify"), headers=HEADERS, json={"url": TEST_URL})
        assert r.json()["url"] == TEST_URL

    def test_from_cache_is_bool(self):
        r = requests.post(url("/classify"), headers=HEADERS, json={"url": TEST_URL})
        assert isinstance(r.json()["from_cache"], bool)

    def test_classification_has_required_fields(self):
        r = requests.post(url("/classify"), headers=HEADERS, json={"url": TEST_URL})
        cls = r.json()["classification"]
        for field in (
            "content_type",
            "antiscraping_protection",
            "tor_network_available",
            "undetected_browser_available",
            "is_public_page",
            "scraping_strategy",
            "classification_confidence",
        ):
            assert field in cls, f"Missing field: {field}"

    def test_confidence_is_float_in_range(self):
        r = requests.post(url("/classify"), headers=HEADERS, json={"url": TEST_URL})
        conf = r.json()["classification"]["classification_confidence"]
        assert isinstance(conf, float)
        assert 0.0 <= conf <= 1.0

    def test_second_call_returns_from_cache(self):
        # First call populates cache; second should be cached.
        requests.post(url("/classify"), headers=HEADERS, json={"url": TEST_URL})
        r = requests.post(url("/classify"), headers=HEADERS, json={"url": TEST_URL})
        assert r.json()["from_cache"] is True

    def test_force_bypasses_cache(self):
        # Pre-populate cache.
        requests.post(url("/classify"), headers=HEADERS, json={"url": TEST_URL})
        r = requests.post(
            url("/classify"), headers=HEADERS, json={"url": TEST_URL, "force": True}
        )
        assert r.json()["from_cache"] is False

    def test_invalid_url_returns_422(self):
        r = requests.post(url("/classify"), headers=HEADERS, json={"url": "not-a-url"})
        assert r.status_code == 422

    def test_non_http_scheme_returns_422(self):
        r = requests.post(
            url("/classify"), headers=HEADERS, json={"url": "ftp://example.com"}
        )
        assert r.status_code == 422

    def test_missing_url_field_returns_422(self):
        r = requests.post(url("/classify"), headers=HEADERS, json={})
        assert r.status_code == 422

    def test_missing_key_returns_401(self):
        r = requests.post(url("/classify"), json={"url": TEST_URL})
        assert r.status_code == 401


# ===========================================================================
# POST /scrape
# ===========================================================================

class TestScrape:
    def test_returns_200(self):
        r = requests.post(url("/scrape"), headers=HEADERS, json={"url": TEST_URL})
        assert r.status_code == 200

    def test_response_schema(self):
        r = requests.post(url("/scrape"), headers=HEADERS, json={"url": TEST_URL})
        body = r.json()
        for field in ("url", "scraping_success", "message", "cached"):
            assert field in body, f"Missing field: {field}"

    def test_url_echoed_back(self):
        r = requests.post(url("/scrape"), headers=HEADERS, json={"url": TEST_URL})
        assert r.json()["url"] == TEST_URL

    def test_scraping_success_is_bool(self):
        r = requests.post(url("/scrape"), headers=HEADERS, json={"url": TEST_URL})
        assert isinstance(r.json()["scraping_success"], bool)

    def test_cached_is_bool(self):
        r = requests.post(url("/scrape"), headers=HEADERS, json={"url": TEST_URL})
        assert isinstance(r.json()["cached"], bool)

    def test_message_is_string(self):
        r = requests.post(url("/scrape"), headers=HEADERS, json={"url": TEST_URL})
        assert isinstance(r.json()["message"], str)

    def test_successful_scrape_returns_html(self):
        r = requests.post(url("/scrape"), headers=HEADERS, json={"url": TEST_URL})
        body = r.json()
        if body["scraping_success"]:
            assert "html" in body
            assert isinstance(body["html"], str)
            assert len(body["html"]) > 0

    def test_second_call_uses_cache(self):
        # First call — not cached.
        requests.post(url("/scrape"), headers=HEADERS, json={"url": TEST_URL})
        # Second call within TTL — should be cached.
        r = requests.post(url("/scrape"), headers=HEADERS, json={"url": TEST_URL})
        assert r.json()["cached"] is True

    def test_force_reclassify_flag_accepted(self):
        r = requests.post(
            url("/scrape"),
            headers=HEADERS,
            json={"url": TEST_URL, "force_reclassify": True},
        )
        assert r.status_code == 200

    def test_strategy_used_field_when_present(self):
        r = requests.post(url("/scrape"), headers=HEADERS, json={"url": TEST_URL})
        body = r.json()
        if "strategy_used" in body:
            valid_strategies = {"static", "browser", "tor", "hybrid", "blocked"}
            assert body["strategy_used"] in valid_strategies

    def test_classification_field_when_present(self):
        r = requests.post(url("/scrape"), headers=HEADERS, json={"url": TEST_URL})
        body = r.json()
        if "classification" in body and body["classification"] is not None:
            assert "scraping_strategy" in body["classification"]

    def test_invalid_url_returns_422(self):
        r = requests.post(url("/scrape"), headers=HEADERS, json={"url": "not-a-url"})
        assert r.status_code == 422

    def test_non_http_scheme_returns_422(self):
        r = requests.post(
            url("/scrape"), headers=HEADERS, json={"url": "ftp://example.com"}
        )
        assert r.status_code == 422

    def test_missing_url_field_returns_422(self):
        r = requests.post(url("/scrape"), headers=HEADERS, json={})
        assert r.status_code == 422

    def test_missing_key_returns_401(self):
        r = requests.post(url("/scrape"), json={"url": TEST_URL})
        assert r.status_code == 401

    def test_wrong_key_returns_401(self):
        r = requests.post(
            url("/scrape"),
            headers={"X-API-KEY": "bad-key"},
            json={"url": TEST_URL},
        )
        assert r.status_code == 401


# ===========================================================================
# GET /status/{url}
# ===========================================================================

class TestStatus:
    @pytest.fixture(autouse=True)
    def ensure_record_exists(self):
        """Make sure TEST_URL has a record before status tests run."""
        requests.post(url("/scrape"), headers=HEADERS, json={"url": TEST_URL})

    def test_returns_200_for_known_url(self):
        encoded = urllib.parse.quote(TEST_URL, safe="")
        r = requests.get(url(f"/status/{encoded}"), headers=HEADERS)
        assert r.status_code == 200

    def test_response_schema_for_known_url(self):
        encoded = urllib.parse.quote(TEST_URL, safe="")
        r = requests.get(url(f"/status/{encoded}"), headers=HEADERS)
        body = r.json()
        assert "url" in body
        assert "found" in body

    def test_found_true_for_known_url(self):
        encoded = urllib.parse.quote(TEST_URL, safe="")
        r = requests.get(url(f"/status/{encoded}"), headers=HEADERS)
        assert r.json()["found"] is True

    def test_record_returned_for_known_url(self):
        encoded = urllib.parse.quote(TEST_URL, safe="")
        r = requests.get(url(f"/status/{encoded}"), headers=HEADERS)
        body = r.json()
        assert "record" in body
        assert body["record"] is not None

    def test_record_does_not_contain_html(self):
        """The API strips last_success_html from the status response."""
        encoded = urllib.parse.quote(TEST_URL, safe="")
        r = requests.get(url(f"/status/{encoded}"), headers=HEADERS)
        record = r.json().get("record", {})
        assert "last_success_html" not in record or record.get("last_success_html") is None

    def test_url_echoed_back(self):
        encoded = urllib.parse.quote(TEST_URL, safe="")
        r = requests.get(url(f"/status/{encoded}"), headers=HEADERS)
        assert r.json()["url"] == TEST_URL

    def test_unknown_url_returns_200_with_found_false(self):
        ghost = urllib.parse.quote("http://this-url-does-not-exist-xyz.example.com", safe="")
        r = requests.get(url(f"/status/{ghost}"), headers=HEADERS)
        assert r.status_code == 200
        assert r.json()["found"] is False

    def test_unknown_url_has_no_record(self):
        ghost = urllib.parse.quote("http://this-url-does-not-exist-xyz.example.com", safe="")
        r = requests.get(url(f"/status/{ghost}"), headers=HEADERS)
        body = r.json()
        assert body.get("record") is None

    def test_missing_key_returns_401(self):
        encoded = urllib.parse.quote(TEST_URL, safe="")
        r = requests.get(url(f"/status/{encoded}"))
        assert r.status_code == 401

    def test_wrong_key_returns_401(self):
        encoded = urllib.parse.quote(TEST_URL, safe="")
        r = requests.get(url(f"/status/{encoded}"), headers={"X-API-KEY": "wrong"})
        assert r.status_code == 401


# ===========================================================================
# Content-type & general HTTP behaviour
# ===========================================================================

class TestGeneral:
    def test_health_content_type_is_json(self):
        r = requests.get(url("/health"), headers=HEADERS)
        assert "application/json" in r.headers.get("content-type", "")

    def test_classify_content_type_is_json(self):
        r = requests.post(url("/classify"), headers=HEADERS, json={"url": TEST_URL})
        assert "application/json" in r.headers.get("content-type", "")

    def test_scrape_content_type_is_json(self):
        r = requests.post(url("/scrape"), headers=HEADERS, json={"url": TEST_URL})
        assert "application/json" in r.headers.get("content-type", "")

    def test_non_existent_route_returns_404(self):
        r = requests.get(url("/does-not-exist"), headers=HEADERS)
        assert r.status_code == 404

    def test_method_not_allowed_on_scrape(self):
        r = requests.get(url("/scrape"), headers=HEADERS)
        assert r.status_code == 405

    def test_method_not_allowed_on_classify(self):
        r = requests.get(url("/classify"), headers=HEADERS)
        assert r.status_code == 405

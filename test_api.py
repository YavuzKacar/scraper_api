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
        for field in ("url", "scraping_success", "message", "provider", "status", "cost_score", "error_reason"):
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
            assert body["strategy_used"] in {"static", "browser", "tor", "scrape_do", "zyte"}

    def test_cost_score_is_number(self):
        body = requests.post(url("/scrape"), headers=HEADERS, json={"url": TEST_URL}).json()
        assert isinstance(body["cost_score"], (int, float))

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
# POST /scrape -- SSRF protection
# ===========================================================================

class TestSSRF:
    BLOCKED_URLS = [
        "http://127.0.0.1/",
        "http://localhost/",
        "http://0.0.0.0/",
        "http://169.254.169.254/",       # cloud metadata endpoint
        "http://10.0.0.5/",
        "http://172.16.0.5/",
        "http://192.168.1.5/",
    ]

    @pytest.mark.parametrize("blocked_url", BLOCKED_URLS)
    def test_blocked_ip_or_host_returns_400(self, blocked_url):
        r = requests.post(url("/scrape"), headers=HEADERS, json={"url": blocked_url})
        assert r.status_code == 400

    def test_non_http_scheme_rejected(self):
        # Caught by Pydantic's scheme prefix check before the SSRF guard even runs.
        r = requests.post(url("/scrape"), headers=HEADERS, json={"url": "file:///etc/passwd"})
        assert r.status_code == 422

    def test_blocked_request_does_not_deduct_credits(self):
        before = requests.get(url("/credits"), headers=HEADERS).json()["balance"]
        requests.post(url("/scrape"), headers=HEADERS, json={"url": "http://127.0.0.1/"})
        after = requests.get(url("/credits"), headers=HEADERS).json()["balance"]
        assert before == after


# ===========================================================================
# /admin/url-lists
# ===========================================================================

class TestAdminUrlLists:
    TEST_DOMAIN = "ssrf-test-domain.example"

    @pytest.fixture(autouse=True)
    def cleanup(self):
        yield
        requests.delete(url(f"/admin/url-lists/block/{self.TEST_DOMAIN}"), headers=HEADERS)

    def test_get_returns_both_lists(self):
        body = requests.get(url("/admin/url-lists"), headers=HEADERS).json()
        assert "allowlist" in body and "blocklist" in body

    def test_add_to_blocklist(self):
        r = requests.post(
            url("/admin/url-lists/block"), headers=HEADERS, json={"domain": self.TEST_DOMAIN}
        )
        assert r.status_code == 200
        assert self.TEST_DOMAIN in r.json()["blocklist"]

    def test_remove_from_blocklist(self):
        requests.post(url("/admin/url-lists/block"), headers=HEADERS, json={"domain": self.TEST_DOMAIN})
        r = requests.delete(url(f"/admin/url-lists/block/{self.TEST_DOMAIN}"), headers=HEADERS)
        assert r.status_code == 200
        assert self.TEST_DOMAIN not in r.json()["blocklist"]

    def test_blocked_domain_rejects_scrape(self):
        requests.post(url("/admin/url-lists/block"), headers=HEADERS, json={"domain": self.TEST_DOMAIN})
        r = requests.post(url("/scrape"), headers=HEADERS, json={"url": f"http://{self.TEST_DOMAIN}/"})
        assert r.status_code == 400

    def test_invalid_list_name_returns_400(self):
        r = requests.post(url("/admin/url-lists/bogus"), headers=HEADERS, json={"domain": "x.com"})
        assert r.status_code == 400


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

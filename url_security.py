"""
url_security.py — SSRF protection for the Scraper API.

Two checks, both invoked once per incoming request before any provider is
dispatched (see app.py::scrape_endpoint), plus a redirect hook reused by the
httpx-based providers so subsequent redirect hops are revalidated too.

    validate_url_scheme_and_literal(url)  — sync: scheme + literal host/IP
                                             + allow/block list.
    validate_resolved_ips(hostname)       — async: DNS-resolve and check
                                             every returned address.
    validate_url_for_scraping(url)        — async: runs both, in order.

Public API
----------
SSRFBlockedError
validate_url_for_scraping(url) -> None
httpx_redirect_validator_hook(request) -> None
"""
from __future__ import annotations

import asyncio
import ipaddress
import logging
import socket
from urllib.parse import urlparse

from config import CONFIG
from url_lists import is_allowed, is_blocked

logger = logging.getLogger(__name__)


class SSRFBlockedError(Exception):
    """Raised when a URL fails the SSRF / scheme / allow-block list guard."""


ALLOWED_SCHEMES: frozenset[str] = frozenset({"http", "https"})

# Literal hostnames blocked regardless of DNS resolution.
BLOCKED_HOSTNAMES: frozenset[str] = frozenset({"localhost"})

# Mandatory ranges from the spec, plus their IPv6 equivalents so a dual-stack
# DNS answer can't bypass the IPv4 list.
BLOCKED_NETWORKS: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...] = (
    ipaddress.ip_network("0.0.0.0/8"),        # "this network" / 0.0.0.0
    ipaddress.ip_network("10.0.0.0/8"),       # RFC1918
    ipaddress.ip_network("127.0.0.0/8"),      # loopback
    ipaddress.ip_network("169.254.0.0/16"),   # link-local, covers metadata IP 169.254.169.254
    ipaddress.ip_network("172.16.0.0/12"),    # RFC1918
    ipaddress.ip_network("192.168.0.0/16"),   # RFC1918
    ipaddress.ip_network("::1/128"),          # IPv6 loopback
    ipaddress.ip_network("fc00::/7"),         # IPv6 unique local
    ipaddress.ip_network("fe80::/10"),        # IPv6 link-local
)


def _is_blocked_ip(ip_str: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip_str)
    except ValueError:
        return False
    return any(addr in net for net in BLOCKED_NETWORKS)


def validate_url_scheme_and_literal(url: str) -> None:
    """
    Synchronous checks that don't require I/O:
      - scheme is http/https (rejects file://, ftp://, data:, javascript:, ...)
      - hostname is present
      - hostname/IP isn't a literal blocked value
      - hostname passes the allow/block list
    """
    parsed = urlparse(url)

    scheme = (parsed.scheme or "").lower()
    if scheme not in ALLOWED_SCHEMES:
        raise SSRFBlockedError(f"Scheme '{scheme}' is not allowed; only http/https.")

    host = (parsed.hostname or "").lower()
    if not host:
        raise SSRFBlockedError("URL has no hostname.")

    if host in BLOCKED_HOSTNAMES:
        raise SSRFBlockedError(f"Host '{host}' is blocked.")

    if _is_blocked_ip(host):
        raise SSRFBlockedError(f"Host '{host}' resolves to a blocked IP range.")

    if is_blocked(host):
        raise SSRFBlockedError(f"Host '{host}' is on the block list.")

    if not is_allowed(host):
        raise SSRFBlockedError(f"Host '{host}' is not on the allow list.")


async def validate_resolved_ips(hostname: str) -> None:
    """
    Resolve *hostname* and reject if any returned address falls inside a
    blocked range.  Checking every record (not just the first) defends
    against a DNS response that mixes a benign address with a private one.
    """
    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(hostname, None, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise SSRFBlockedError(f"DNS resolution failed for '{hostname}': {exc}") from exc

    resolved_ips = {info[4][0] for info in infos}
    for ip_str in resolved_ips:
        if _is_blocked_ip(ip_str):
            raise SSRFBlockedError(
                f"Host '{hostname}' resolves to blocked address {ip_str}."
            )


async def validate_url_for_scraping(url: str) -> None:
    """Run both the literal and DNS-resolution SSRF checks for *url*."""
    if not CONFIG.ssrf_protection_enabled:
        return
    validate_url_scheme_and_literal(url)
    host = urlparse(url).hostname or ""
    await validate_resolved_ips(host)


async def httpx_redirect_validator_hook(request) -> None:
    """
    httpx ``event_hooks={"request": [...]}`` callback.

    Must be ``async def`` -- httpx.AsyncClient unconditionally does
    ``await hook(request)`` for every "request" hook, even if the hook
    itself is synchronous. A plain ``def`` here returns None, and awaiting
    None raises "object NoneType can't be used in 'await' expression" on
    every single request (not just redirects), since this hook fires on the
    initial request too.

    httpx invokes this for the original request AND every internal redirect
    request it follows, so wiring this into a client gives redirect
    validation "for free" and narrows the DNS-rebinding TOCTOU window to a
    single hop. Only the literal/scheme/list checks run here (sync, no extra
    DNS round trip per hop) -- the full DNS-resolution check already ran
    once upfront in validate_url_for_scraping().
    """
    if not CONFIG.ssrf_protection_enabled:
        return
    try:
        validate_url_scheme_and_literal(str(request.url))
    except SSRFBlockedError as exc:
        logger.warning("Redirect blocked by SSRF guard: %s", exc)
        raise

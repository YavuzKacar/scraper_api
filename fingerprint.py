"""
fingerprint.py — Browser fingerprint profiles and randomised header builder.

Five predefined profiles cover the most common real-world user agents:
    desktop_chrome_windows
    desktop_chrome_linux
    desktop_firefox_windows
    mobile_chrome_android
    mobile_safari_ios

Public API
----------
get_random_profile()                  → FingerprintProfile
get_profile_by_name(name)             → FingerprintProfile
build_http_headers(profile)           → dict[str, str]
build_browser_js_overrides(profile)   → str   (JS snippet for CDP injection)
"""
from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Optional


# ── Data model ────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class FingerprintProfile:
    name: str
    user_agent: str
    platform: str
    locale: str
    timezone: str
    viewport_width: int
    viewport_height: int
    webgl_vendor: str
    webgl_renderer: str
    device_memory: int
    hardware_concurrency: int
    accept_language: str
    sec_ch_ua: str             # empty string for browsers that don't send it
    sec_ch_ua_platform: str
    mobile: bool


# ── Profile registry ──────────────────────────────────────────────────────────

_PROFILES: list[FingerprintProfile] = [
    FingerprintProfile(
        name="desktop_chrome_windows",
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/122.0.0.0 Safari/537.36"
        ),
        platform="Win32",
        locale="en-US",
        timezone="America/New_York",
        viewport_width=1920,
        viewport_height=1080,
        webgl_vendor="Google Inc. (Intel)",
        webgl_renderer=(
            "ANGLE (Intel, Intel(R) UHD Graphics 730 "
            "Direct3D11 vs_5_0 ps_5_0, D3D11)"
        ),
        device_memory=8,
        hardware_concurrency=8,
        accept_language="en-US,en;q=0.9",
        sec_ch_ua=(
            '"Chromium";v="122", "Not(A:Brand";v="24", '
            '"Google Chrome";v="122"'
        ),
        sec_ch_ua_platform='"Windows"',
        mobile=False,
    ),
    FingerprintProfile(
        name="desktop_chrome_linux",
        user_agent=(
            "Mozilla/5.0 (X11; Linux x86_64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/122.0.0.0 Safari/537.36"
        ),
        platform="Linux x86_64",
        locale="en-US",
        timezone="America/Chicago",
        viewport_width=1920,
        viewport_height=1080,
        webgl_vendor="Google Inc. (Mesa/X.org)",
        webgl_renderer="Mesa Intel(R) HD Graphics 620 (KBL GT2)",
        device_memory=8,
        hardware_concurrency=4,
        accept_language="en-US,en;q=0.9",
        sec_ch_ua=(
            '"Chromium";v="122", "Not(A:Brand";v="24", '
            '"Google Chrome";v="122"'
        ),
        sec_ch_ua_platform='"Linux"',
        mobile=False,
    ),
    FingerprintProfile(
        name="desktop_firefox_windows",
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:123.0) "
            "Gecko/20100101 Firefox/123.0"
        ),
        platform="Win32",
        locale="en-US",
        timezone="America/Los_Angeles",
        viewport_width=1440,
        viewport_height=900,
        webgl_vendor="Google Inc. (Intel)",
        webgl_renderer=(
            "ANGLE (Intel, Intel(R) HD Graphics 4600 "
            "Direct3D11 vs_5_0 ps_5_0)"
        ),
        device_memory=8,
        hardware_concurrency=8,
        accept_language="en-US,en;q=0.5",
        sec_ch_ua="",           # Firefox does not send sec-ch-ua
        sec_ch_ua_platform="",
        mobile=False,
    ),
    FingerprintProfile(
        name="mobile_chrome_android",
        user_agent=(
            "Mozilla/5.0 (Linux; Android 14; Pixel 8) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/122.0.6261.119 Mobile Safari/537.36"
        ),
        platform="Linux armv8l",
        locale="en-US",
        timezone="America/New_York",
        viewport_width=412,
        viewport_height=915,
        webgl_vendor="Qualcomm Technologies, Inc.",
        webgl_renderer="Adreno (TM) 740",
        device_memory=4,
        hardware_concurrency=8,
        accept_language="en-US,en;q=0.9",
        sec_ch_ua=(
            '"Chromium";v="122", "Not(A:Brand";v="24", '
            '"Google Chrome";v="122"'
        ),
        sec_ch_ua_platform='"Android"',
        mobile=True,
    ),
    FingerprintProfile(
        name="mobile_safari_ios",
        user_agent=(
            "Mozilla/5.0 (iPhone; CPU iPhone OS 17_3 like Mac OS X) "
            "AppleWebKit/605.1.15 (KHTML, like Gecko) "
            "Version/17.3 Mobile/15E148 Safari/604.1"
        ),
        platform="iPhone",
        locale="en-US",
        timezone="America/Chicago",
        viewport_width=390,
        viewport_height=844,
        webgl_vendor="Apple GPU",
        webgl_renderer="Apple GPU",
        device_memory=4,
        hardware_concurrency=6,
        accept_language="en-US,en;q=0.9",
        sec_ch_ua="",           # Safari does not send sec-ch-ua
        sec_ch_ua_platform="",
        mobile=True,
    ),
]

_PROFILE_MAP: dict[str, FingerprintProfile] = {p.name: p for p in _PROFILES}

# Randomised pool values ──────────────────────────────────────────────────────

_REFERERS: list[str] = [
    "https://www.google.com/",
    "https://www.bing.com/",
    "https://duckduckgo.com/",
    "https://www.yahoo.com/",
    "",   # direct / no referrer (most common)
    "",
    "",
]

_ACCEPT_ENCODINGS: list[str] = [
    "gzip, deflate, br",
    "gzip, deflate, br, zstd",
    "gzip, deflate",
]


# ── Public API ────────────────────────────────────────────────────────────────

def get_random_profile() -> FingerprintProfile:
    """Return a randomly chosen fingerprint profile."""
    return random.choice(_PROFILES)


def get_profile_by_name(name: str) -> FingerprintProfile:
    """Return the named profile; raises KeyError for unknown names."""
    return _PROFILE_MAP[name]


def build_http_headers(
    profile: FingerprintProfile,
    url: Optional[str] = None,
) -> dict[str, str]:
    """
    Build a realistic HTTP header dict from *profile*.

    Adds optional randomised Referer, Accept-Encoding, and
    Chromium-specific Client Hints when the profile supports them.
    """
    headers: dict[str, str] = {
        "User-Agent": profile.user_agent,
        "Accept": (
            "text/html,application/xhtml+xml,application/xml;"
            "q=0.9,image/avif,image/webp,*/*;q=0.8"
        ),
        "Accept-Language": profile.accept_language,
        "Accept-Encoding": random.choice(_ACCEPT_ENCODINGS),
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
        "DNT": "1",
    }

    referer = random.choice(_REFERERS)
    if referer:
        headers["Referer"] = referer

    # Chromium Client Hints (Chrome-based profiles only)
    if profile.sec_ch_ua:
        headers["sec-ch-ua"] = profile.sec_ch_ua
        headers["sec-ch-ua-mobile"] = "?1" if profile.mobile else "?0"
        headers["sec-ch-ua-platform"] = profile.sec_ch_ua_platform
        headers["Sec-Fetch-Dest"] = "document"
        headers["Sec-Fetch-Mode"] = "navigate"
        headers["Sec-Fetch-Site"] = "cross-site" if referer else "none"
        headers["Sec-Fetch-User"] = "?1"

    return headers


def build_browser_js_overrides(profile: FingerprintProfile) -> str:
    """
    Return a JavaScript snippet to inject via CDP (Page.addScriptToEvaluateOnNewDocument).

    Overrides navigator/WebGL properties to match *profile*, defeating
    basic headless-browser fingerprinting checks.
    """
    webgl_vendor = profile.webgl_vendor.replace("'", "\\'")
    webgl_renderer = profile.webgl_renderer.replace("'", "\\'")
    platform = profile.platform.replace("'", "\\'")
    locale = profile.locale.replace("'", "\\'")

    return f"""
(function() {{
  // Suppress webdriver flag
  Object.defineProperty(navigator, 'webdriver', {{ get: () => undefined }});

  // Platform and memory overrides
  Object.defineProperty(navigator, 'platform',
    {{ get: () => '{platform}' }});
  Object.defineProperty(navigator, 'deviceMemory',
    {{ get: () => {profile.device_memory} }});
  Object.defineProperty(navigator, 'hardwareConcurrency',
    {{ get: () => {profile.hardware_concurrency} }});
  Object.defineProperty(navigator, 'language',
    {{ get: () => '{locale}' }});
  Object.defineProperty(navigator, 'languages',
    {{ get: () => ['{locale}', 'en'] }});

  // WebGL vendor / renderer spoofing
  const _getParam = WebGLRenderingContext.prototype.getParameter;
  WebGLRenderingContext.prototype.getParameter = function(p) {{
    if (p === 37445) return '{webgl_vendor}';
    if (p === 37446) return '{webgl_renderer}';
    return _getParam.call(this, p);
  }};
  const _getParam2 = WebGL2RenderingContext.prototype.getParameter;
  WebGL2RenderingContext.prototype.getParameter = function(p) {{
    if (p === 37445) return '{webgl_vendor}';
    if (p === 37446) return '{webgl_renderer}';
    return _getParam2.call(this, p);
  }};

  // Plugins array — non-empty like a real browser
  Object.defineProperty(navigator, 'plugins', {{
    get: () => [
      {{ name: 'Chrome PDF Plugin' }},
      {{ name: 'Chrome PDF Viewer' }},
      {{ name: 'Native Client' }},
    ],
  }});
}})();
""".strip()

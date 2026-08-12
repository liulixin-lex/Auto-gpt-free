"""Attempt-scoped fingerprint bundles for ChatGPT registration.

Protocol registration uses a Chrome TLS/UA/Client-Hints bundle. Camoufox
registration uses a native Firefox profile descriptor and lets Camoufox build
the matching browser fingerprint instead of injecting Chrome-only headers.
"""
from __future__ import annotations

import random
import re
import secrets
from typing import Any


# curl_cffi impersonate targets that are widely available in recent builds.
CHROME_PROFILES: list[dict[str, Any]] = [
    {
        "key": "chrome131",
        "impersonate": "chrome131",
        "major": 131,
        "build": 6778,
        "patch_min": 69,
        "patch_max": 205,
        "sec_ch_ua": '"Google Chrome";v="131", "Chromium";v="131", "Not_A Brand";v="24"',
        "platform": "Windows",
    },
    {
        "key": "chrome133",
        "impersonate": "chrome133a",
        "major": 133,
        "build": 6943,
        "patch_min": 33,
        "patch_max": 140,
        "sec_ch_ua": '"Google Chrome";v="133", "Chromium";v="133", "Not_A Brand";v="24"',
        "platform": "Windows",
    },
    {
        "key": "chrome136",
        "impersonate": "chrome136",
        "major": 136,
        "build": 7103,
        "patch_min": 48,
        "patch_max": 120,
        "sec_ch_ua": '"Google Chrome";v="136", "Chromium";v="136", "Not_A Brand";v="24"',
        "platform": "Windows",
    },
    {
        "key": "chrome142",
        "impersonate": "chrome142",
        "major": 142,
        "build": 7444,
        "patch_min": 50,
        "patch_max": 180,
        "sec_ch_ua": '"Google Chrome";v="142", "Chromium";v="142", "Not_A Brand";v="24"',
        "platform": "Windows",
    },
    {
        "key": "chrome145",
        "impersonate": "chrome145",
        "major": 145,
        "build": 7632,
        "patch_min": 40,
        "patch_max": 160,
        "sec_ch_ua": '"Google Chrome";v="145", "Chromium";v="145", "Not_A Brand";v="24"',
        "platform": "Windows",
    },
    {
        "key": "chrome146",
        "impersonate": "chrome146",
        "major": 146,
        "build": 7680,
        "patch_min": 40,
        "patch_max": 160,
        "sec_ch_ua": '"Google Chrome";v="146", "Chromium";v="146", "Not_A Brand";v="24"',
        "platform": "Windows",
    },
]


def pick_chrome_profile(rng: random.Random | None = None) -> dict[str, Any]:
    """Return a full browser fingerprint bundle for one registration slot."""
    r = rng or random.SystemRandom()
    base = dict(r.choice(CHROME_PROFILES))
    major = int(base["major"])
    build = int(base["build"])
    patch = r.randint(int(base["patch_min"]), int(base["patch_max"]))
    full = f"{major}.0.{build}.{patch}"
    ua = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        f"Chrome/{full} Safari/537.36"
    )
    viewport = dict(r.choice(
        (
            {"width": 1366, "height": 768},
            {"width": 1440, "height": 900},
            {"width": 1536, "height": 864},
            {"width": 1600, "height": 900},
            {"width": 1920, "height": 1080},
        )
    ))
    return {
        "key": base["key"],
        "impersonate": base["impersonate"],
        "major": major,
        "full_version": full,
        "user_agent": ua,
        "sec_ch_ua": base["sec_ch_ua"],
        "sec_ch_ua_mobile": "?0",
        "sec_ch_ua_platform": f'"{base["platform"]}"',
        "platform": base["platform"],
        "accept_language": "en-US,en;q=0.9",
        "locale": "en-US",
        "timezone_id": "America/New_York",
        "viewport": viewport,
        "screen": {"width": viewport["width"], "height": viewport["height"]},
        "hardware_concurrency": r.choice((4, 8, 12)),
        "device_memory": r.choice((4, 8)),
    }


def align_chrome_profile_to_user_agent(
    profile: dict[str, Any],
    user_agent: str,
) -> dict[str, Any]:
    """Return a coherent curl/UA/Client-Hints bundle for a solver UA.

    FlareSolverr runs its own Chrome build.  A clearance cookie is only useful
    when the direct HTTP client presents the same browser major and Client
    Hints.  Unsupported majors are rejected instead of mutating only the UA.
    """
    ua = str(user_agent or "").strip()
    match = re.search(r"Chrome/(\d+)\.", ua)
    if not match:
        raise ValueError("AUTH_SESSION_DESYNC: FlareSolverr did not return a Chrome user agent")
    major = int(match.group(1))
    template = next(
        (item for item in CHROME_PROFILES if int(item["major"]) == major),
        None,
    )
    if template is None:
        raise ValueError(
            f"AUTH_SESSION_DESYNC: unsupported FlareSolverr Chrome major {major}"
        )
    tls_major = int(template["major"])
    out = dict(profile or {})
    out.update(
        {
            "key": template["key"],
            "impersonate": template["impersonate"],
            "major": major,
            "tls_profile_major": tls_major,
            "user_agent": ua,
            "sec_ch_ua": (
                f'"Google Chrome";v="{major}", "Chromium";v="{major}", '
                '"Not_A Brand";v="24"'
            ),
            "sec_ch_ua_mobile": "?0",
            "sec_ch_ua_platform": f'"{template["platform"]}"',
            "platform": template["platform"],
        }
    )
    return out


def pick_camoufox_profile(rng: random.Random | None = None) -> dict[str, Any]:
    """Return an attempt-local Camoufox descriptor without Chrome UA fields."""
    r = rng or random.SystemRandom()
    viewport = dict(
        r.choice(
            (
                {"width": 1366, "height": 768},
                {"width": 1440, "height": 900},
                {"width": 1536, "height": 864},
                {"width": 1600, "height": 900},
                {"width": 1920, "height": 1080},
            )
        )
    )
    fingerprint_id = f"camoufox-win-{secrets.token_hex(6)}"
    return {
        "key": "camoufox-firefox",
        "fingerprint_id": fingerprint_id,
        "engine": "camoufox",
        "browser_family": "firefox",
        "os": "windows",
        "accept_language": "en-US,en;q=0.9",
        "locale": "en-US",
        "viewport": viewport,
        "screen": dict(viewport),
    }


def navigate_headers(profile: dict[str, Any], *, referer: str = "") -> dict[str, str]:
    headers = {
        "accept": (
            "text/html,application/xhtml+xml,application/xml;q=0.9,"
            "image/avif,image/webp,image/apng,*/*;q=0.8"
        ),
        "accept-language": profile.get("accept_language") or "en-US,en;q=0.9",
        "cache-control": "no-cache",
        "pragma": "no-cache",
        "sec-ch-ua": profile.get("sec_ch_ua") or "",
        "sec-ch-ua-mobile": profile.get("sec_ch_ua_mobile") or "?0",
        "sec-ch-ua-platform": profile.get("sec_ch_ua_platform") or '"Windows"',
        "sec-fetch-dest": "document",
        "sec-fetch-mode": "navigate",
        "sec-fetch-site": "none" if not referer else "same-origin",
        "sec-fetch-user": "?1",
        "upgrade-insecure-requests": "1",
        "user-agent": profile.get("user_agent") or "",
    }
    if referer:
        headers["referer"] = referer
    return headers


def api_headers(
    profile: dict[str, Any],
    *,
    origin: str,
    referer: str,
    content_type: str = "application/json",
) -> dict[str, str]:
    return {
        "accept": "application/json",
        "accept-language": profile.get("accept_language") or "en-US,en;q=0.9",
        "content-type": content_type,
        "origin": origin,
        "referer": referer,
        "sec-ch-ua": profile.get("sec_ch_ua") or "",
        "sec-ch-ua-mobile": profile.get("sec_ch_ua_mobile") or "?0",
        "sec-ch-ua-platform": profile.get("sec_ch_ua_platform") or '"Windows"',
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin" if origin in referer else "same-site",
        "user-agent": profile.get("user_agent") or "",
    }

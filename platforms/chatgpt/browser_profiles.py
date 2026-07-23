"""Chrome TLS/UA/Client-Hints profile bundles for protocol registration.

Each registration slot should pick one profile and keep impersonate + UA +
sec-ch-ua consistent for the whole account lifecycle (industry best practice).
"""
from __future__ import annotations

import random
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

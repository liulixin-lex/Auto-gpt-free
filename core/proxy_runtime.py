"""Outbound runtime: optional proxy + Cloudflare clearance (FlareSolverr / manual).

Direct connection is always allowed (proxy URL may be empty).
Clearance is independent of having a proxy: FlareSolverr on the same host
can solve CF challenges on the server IP.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from core.config_store import config_store

_LOCK = threading.Lock()
_CLEARANCE_CACHE: dict[tuple[str, str], dict[str, Any]] = {}

# Compose service name — app container reaches FS here (not 127.0.0.1).
DEFAULT_FLARESOLVERR_URL = "http://flaresolverr:8191"

UPSTREAM_HOST_SUFFIXES = (
    "openai.com",
    "chatgpt.com",
    "oaistatic.com",
    "oaiusercontent.com",
)


def normalize_proxy_url(url: str) -> str:
    candidate = str(url or "").strip()
    if not candidate:
        return ""
    if "://" not in candidate:
        if candidate.count(":") == 1 and not candidate.startswith("["):
            candidate = f"http://{candidate}"
    lowered = candidate.lower()
    if lowered.startswith("socks://"):
        return "socks5h://" + candidate[len("socks://") :]
    if lowered.startswith("socks5://"):
        return "socks5h://" + candidate[len("socks5://") :]
    return candidate


def normalize_flaresolverr_url(url: str) -> str:
    candidate = str(url or "").strip().rstrip("/")
    if not candidate:
        return ""
    if "://" not in candidate:
        candidate = f"http://{candidate}"
    # Common mistake: host loopback from inside the app container.
    # Keep as-is if user really wants it; UI warns separately.
    return candidate


def _host_matches(host: str, suffix: str) -> bool:
    h = (host or "").lower().strip(".")
    s = (suffix or "").lower().strip(".")
    return h == s or h.endswith("." + s)


def is_upstream_host(host: str) -> bool:
    return any(_host_matches(host, s) for s in UPSTREAM_HOST_SUFFIXES)


def is_upstream_url(url: str) -> bool:
    try:
        host = urlparse(str(url or "")).hostname or ""
    except Exception:
        host = ""
    return is_upstream_host(host)


@dataclass
class ProxyRuntimeSettings:
    enabled: bool = False
    proxy_url: str = ""
    # upstream_only | all
    scope: str = "upstream_only"
    # none | manual | flaresolverr
    clearance_mode: str = "none"
    flaresolverr_url: str = ""
    clearance_cookie: str = ""
    clearance_user_agent: str = ""
    refresh_interval_sec: int = 300
    timeout_sec: int = 60
    skip_ssl_verify: bool = False

    @classmethod
    def load(cls) -> "ProxyRuntimeSettings":
        def g(key: str, default: str = "") -> str:
            return str(config_store.get(key, default) or default).strip()

        def truthy(key: str, default: str = "false") -> bool:
            return g(key, default).lower() in {"1", "true", "yes", "on"}

        try:
            interval = int(g("proxy_runtime_refresh_interval_sec", "300") or 300)
        except Exception:
            interval = 300
        try:
            timeout = int(g("proxy_runtime_timeout_sec", "60") or 60)
        except Exception:
            timeout = 60

        mode = (g("proxy_runtime_clearance_mode", "none") or "none").lower()
        fs_url = normalize_flaresolverr_url(g("proxy_runtime_flaresolverr_url", ""))
        # Sensible default when mode is flaresolverr but URL left blank (compose service).
        if mode == "flaresolverr" and not fs_url:
            fs_url = DEFAULT_FLARESOLVERR_URL

        return cls(
            enabled=truthy("proxy_runtime_enabled", "false"),
            proxy_url=normalize_proxy_url(g("proxy_runtime_proxy_url", "")),
            scope=(g("proxy_runtime_scope", "upstream_only") or "upstream_only").lower(),
            clearance_mode=mode,
            flaresolverr_url=fs_url,
            clearance_cookie=g("proxy_runtime_clearance_cookie", ""),
            clearance_user_agent=g("proxy_runtime_clearance_ua", ""),
            refresh_interval_sec=max(0, interval),
            timeout_sec=max(5, timeout),
            skip_ssl_verify=truthy("proxy_runtime_skip_ssl_verify", "false"),
        )

    def to_public_dict(self) -> dict[str, Any]:
        path = "direct"
        if self.enabled and self.proxy_url:
            path = "proxy"
        elif self.enabled:
            path = "direct"
        return {
            "enabled": self.enabled,
            "proxy_url": self.proxy_url,
            "has_proxy": bool(self.proxy_url),
            "egress_path": path,
            "scope": self.scope,
            "clearance_mode": self.clearance_mode,
            "flaresolverr_url": self.flaresolverr_url,
            "has_clearance_cookie": bool(self.clearance_cookie),
            "refresh_interval_sec": self.refresh_interval_sec,
            "timeout_sec": self.timeout_sec,
            "skip_ssl_verify": self.skip_ssl_verify,
            "default_flaresolverr_url": DEFAULT_FLARESOLVERR_URL,
        }


def resolve_proxy_for_url(
    url: str,
    *,
    explicit_proxy: str | None = None,
    pool_proxy: str | None = None,
) -> str | None:
    """Pick proxy for a request. Explicit > runtime (if scope matches) > pool > None(direct)."""
    explicit = normalize_proxy_url(explicit_proxy or "")
    if explicit:
        return explicit
    settings = ProxyRuntimeSettings.load()
    if settings.enabled and settings.proxy_url:
        if settings.scope == "all" or is_upstream_url(url):
            return settings.proxy_url
    pool = normalize_proxy_url(pool_proxy or "")
    return pool or None


def resolve_registration_proxy(
    *,
    explicit_proxy: str | None = None,
    pool_proxy: str | None = None,
) -> str | None:
    return resolve_proxy_for_url(
        "https://chatgpt.com/",
        explicit_proxy=explicit_proxy,
        pool_proxy=pool_proxy,
    )


def session_kwargs_for_url(
    url: str,
    *,
    explicit_proxy: str | None = None,
    pool_proxy: str | None = None,
    impersonate: str = "chrome136",
    timeout: float = 60,
) -> dict[str, Any]:
    proxy = resolve_proxy_for_url(url, explicit_proxy=explicit_proxy, pool_proxy=pool_proxy)
    settings = ProxyRuntimeSettings.load()
    kwargs: dict[str, Any] = {
        "impersonate": impersonate,
        "timeout": timeout,
    }
    if proxy:
        kwargs["proxies"] = {"http": proxy, "https": proxy}
    if settings.skip_ssl_verify:
        kwargs["verify"] = False
    return kwargs


def _cache_key(host: str, settings: ProxyRuntimeSettings) -> tuple[str, str]:
    return (settings.proxy_url or "direct", (host or "").lower())


def invalidate_clearance(host: str | None = None) -> None:
    """Drop cached clearance (all hosts, or one host)."""
    with _LOCK:
        if not host:
            _CLEARANCE_CACHE.clear()
            return
        h = host.lower()
        for key in list(_CLEARANCE_CACHE.keys()):
            if key[1] == h:
                _CLEARANCE_CACHE.pop(key, None)


def _cookie_header_from_solution(cookies: Any) -> str:
    parts: list[str] = []
    if isinstance(cookies, list):
        for c in cookies:
            if not isinstance(c, dict):
                continue
            name = str(c.get("name") or "")
            value = str(c.get("value") or "")
            if name:
                parts.append(f"{name}={value}")
    return "; ".join(parts)


def _get_flaresolverr_clearance(
    host: str,
    settings: ProxyRuntimeSettings,
    *,
    force: bool = False,
) -> dict[str, Any] | None:
    endpoint = (settings.flaresolverr_url or DEFAULT_FLARESOLVERR_URL).rstrip("/")
    if not endpoint or not host:
        return None
    proxy = settings.proxy_url or ""
    key = _cache_key(host, settings)
    now = time.time()
    if not force:
        with _LOCK:
            cached = _CLEARANCE_CACHE.get(key)
            if cached and (
                settings.refresh_interval_sec <= 0
                or now - float(cached.get("at") or 0) < settings.refresh_interval_sec
            ):
                return cached
    target = f"https://{host}/"
    try:
        import requests

        body: dict[str, Any] = {
            "cmd": "request.get",
            "url": target,
            "maxTimeout": max(settings.timeout_sec, 45) * 1000,
            "returnOnlyCookies": True,
        }
        if proxy:
            body["proxy"] = {"url": proxy}
        resp = requests.post(
            f"{endpoint}/v1",
            json=body,
            timeout=max(settings.timeout_sec, 45) + 15,
        )
        data = resp.json() if resp.ok else {}
        if not isinstance(data, dict) or str(data.get("status") or "").lower() not in {
            "ok",
            "",
        }:
            # Some versions use status ok only on success.
            if not resp.ok:
                return None
        solution = (data.get("solution") or {}) if isinstance(data, dict) else {}
        cookie_header = _cookie_header_from_solution(solution.get("cookies") or [])
        ua = str(solution.get("userAgent") or settings.clearance_user_agent or "")
        if not cookie_header:
            return None
        # Prefer cf_clearance presence when available.
        has_cf = "cf_clearance=" in cookie_header
        bundle = {
            "cookie": cookie_header,
            "user_agent": ua,
            "at": now,
            "has_cf_clearance": has_cf,
            "source": "flaresolverr",
            "host": host.lower(),
        }
        with _LOCK:
            _CLEARANCE_CACHE[key] = bundle
        return bundle
    except Exception:
        return None


def get_clearance_bundle(
    host: str = "chatgpt.com",
    *,
    force: bool = False,
) -> dict[str, Any] | None:
    """Return {cookie, user_agent, ...} for CF, or None."""
    settings = ProxyRuntimeSettings.load()
    if not settings.enabled or settings.clearance_mode in {"", "none"}:
        return None
    if settings.clearance_mode == "manual":
        if not settings.clearance_cookie:
            return None
        return {
            "cookie": settings.clearance_cookie,
            "user_agent": settings.clearance_user_agent or "",
            "at": time.time(),
            "has_cf_clearance": "cf_clearance=" in settings.clearance_cookie,
            "source": "manual",
            "host": host.lower(),
        }
    if settings.clearance_mode == "flaresolverr":
        return _get_flaresolverr_clearance(host, settings, force=force)
    return None


def clearance_headers_for_host(host: str, *, force: bool = False) -> dict[str, str]:
    """Optional CF clearance headers (manual cookie or FlareSolverr cache)."""
    bundle = get_clearance_bundle(host, force=force)
    if not bundle:
        return {}
    headers: dict[str, str] = {}
    if bundle.get("cookie"):
        headers["cookie"] = str(bundle["cookie"])
    if bundle.get("user_agent"):
        headers["user-agent"] = str(bundle["user_agent"])
    return headers


def apply_clearance_to_profile(
    profile: dict[str, Any],
    host: str = "chatgpt.com",
    *,
    force: bool = False,
) -> dict[str, Any]:
    """Mutate browser profile so UA matches clearance cookie (required by CF).

    Returns the clearance bundle used (or empty dict).
    """
    bundle = get_clearance_bundle(host, force=force) or {}
    ua = str(bundle.get("user_agent") or "").strip()
    if ua:
        profile["user_agent"] = ua
        # Align sec-ch-ua major with FS Chrome when possible.
        try:
            import re

            m = re.search(r"Chrome/(\d+)", ua)
            if m:
                major = m.group(1)
                profile["sec_ch_ua"] = (
                    f'"Google Chrome";v="{major}", "Chromium";v="{major}", "Not_A Brand";v="24"'
                )
        except Exception:
            pass
    if bundle.get("cookie"):
        profile["clearance_cookie"] = str(bundle["cookie"])
        profile["clearance_source"] = str(bundle.get("source") or "")
    return bundle


def merge_clearance_into_headers(
    headers: dict[str, str],
    host: str = "chatgpt.com",
    *,
    force: bool = False,
    profile: dict[str, Any] | None = None,
) -> dict[str, str]:
    """Return headers with cookie + UA from clearance; prefer profile UA if set."""
    out = dict(headers or {})
    clr = clearance_headers_for_host(host, force=force)
    if not clr:
        # Profile may already carry a bound clearance cookie from apply_clearance_to_profile.
        if profile and profile.get("clearance_cookie"):
            existing = out.get("cookie") or out.get("Cookie") or ""
            cookie = str(profile["clearance_cookie"])
            out["cookie"] = f"{existing}; {cookie}".strip("; ") if existing else cookie
        return out
    if clr.get("cookie"):
        existing = out.get("cookie") or out.get("Cookie") or ""
        out["cookie"] = (
            f"{existing}; {clr['cookie']}".strip("; ") if existing else clr["cookie"]
        )
    # UA: profile (already bound) wins, else clearance UA.
    if profile and profile.get("user_agent"):
        out["user-agent"] = str(profile["user_agent"])
    elif clr.get("user-agent"):
        out["user-agent"] = clr["user-agent"]
    return out


def is_cloudflare_blocked(status_code: int, body: str = "") -> bool:
    code = int(status_code or 0)
    text = (body or "")[:800].lower()
    if code in {403, 503, 429} and (
        "just a moment" in text
        or "cf-browser" in text
        or "challenge-platform" in text
        or "attention required" in text
        or "cloudflare" in text
        or code == 403
    ):
        # 403 alone from chatgpt is almost always CF on cloud IPs.
        return True
    if "just a moment" in text or "cf-mitigated" in text:
        return True
    return False


def probe_flaresolverr(url: str = "") -> dict[str, Any]:
    endpoint = normalize_flaresolverr_url(url) or ProxyRuntimeSettings.load().flaresolverr_url
    endpoint = (endpoint or DEFAULT_FLARESOLVERR_URL).rstrip("/")
    try:
        import requests

        resp = requests.post(
            f"{endpoint}/v1",
            json={"cmd": "sessions.list"},
            timeout=8,
        )
        data = resp.json() if resp.ok else {}
        ok = resp.ok and str((data or {}).get("status") or "ok").lower() in {"ok", ""}
        return {
            "ok": ok,
            "url": endpoint,
            "status_code": resp.status_code,
            "version": (data or {}).get("version") or "",
            "error": "" if ok else str(data)[:160],
        }
    except Exception as exc:
        return {"ok": False, "url": endpoint, "status_code": 0, "error": str(exc)[:200]}


def test_runtime_proxy() -> dict[str, Any]:
    """Diagnose egress + clearance against chatgpt.com."""
    settings = ProxyRuntimeSettings.load()
    proxy = settings.proxy_url if settings.enabled else ""
    path = "proxy" if proxy else "direct"
    result: dict[str, Any] = {
        "ok": False,
        "status_code": 0,
        "proxy_url": proxy or "direct",
        "egress_path": path,
        "enabled": settings.enabled,
        "scope": settings.scope,
        "clearance_mode": settings.clearance_mode,
        "flaresolverr_url": settings.flaresolverr_url if settings.clearance_mode == "flaresolverr" else "",
        "flaresolverr_ok": None,
        "clearance_applied": False,
        "has_cf_clearance": False,
        "user_agent_bound": False,
        "error": "",
        "hint": "",
    }

    if settings.clearance_mode == "flaresolverr":
        fs = probe_flaresolverr(settings.flaresolverr_url)
        result["flaresolverr_ok"] = fs.get("ok")
        result["flaresolverr_version"] = fs.get("version") or ""
        if not fs.get("ok"):
            result["error"] = f"FlareSolverr 不可达: {fs.get('error') or fs.get('status_code')}"
            result["hint"] = (
                "确认容器 freeagentidentity-flaresolverr-1 在跑，"
                f"地址填 {DEFAULT_FLARESOLVERR_URL}（不要填 127.0.0.1）"
            )
            return result

    try:
        from curl_cffi import requests as cffi

        # Force-refresh clearance once for an honest test.
        if settings.enabled and settings.clearance_mode not in {"", "none"}:
            invalidate_clearance("chatgpt.com")
        headers = clearance_headers_for_host("chatgpt.com", force=True)
        result["clearance_applied"] = bool(headers.get("cookie"))
        result["has_cf_clearance"] = "cf_clearance=" in (headers.get("cookie") or "")
        result["user_agent_bound"] = bool(headers.get("user-agent"))

        kwargs: dict[str, Any] = {
            "timeout": 20,
            "impersonate": "chrome136",
            "allow_redirects": True,
        }
        if proxy:
            kwargs["proxies"] = {"http": proxy, "https": proxy}
        if settings.skip_ssl_verify:
            kwargs["verify"] = False

        resp = cffi.get(
            "https://chatgpt.com/",
            headers=headers or None,
            **kwargs,
        )
        code = int(getattr(resp, "status_code", 0) or 0)
        text = str(getattr(resp, "text", "") or "")
        blocked = is_cloudflare_blocked(code, text)
        result["status_code"] = code
        result["ok"] = 200 <= code < 400 and not blocked
        if result["ok"]:
            result["error"] = ""
            result["hint"] = (
                f"出口={path}"
                + (
                    f" · 过盾={settings.clearance_mode}"
                    if settings.clearance_mode not in {"", "none"}
                    else " · 未启用过盾"
                )
            )
        else:
            result["error"] = f"blocked HTTP {code}" if blocked else f"HTTP {code}"
            if not settings.enabled:
                result["hint"] = "请先开启「访问运行时」，并保存配置"
            elif settings.clearance_mode in {"", "none"}:
                result["hint"] = "直连被 Cloudflare 挑战时，请将过盾模式设为 FlareSolverr"
            elif settings.clearance_mode == "flaresolverr" and not result["clearance_applied"]:
                result["hint"] = "FlareSolverr 已连通但未拿到 Cookie，查看 FS 日志或稍后重试"
            else:
                result["hint"] = "仍被拦：可能是 IP 硬封，或需更换代理出口"
        return result
    except Exception as exc:
        result["error"] = str(exc)[:200]
        result["hint"] = "网络异常或代理不可用"
        return result


def ensure_recommended_no_proxy_fs() -> ProxyRuntimeSettings:
    """One-shot: enable runtime + FlareSolverr defaults (no proxy required)."""
    config_store.set("proxy_runtime_enabled", "true")
    config_store.set("proxy_runtime_scope", "upstream_only")
    config_store.set("proxy_runtime_clearance_mode", "flaresolverr")
    if not str(config_store.get("proxy_runtime_flaresolverr_url", "") or "").strip():
        config_store.set("proxy_runtime_flaresolverr_url", DEFAULT_FLARESOLVERR_URL)
    if not str(config_store.get("proxy_runtime_refresh_interval_sec", "") or "").strip():
        config_store.set("proxy_runtime_refresh_interval_sec", "300")
    invalidate_clearance()
    return ProxyRuntimeSettings.load()


def save_runtime_from_form(form: dict[str, Any]) -> ProxyRuntimeSettings:
    """Persist runtime keys from settings form (string values)."""
    mapping = {
        "proxy_runtime_enabled": form.get("proxy_runtime_enabled"),
        "proxy_runtime_proxy_url": form.get("proxy_runtime_proxy_url"),
        "proxy_runtime_scope": form.get("proxy_runtime_scope"),
        "proxy_runtime_clearance_mode": form.get("proxy_runtime_clearance_mode"),
        "proxy_runtime_flaresolverr_url": form.get("proxy_runtime_flaresolverr_url"),
        "proxy_runtime_clearance_cookie": form.get("proxy_runtime_clearance_cookie"),
        "proxy_runtime_clearance_ua": form.get("proxy_runtime_clearance_ua"),
        "proxy_runtime_refresh_interval_sec": form.get("proxy_runtime_refresh_interval_sec"),
        "proxy_runtime_timeout_sec": form.get("proxy_runtime_timeout_sec"),
        "proxy_runtime_skip_ssl_verify": form.get("proxy_runtime_skip_ssl_verify"),
    }
    for key, value in mapping.items():
        if value is None:
            continue
        if key == "proxy_runtime_proxy_url":
            config_store.set(key, normalize_proxy_url(str(value)))
        elif key == "proxy_runtime_flaresolverr_url":
            raw = str(value).strip()
            if not raw and str(form.get("proxy_runtime_clearance_mode") or "").lower() == "flaresolverr":
                raw = DEFAULT_FLARESOLVERR_URL
            config_store.set(key, normalize_flaresolverr_url(raw))
        else:
            config_store.set(
                key,
                str(value).strip()
                if not isinstance(value, bool)
                else ("true" if value else "false"),
            )
    invalidate_clearance()
    return ProxyRuntimeSettings.load()

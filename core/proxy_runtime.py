"""Outbound runtime: optional proxy + Cloudflare clearance (FlareSolverr / manual).

Direct connection is always allowed (proxy URL may be empty).
Clearance is independent of having a proxy: FlareSolverr on the same host
can solve CF challenges on the server IP.
"""
from __future__ import annotations

import hashlib
import threading
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from core.config_store import config_store
from domain.registration_runtime import redact_registration_text

_LOCK = threading.Lock()
_CLEARANCE_CACHE: dict[tuple[str, str, str, str], dict[str, Any]] = {}
_NEGATIVE_CACHE: dict[tuple[str, str, str, str], float] = {}
_INFLIGHT: dict[tuple[str, str, str, str], threading.Event] = {}
_FS_SESSIONS: dict[tuple[str, str, str, str], str] = {}
# Session scope is deliberately coarser than the clearance cache key.  Each
# registration attempt keeps its own HTTP cookies/cache entry, while the
# FlareSolverr browser is reused for the same physical egress and Chrome
# profile family.  The clearance cache remains bound to the exact UA hash;
# when a real cookie is returned, the protocol transport is aligned to the
# solver browser before that cookie is applied.
_FS_SESSION_SCOPES: dict[str, tuple[str, str]] = {}
_FS_SESSION_LAST_USED: dict[str, float] = {}
_FS_SCOPE_LOCKS: dict[tuple[str, str], threading.RLock] = {}
_FS_LAST_CLEANUP_AT = 0.0
_EGRESS_CACHE: dict[str, tuple[float, str]] = {}

# Compose service name — app container reaches FS here (not 127.0.0.1).
DEFAULT_FLARESOLVERR_URL = "http://flaresolverr:8191"
DEFAULT_FLARESOLVERR_SESSION_IDLE_TTL_SEC = 900

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


def public_endpoint_url(url: str, *, empty: str = "") -> str:
    """Return an endpoint descriptor without embedded credentials or paths."""
    value = str(url or "").strip()
    if not value:
        return empty
    try:
        parsed = urlparse(value if "://" in value else f"http://{value}")
        host = parsed.hostname or ""
        if not host:
            return "configured"
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        port = f":{parsed.port}" if parsed.port else ""
        return f"{parsed.scheme or 'http'}://{host}{port}"
    except Exception:
        return "configured"


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


def validate_upstream_target(host: str, target_url: str = "") -> tuple[str, str]:
    """Return a normalized upstream host and HTTPS target without its fragment.

    The exact authorize query is intentionally preserved for Cloudflare, but
    credentials, non-standard ports and non-OpenAI redirect targets are never
    forwarded to FlareSolverr.
    """
    raw_host = str(host or "").strip()
    if not raw_host:
        raise ValueError("AUTH_REDIRECT: empty upstream host")
    try:
        host_ref = urlparse(raw_host if "://" in raw_host else f"https://{raw_host}/")
        canonical_host = str(host_ref.hostname or "").lower().rstrip(".")
        host_port = host_ref.port
    except (TypeError, ValueError) as exc:
        raise ValueError("AUTH_REDIRECT: invalid upstream host") from exc
    if (
        host_ref.scheme.lower() != "https"
        or host_ref.username is not None
        or host_ref.password is not None
        or host_port not in {None, 443}
        or not is_upstream_host(canonical_host)
    ):
        raise ValueError("AUTH_REDIRECT: upstream host is not allowed")

    requested = str(target_url or "").strip()
    if not requested:
        return canonical_host, f"https://{canonical_host}/"
    if any(ord(char) < 32 for char in requested):
        raise ValueError("AUTH_REDIRECT: invalid clearance target")
    try:
        parsed = urlparse(requested)
        target_host = str(parsed.hostname or "").lower().rstrip(".")
        target_port = parsed.port
    except (TypeError, ValueError) as exc:
        raise ValueError("AUTH_REDIRECT: invalid clearance target") from exc
    if (
        parsed.scheme.lower() != "https"
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or target_port not in {None, 443}
        or target_host != canonical_host
        or not is_upstream_host(target_host)
    ):
        raise ValueError("AUTH_REDIRECT: clearance target is not allowed")
    return canonical_host, parsed._replace(fragment="").geturl()


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
            "proxy_url": public_endpoint_url(self.proxy_url),
            "has_proxy": bool(self.proxy_url),
            "egress_path": path,
            "scope": self.scope,
            "clearance_mode": self.clearance_mode,
            "flaresolverr_url": public_endpoint_url(self.flaresolverr_url),
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


def _short_hash(value: str, *, empty: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        return empty
    return "sha256:" + hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]


def _origin_for_host(host: str) -> str:
    value = str(host or "").strip().lower()
    if "://" in value:
        parsed = urlparse(value)
        hostname = parsed.hostname or ""
        port = f":{parsed.port}" if parsed.port else ""
        return f"{parsed.scheme or 'https'}://{hostname}{port}"
    return f"https://{value.strip('/')}"


def clearance_cache_key(
    host: str,
    settings: ProxyRuntimeSettings,
    *,
    profile: dict[str, Any] | None = None,
    proxy_url: str | None = None,
    proxy_lease_id: str = "",
    fingerprint_id: str = "",
    user_agent: str = "",
) -> tuple[str, str, str, str]:
    """Build the identity-bound clearance key without exposing credentials."""
    bound_profile = profile or {}
    proxy_identity = (
        proxy_lease_id
        or str(bound_profile.get("proxy_lease_id") or bound_profile.get("proxy_ref") or "")
        or _short_hash(proxy_url or settings.proxy_url, empty="direct")
    )
    fingerprint = (
        fingerprint_id
        or str(bound_profile.get("fingerprint_id") or bound_profile.get("key") or "")
        or "default"
    )
    ua = user_agent or str(bound_profile.get("user_agent") or settings.clearance_user_agent or "")
    return (
        _origin_for_host(host),
        proxy_identity,
        fingerprint,
        _short_hash(ua, empty="ua:none"),
    )


def resolve_egress_ref(proxy_url: str | None = None, *, timeout: float = 5.0) -> str:
    """Return a credential-free observed egress identity, with stable fallback."""
    proxy = normalize_proxy_url(proxy_url or "")
    cache_key = _short_hash(proxy, empty="direct")
    now = time.time()
    with _LOCK:
        cached = _EGRESS_CACHE.get(cache_key)
        if cached and now - cached[0] < 300:
            return cached[1]
    try:
        from curl_cffi import requests as curl_requests

        kwargs: dict[str, Any] = {
            "timeout": max(float(timeout), 1.0),
            "impersonate": "chrome142",
        }
        if proxy:
            kwargs["proxies"] = {"http": proxy, "https": proxy}
        response = curl_requests.get("https://api.ipify.org?format=json", **kwargs)
        payload = response.json() if int(getattr(response, "status_code", 0) or 0) == 200 else {}
        address = str(payload.get("ip") or "").strip() if isinstance(payload, dict) else ""
        result = "egress:" + hashlib.sha256(address.encode("utf-8")).hexdigest()[:16] if address else cache_key
    except Exception:
        result = cache_key
    with _LOCK:
        _EGRESS_CACHE[cache_key] = (now, result)
    return result


def invalidate_clearance(host: str | None = None) -> None:
    """Drop cached clearance (all hosts, or one host)."""
    with _LOCK:
        if not host:
            _CLEARANCE_CACHE.clear()
            _NEGATIVE_CACHE.clear()
            return
        origin = _origin_for_host(host)
        for key in list(_CLEARANCE_CACHE.keys()):
            if key[0] == origin:
                _CLEARANCE_CACHE.pop(key, None)
                _NEGATIVE_CACHE.pop(key, None)


def _flaresolverr_scope(key: tuple[str, str, str, str]) -> tuple[str, str]:
    # Protocol fingerprints include an attempt suffix (for example
    # ``chrome145:<attempt-hash>``).  The solver browser must persist across
    # those attempts.  Exact UA isolation remains in the clearance cache key.
    profile_family = str(key[2] or "").split(":", 1)[0] or "default"
    return (str(key[1] or "direct"), profile_family)


def _flaresolverr_session_id(key: tuple[str, str, str, str]) -> str:
    # Origins keep separate clearance bundles, while the browser session shares
    # one proxy exit, profile family, and exact UA identity.
    scope = _flaresolverr_scope(key)
    digest = hashlib.sha256("|".join(scope).encode("utf-8")).hexdigest()[:24]
    return f"reg-{digest}"


def _flaresolverr_scope_lock(key: tuple[str, str, str, str]) -> threading.RLock:
    scope = _flaresolverr_scope(key)
    with _LOCK:
        return _FS_SCOPE_LOCKS.setdefault(scope, threading.RLock())


def _known_flaresolverr_session(key: tuple[str, str, str, str]) -> str:
    with _LOCK:
        direct = _FS_SESSIONS.get(key)
        if direct:
            _FS_SESSION_LAST_USED[direct] = time.time()
            return direct
        wanted_scope = _flaresolverr_scope(key)
        matched = ""
        for existing_key, session_id in _FS_SESSIONS.items():
            existing_scope = _FS_SESSION_SCOPES.get(session_id) or _flaresolverr_scope(existing_key)
            if existing_scope == wanted_scope:
                matched = session_id
                break
        if not matched:
            for session_id, existing_scope in _FS_SESSION_SCOPES.items():
                if existing_scope == wanted_scope:
                    matched = session_id
                    break
        if matched:
            _FS_SESSIONS[key] = matched
            _FS_SESSION_SCOPES.setdefault(matched, wanted_scope)
            _FS_SESSION_LAST_USED[matched] = time.time()
            return matched
    return ""


def _fingerprint_filter_matches(
    key: tuple[str, str, str, str],
    fingerprint_id: str,
) -> bool:
    """Match an exact attempt fingerprint or an explicit profile family.

    Attempts append a short suffix to the browser profile key.  Shutdown code
    often only has the base family (for example ``chrome145``), so accepting
    that family here prevents persistent solver sessions from being leaked.
    An id containing ``:`` remains exact to avoid destroying a sibling attempt.
    """
    wanted = str(fingerprint_id or "").strip()
    if not wanted:
        return True
    if key[2] == wanted:
        return True
    if ":" in wanted:
        return False
    return _flaresolverr_scope(key)[1] == wanted


def destroy_clearance_sessions(
    *,
    proxy_lease_id: str = "",
    fingerprint_id: str = "",
) -> int:
    """Destroy attempt-bound FlareSolverr browser sessions without logging secrets."""
    settings = ProxyRuntimeSettings.load()
    endpoint = (settings.flaresolverr_url or DEFAULT_FLARESOLVERR_URL).rstrip("/")
    with _LOCK:
        selected = [
            (key, session_id)
            for key, session_id in _FS_SESSIONS.items()
            if (not proxy_lease_id or key[1] == proxy_lease_id)
            and _fingerprint_filter_matches(key, fingerprint_id)
        ]
        selected_ids = {session_id for _key, session_id in selected}
        wanted_scopes = {
            scope
            for session_id, scope in _FS_SESSION_SCOPES.items()
            if (not proxy_lease_id or scope[0] == proxy_lease_id)
            and (not fingerprint_id or scope[1] == fingerprint_id or scope[1] == str(fingerprint_id).split(":", 1)[0])
        }
        selected_ids.update(
            session_id
            for session_id, scope in _FS_SESSION_SCOPES.items()
            if scope in wanted_scopes
        )
        remaining_ids = {
            session_id
            for key, session_id in _FS_SESSIONS.items()
            if key not in {item_key for item_key, _ in selected}
        }
        destroy_ids = selected_ids - remaining_ids
        for key, _ in selected:
            _FS_SESSIONS.pop(key, None)
            _CLEARANCE_CACHE.pop(key, None)
            _NEGATIVE_CACHE.pop(key, None)
        for session_id in destroy_ids:
            _FS_SESSION_SCOPES.pop(session_id, None)
            _FS_SESSION_LAST_USED.pop(session_id, None)
    if not endpoint:
        return len(selected_ids)
    try:
        import requests

        for session_id in sorted(destroy_ids):
            try:
                requests.post(
                    f"{endpoint}/v1",
                    json={"cmd": "sessions.destroy", "session": session_id},
                    timeout=min(max(settings.timeout_sec, 5), 20),
                )
            except Exception:
                continue
    except Exception:
        pass
    return len(selected_ids)


def release_clearance_aliases(*, proxy_lease_id: str = "", fingerprint_id: str = "") -> int:
    """Release one attempt's cache aliases while retaining the solver browser.

    FlareSolverr sessions carry sticky browser state.  Closing them after every
    registration defeats that state and recreates the intermittent CF failure
    seen on sequential attempts.  The session is retained in-process and can
    still be explicitly removed with :func:`destroy_clearance_sessions`.
    """
    with _LOCK:
        selected = [
            key
            for key in _FS_SESSIONS
            if (not proxy_lease_id or key[1] == proxy_lease_id)
            and _fingerprint_filter_matches(key, fingerprint_id)
        ]
        for key in selected:
            _FS_SESSIONS.pop(key, None)
            _CLEARANCE_CACHE.pop(key, None)
            _NEGATIVE_CACHE.pop(key, None)
        return len(selected)


def cleanup_idle_clearance_sessions(
    *,
    max_idle_seconds: float = DEFAULT_FLARESOLVERR_SESSION_IDLE_TTL_SEC,
    force: bool = False,
    settings: ProxyRuntimeSettings | None = None,
    now: float | None = None,
) -> int:
    """Destroy retained solver sessions after all aliases are released and idle."""
    global _FS_LAST_CLEANUP_AT
    current = float(now if now is not None else time.time())
    ttl = max(float(max_idle_seconds), 1.0)
    with _LOCK:
        if not force and current - _FS_LAST_CLEANUP_AT < 60.0:
            return 0
        _FS_LAST_CLEANUP_AT = current
        referenced = set(_FS_SESSIONS.values())
        candidates = [
            (session_id, scope)
            for session_id, scope in _FS_SESSION_SCOPES.items()
            if session_id not in referenced
            and current - float(_FS_SESSION_LAST_USED.get(session_id) or current) >= ttl
        ]
    if not candidates:
        return 0

    runtime = settings or ProxyRuntimeSettings.load()
    endpoint = (runtime.flaresolverr_url or DEFAULT_FLARESOLVERR_URL).rstrip("/")
    destroyed = 0
    for session_id, scope in candidates:
        # Read/create the lock under the global map lock, but never hold
        # ``_LOCK`` while waiting for the per-scope lock.  Request workers use
        # the opposite order (scope lock -> ``_LOCK``) while recording aliases;
        # this ordering avoids a cleanup/request deadlock.
        with _LOCK:
            scope_lock = _FS_SCOPE_LOCKS.setdefault(scope, threading.RLock())
        if not scope_lock.acquire(blocking=False):
            continue
        try:
            with _LOCK:
                referenced = session_id in _FS_SESSIONS.values()
                last_used = float(_FS_SESSION_LAST_USED.get(session_id) or current)
                if referenced or current - last_used < ttl:
                    continue
                _FS_SESSION_SCOPES.pop(session_id, None)
                _FS_SESSION_LAST_USED.pop(session_id, None)
            try:
                import requests

                requests.post(
                    f"{endpoint}/v1",
                    json={"cmd": "sessions.destroy", "session": session_id},
                    timeout=min(max(runtime.timeout_sec, 5), 20),
                )
            except Exception:
                pass
            destroyed += 1
        finally:
            scope_lock.release()
    return destroyed


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
    profile: dict[str, Any] | None = None,
    proxy_url: str | None = None,
    proxy_lease_id: str = "",
    fingerprint_id: str = "",
    user_agent: str = "",
    require_clearance: bool = False,
    target_url: str = "",
) -> dict[str, Any] | None:
    endpoint = (settings.flaresolverr_url or DEFAULT_FLARESOLVERR_URL).rstrip("/")
    if not endpoint or not host:
        return None
    host, target = validate_upstream_target(host, target_url)
    cleanup_idle_clearance_sessions(settings=settings)
    proxy = normalize_proxy_url(proxy_url or settings.proxy_url or "")
    key = clearance_cache_key(
        host,
        settings,
        profile=profile,
        proxy_url=proxy,
        proxy_lease_id=proxy_lease_id,
        fingerprint_id=fingerprint_id,
        user_agent=user_agent,
    )
    now = time.time()
    if not force:
        with _LOCK:
            if _NEGATIVE_CACHE.get(key, 0) > now:
                return {
                    "status": "clearance_missing",
                    "cookie": "",
                    "user_agent": "",
                    "has_cf_clearance": False,
                    "source": "flaresolverr",
                    "host": host.lower(),
                    "negative_cached": True,
                }
            cached = _CLEARANCE_CACHE.get(key)
            if cached and (
                settings.refresh_interval_sec <= 0
                or now - float(cached.get("at") or 0) < settings.refresh_interval_sec
            ):
                # A solver may legitimately return an ordinary 200 page with
                # no CF cookie.  Reuse that negative answer when it explicitly
                # says no challenge was present; do not hammer FlareSolverr on
                # every attempt just because the caller is challenge-ready.
                if cached.get("has_cf_clearance") or not cached.get("challenge_detected", False):
                    return cached
    with _LOCK:
        inflight = _INFLIGHT.get(key)
        if inflight is None:
            inflight = threading.Event()
            _INFLIGHT[key] = inflight
            owner = True
        else:
            owner = False
    if not owner:
        inflight.wait(timeout=max(settings.timeout_sec, 45) + 20)
        with _LOCK:
            cached = _CLEARANCE_CACHE.get(key)
            if cached and (cached.get("has_cf_clearance") or not cached.get("challenge_detected", False)):
                return cached
            if _NEGATIVE_CACHE.get(key, 0) > time.time():
                return {
                    "status": "clearance_missing",
                    "cookie": "",
                    "user_agent": "",
                    "has_cf_clearance": False,
                    "source": "flaresolverr",
                    "host": host.lower(),
                    "negative_cached": True,
                }
        return None
    try:
        import requests

        scope_lock = _flaresolverr_scope_lock(key)
        scope_lock.acquire()
        try:
            known_session = _known_flaresolverr_session(key)
            # Reuse the server-side session discovered through an alias.  The
            # mapping is intentionally persistent across ChatGPT/auth origins and
            # attempt-suffixed cache keys; creating a fresh session here would
            # silently discard the sticky proxy/browser state.
            session_id = known_session or _flaresolverr_session_id(key)
            def _create_solver_session() -> bool:
                create_body: dict[str, Any] = {
                    "cmd": "sessions.create",
                    "session": session_id,
                }
                if proxy:
                    create_body["proxy"] = {"url": proxy}
                create_resp = requests.post(
                    f"{endpoint}/v1",
                    json=create_body,
                    timeout=max(settings.timeout_sec, 45) + 15,
                )
                create_data = create_resp.json() if create_resp.ok else {}
                create_status = str(create_data.get("status") or "").lower() if isinstance(create_data, dict) else ""
                create_message = str(create_data.get("message") or "").lower() if isinstance(create_data, dict) else ""
                if not create_resp.ok or (create_status not in {"ok", ""} and "already" not in create_message):
                    return False
                with _LOCK:
                    _FS_SESSIONS[key] = session_id
                    _FS_SESSION_SCOPES[session_id] = _flaresolverr_scope(key)
                    _FS_SESSION_LAST_USED[session_id] = time.time()
                return True

            if not known_session:
                if not _create_solver_session():
                    return None
            else:
                with _LOCK:
                    _FS_SESSIONS[key] = session_id
                    _FS_SESSION_SCOPES.setdefault(session_id, _flaresolverr_scope(key))
                    _FS_SESSION_LAST_USED[session_id] = time.time()

            body: dict[str, Any] = {
                "cmd": "request.get",
                "url": target,
                "session": session_id,
                "maxTimeout": max(settings.timeout_sec, 45) * 1000,
                "returnOnlyCookies": False,
            }
            def _request_solver():
                response = requests.post(
                    f"{endpoint}/v1",
                    json=body,
                    timeout=max(settings.timeout_sec, 45) + 15,
                )
                payload = response.json() if response.ok else {}
                return response, payload

            resp, data = _request_solver()
            response_ok = (
                resp.ok
                and isinstance(data, dict)
                and str(data.get("status") or "").lower() in {"ok", ""}
            )
            if known_session and not response_ok:
                # FlareSolverr may restart while the app process keeps a retained
                # alias.  Drop the stale server-session mapping and recreate it
                # once instead of poisoning every later registration attempt.
                with _LOCK:
                    for alias_key, alias_session in list(_FS_SESSIONS.items()):
                        if alias_session == session_id:
                            _FS_SESSIONS.pop(alias_key, None)
                    _FS_SESSION_SCOPES.pop(session_id, None)
                    _FS_SESSION_LAST_USED.pop(session_id, None)
                if not _create_solver_session():
                    return None
                resp, data = _request_solver()
            if not resp.ok or not isinstance(data, dict) or str(data.get("status") or "").lower() not in {"ok", ""}:
                return None
        finally:
            scope_lock.release()
        solution = (data.get("solution") or {}) if isinstance(data, dict) else {}
        cookie_header = _cookie_header_from_solution(solution.get("cookies") or [])
        ua = str(solution.get("userAgent") or settings.clearance_user_agent or "")
        has_cf = "cf_clearance=" in cookie_header
        try:
            solution_status = int(solution.get("status") or 0)
        except (TypeError, ValueError):
            solution_status = 0
        solution_body = str(solution.get("response") or "")[:12000]
        # A plain 403/429 from FlareSolverr is still an unavailable clearance
        # outcome even when the response body omits the usual "Just a moment"
        # marker.  Treating it as ``not_required`` binds ordinary solver state
        # to the registration transport and causes the next request to fail.
        challenged = is_cloudflare_blocked(solution_status, solution_body) or solution_status in {
            403,
            429,
        }
        # `require_clearance` means a real challenge must be solved; it does
        # not turn an ordinary, unchallenged page into a solver failure.  This
        # distinction is critical for intermittent direct egresses where most
        # requests are normal and only some are challenged.
        status = "valid_clearance" if has_cf else "not_required"
        if challenged and not has_cf:
            status = "clearance_missing"
        elif solution_status >= 400 and not has_cf:
            status = "solver_error"
        bundle = {
            "status": status,
            "cookie": cookie_header,
            "user_agent": ua,
            "at": now,
            "has_cf_clearance": has_cf,
            "challenge_detected": bool(challenged),
            "solution_status": solution_status,
            "source": "flaresolverr",
            "host": host.lower(),
            "binding": {
                "origin": key[0],
                "proxy_lease_id": key[1],
                "fingerprint_id": key[2],
                "user_agent_hash": key[3],
            },
        }
        with _LOCK:
            if status in {"valid_clearance", "not_required"}:
                _CLEARANCE_CACHE[key] = bundle
                _NEGATIVE_CACHE.pop(key, None)
            else:
                _CLEARANCE_CACHE.pop(key, None)
                _NEGATIVE_CACHE[key] = time.time() + 60.0
        return bundle
    except Exception:
        return None
    finally:
        with _LOCK:
            event = _INFLIGHT.pop(key, None)
        if event is not None:
            event.set()


def get_clearance_bundle(
    host: str = "chatgpt.com",
    *,
    force: bool = False,
    profile: dict[str, Any] | None = None,
    proxy_url: str | None = None,
    proxy_lease_id: str = "",
    fingerprint_id: str = "",
    user_agent: str = "",
    require_clearance: bool = False,
    target_url: str = "",
) -> dict[str, Any] | None:
    """Return {cookie, user_agent, ...} for CF, or None."""
    settings = ProxyRuntimeSettings.load()
    if not settings.enabled or settings.clearance_mode in {"", "none"}:
        return None
    if settings.clearance_mode == "manual":
        if not settings.clearance_cookie:
            return None
        key = clearance_cache_key(
            host,
            settings,
            profile=profile,
            proxy_url=proxy_url,
            proxy_lease_id=proxy_lease_id,
            fingerprint_id=fingerprint_id,
            user_agent=user_agent,
        )
        return {
            "status": (
                "valid_clearance"
                if "cf_clearance=" in settings.clearance_cookie
                else ("clearance_missing" if require_clearance else "not_required")
            ),
            "cookie": settings.clearance_cookie,
            "user_agent": settings.clearance_user_agent or "",
            "at": time.time(),
            "has_cf_clearance": "cf_clearance=" in settings.clearance_cookie,
            "challenge_detected": False,
            "solution_status": 200,
            "source": "manual",
            "host": host.lower(),
            "binding": {
                "origin": key[0],
                "proxy_lease_id": key[1],
                "fingerprint_id": key[2],
                "user_agent_hash": key[3],
            },
        }
    if settings.clearance_mode == "flaresolverr":
        return _get_flaresolverr_clearance(
            host,
            settings,
            force=force,
            profile=profile,
            proxy_url=proxy_url,
            proxy_lease_id=proxy_lease_id,
            fingerprint_id=fingerprint_id,
            user_agent=user_agent,
            require_clearance=require_clearance,
            target_url=target_url,
        )
    return None


def clearance_headers_for_host(
    host: str,
    *,
    force: bool = False,
    profile: dict[str, Any] | None = None,
    proxy_url: str | None = None,
) -> dict[str, str]:
    """Optional CF clearance headers (manual cookie or FlareSolverr cache)."""
    bundle = get_clearance_bundle(
        host,
        force=force,
        profile=profile,
        proxy_url=proxy_url,
    )
    if not bundle:
        return {}
    # ``not_required`` is an observation that this solver request saw a normal
    # page.  It is not transferable clearance: do not copy the solver's UA or
    # ordinary cookies into the registration transport, otherwise curl TLS and
    # browser identity can diverge on the next request.
    bundle_status = str(bundle.get("status") or "")
    if bundle_status != "valid_clearance" and not (
        not bundle_status and bool(bundle.get("has_cf_clearance"))
    ):
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
    proxy_url: str | None = None,
    require_clearance: bool = False,
    target_url: str = "",
) -> dict[str, Any]:
    """Mutate browser profile so UA matches clearance cookie (required by CF).

    Returns the clearance bundle used (or empty dict).
    """
    bundle = get_clearance_bundle(
        host,
        force=force,
        profile=profile,
        proxy_url=proxy_url,
        require_clearance=require_clearance,
        target_url=target_url,
    ) or {}
    bundle_status = str(bundle.get("status") or "")
    if not bundle_status and bool(bundle.get("has_cf_clearance")):
        bundle_status = "valid_clearance"
        bundle["status"] = bundle_status
    if bundle_status != "valid_clearance":
        profile.pop("clearance_cookie", None)
        profile.pop("clearance_source", None)
        return bundle
    old_key = clearance_cache_key(
        host,
        ProxyRuntimeSettings.load(),
        profile=profile,
        proxy_url=proxy_url,
    )
    ua = str(bundle.get("user_agent") or "").strip()
    if ua:
        from platforms.chatgpt.browser_profiles import align_chrome_profile_to_user_agent

        profile.update(align_chrome_profile_to_user_agent(profile, ua))
        new_key = clearance_cache_key(
            host,
            ProxyRuntimeSettings.load(),
            profile=profile,
            proxy_url=proxy_url,
        )
        if new_key != old_key:
            aliased = dict(bundle)
            aliased["binding"] = {
                "origin": new_key[0],
                "proxy_lease_id": new_key[1],
                "fingerprint_id": new_key[2],
                "user_agent_hash": new_key[3],
            }
            session_id = _known_flaresolverr_session(old_key)
            with _LOCK:
                _CLEARANCE_CACHE[new_key] = aliased
                if session_id:
                    _FS_SESSIONS[new_key] = session_id
                    _FS_SESSION_SCOPES.setdefault(session_id, _flaresolverr_scope(new_key))
    # Only a real cf_clearance bundle is transferable to the registration
    # profile.  FlareSolverr commonly returns __cf_bm/_cfuvid on ordinary pages;
    # persisting those as ``clearance_cookie`` would make a later header merge
    # inject solver state with a mismatched UA.
    if bundle.get("cookie") and bundle_status == "valid_clearance":
        profile["clearance_cookie"] = str(bundle["cookie"])
        profile["clearance_source"] = str(bundle.get("source") or "")
    return bundle


def merge_clearance_into_headers(
    headers: dict[str, str],
    host: str = "chatgpt.com",
    *,
    force: bool = False,
    profile: dict[str, Any] | None = None,
    proxy_url: str | None = None,
) -> dict[str, str]:
    """Return headers with cookie + UA from clearance; prefer profile UA if set."""
    out = dict(headers or {})
    clr = clearance_headers_for_host(
        host,
        force=force,
        profile=profile,
        proxy_url=proxy_url,
    )
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
    from domain.challenge_runtime import ChallengeClassifier

    result = ChallengeClassifier.classify(
        status_code=status_code,
        body=(body or "")[:4000],
    )
    return result.challenged or (
        int(status_code or 0) == 403
        and result.error_code is not None
    )


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
            "url": public_endpoint_url(endpoint),
            "status_code": resp.status_code,
            "version": (data or {}).get("version") or "",
            "error": "" if ok else str(data)[:160],
        }
    except Exception as exc:
        return {
            "ok": False,
            "url": public_endpoint_url(endpoint),
            "status_code": 0,
            "error": redact_registration_text(exc)[:200],
        }


def test_runtime_proxy() -> dict[str, Any]:
    """Diagnose egress + clearance against chatgpt.com."""
    settings = ProxyRuntimeSettings.load()
    proxy = settings.proxy_url if settings.enabled else ""
    path = "proxy" if proxy else "direct"
    result: dict[str, Any] = {
        "ok": False,
        "status_code": 0,
        "proxy_url": public_endpoint_url(proxy, empty="direct"),
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

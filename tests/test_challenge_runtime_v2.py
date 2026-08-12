from __future__ import annotations

from types import SimpleNamespace
import threading
import time

import pytest

from core import proxy_runtime
from domain.challenge_runtime import ChallengeClassifier, ChallengeKind
from domain.registration_runtime import RegistrationErrorCode
from platforms.chatgpt.browser_profiles import align_chrome_profile_to_user_agent


def test_classifier_distinguishes_challenge_and_transport_categories():
    turnstile = ChallengeClassifier.classify(
        status_code=403,
        body='<div class="cf-turnstile" data-sitekey="test"></div>',
    )
    managed = ChallengeClassifier.classify(
        status_code=503,
        headers={"cf-mitigated": "challenge"},
        body="Just a moment",
    )
    sentinel = ChallengeClassifier.classify(
        status_code=200,
        payload={"sentinel": {"proof-of-work": {"required": True}}},
    )
    rate_limit = ChallengeClassifier.classify(status_code=429)
    network = ChallengeClassifier.classify(error="DNS name resolution failed")

    assert turnstile.kind is ChallengeKind.TURNSTILE
    assert managed.kind is ChallengeKind.CLOUDFLARE_MANAGED
    assert sentinel.kind is ChallengeKind.SENTINEL_POW
    assert sentinel.error_code is RegistrationErrorCode.SENTINEL_PROOF
    assert rate_limit.kind is ChallengeKind.HTTP_RATE_LIMIT
    assert rate_limit.error_code is RegistrationErrorCode.HTTP_RATE_LIMIT
    assert network.kind is ChallengeKind.NETWORK_ERROR
    assert network.error_code is RegistrationErrorCode.NET_DNS


def test_managed_challenge_wins_when_page_also_loads_turnstile_script():
    result = ChallengeClassifier.classify(
        headers={"cf-mitigated": "challenge"},
        body=(
            "<title>Performing security verification</title>"
            "<script src='https://challenges.cloudflare.com/turnstile/v0/api.js'></script>"
            "<div class='cf-turnstile'></div>"
        ),
    )

    assert result.kind is ChallengeKind.CLOUDFLARE_MANAGED


def test_background_cloudflare_js_detection_is_not_a_managed_challenge():
    result = ChallengeClassifier.classify(
        status_code=200,
        body=(
            "<title>Check your inbox - OpenAI</title>"
            "<input name='code' autocomplete='one-time-code'>"
            "<script src='/cdn-cgi/challenge-platform/scripts/jsd/main.js'></script>"
            "<script>window._cf_chl_opt={chlApiRq:'cf-chl-test'}</script>"
        ),
    )

    assert result.kind is ChallengeKind.NONE


def test_classifier_keeps_plain_403_and_auth_business_errors_separate():
    forbidden = ChallengeClassifier.classify(status_code=403, body="forbidden")
    invalid_step = ChallengeClassifier.classify(
        status_code=400,
        payload={"error": {"code": "invalid_auth_step"}},
    )

    assert forbidden.kind is ChallengeKind.HTTP_FORBIDDEN
    assert forbidden.challenged is False
    assert invalid_step.kind is ChallengeKind.OPENAI_AUTH_ERROR
    assert invalid_step.error_code is RegistrationErrorCode.AUTH_INVALID_STEP


def test_clearance_cache_key_is_bound_to_origin_proxy_fingerprint_and_ua():
    settings = proxy_runtime.ProxyRuntimeSettings(proxy_url="http://user:secret@proxy:8080")
    first = proxy_runtime.clearance_cache_key(
        "chatgpt.com",
        settings,
        profile={
            "proxy_lease_id": "lease-a",
            "fingerprint_id": "fp-a",
            "user_agent": "UA/1",
        },
    )
    second = proxy_runtime.clearance_cache_key(
        "chatgpt.com",
        settings,
        profile={
            "proxy_lease_id": "lease-a",
            "fingerprint_id": "fp-b",
            "user_agent": "UA/1",
        },
    )

    assert first == ("https://chatgpt.com", "lease-a", "fp-a", first[3])
    assert first != second
    assert "secret" not in "|".join(first)


def test_flaresolverr_uses_attempt_proxy_and_does_not_share_bound_cache(monkeypatch):
    proxy_runtime.invalidate_clearance()
    requests_seen = []

    class Response:
        ok = True

        @staticmethod
        def json():
            return {
                "status": "ok",
                "solution": {
                    "userAgent": "UA/solver",
                    "cookies": [{"name": "cf_clearance", "value": "value"}],
                },
            }

    def fake_post(url, *, json, timeout):
        requests_seen.append((url, json, timeout))
        return Response()

    monkeypatch.setitem(__import__("sys").modules, "requests", SimpleNamespace(post=fake_post))
    settings = proxy_runtime.ProxyRuntimeSettings(
        enabled=True,
        clearance_mode="flaresolverr",
        flaresolverr_url="http://solver:8191",
        refresh_interval_sec=300,
    )
    monkeypatch.setattr(proxy_runtime.ProxyRuntimeSettings, "load", classmethod(lambda cls: settings))

    profile_a = {"proxy_lease_id": "lease-a", "fingerprint_id": "fp-a", "user_agent": "UA/1"}
    profile_b = {"proxy_lease_id": "lease-b", "fingerprint_id": "fp-b", "user_agent": "UA/1"}
    first = proxy_runtime.get_clearance_bundle(
        "chatgpt.com",
        profile=profile_a,
        proxy_url="http://user:pass@proxy-a:8080",
    )
    cached = proxy_runtime.get_clearance_bundle(
        "chatgpt.com",
        profile=profile_a,
        proxy_url="http://user:pass@proxy-a:8080",
    )
    second = proxy_runtime.get_clearance_bundle(
        "chatgpt.com",
        profile=profile_b,
        proxy_url="http://proxy-b:8080",
    )

    assert first == cached
    assert second is not None
    assert [item[1]["cmd"] for item in requests_seen] == [
        "sessions.create",
        "request.get",
        "sessions.create",
        "request.get",
    ]
    assert requests_seen[0][1]["proxy"]["url"] == "http://user:pass@proxy-a:8080"
    assert "proxy" not in requests_seen[1][1]
    assert requests_seen[2][1]["proxy"]["url"] == "http://proxy-b:8080"
    assert "proxy" not in requests_seen[3][1]
    assert requests_seen[0][1]["session"] == requests_seen[1][1]["session"]
    assert requests_seen[2][1]["session"] == requests_seen[3][1]["session"]
    assert first["binding"]["proxy_lease_id"] == "lease-a"
    assert second["binding"]["proxy_lease_id"] == "lease-b"


def test_cloudflare_compatibility_helper_uses_classifier():
    assert proxy_runtime.is_cloudflare_blocked(503, "Just a moment") is True
    assert proxy_runtime.is_cloudflare_blocked(200, "normal page") is False
    assert proxy_runtime.is_cloudflare_blocked(403, "forbidden") is True


def test_required_clearance_rejects_ordinary_cloudflare_cookies(monkeypatch):
    proxy_runtime.invalidate_clearance()

    class Response:
        ok = True

        def __init__(self, body):
            self._body = body

        def json(self):
            return self._body

    def fake_post(_url, *, json, timeout):
        del timeout
        if json["cmd"] == "sessions.create":
            return Response({"status": "ok"})
        return Response(
            {
                "status": "ok",
                "solution": {
                    "status": 403,
                    "response": "<title>Just a moment...</title>",
                    "userAgent": "UA/solver",
                    "cookies": [{"name": "__cf_bm", "value": "ordinary"}],
                },
            }
        )

    monkeypatch.setitem(__import__("sys").modules, "requests", SimpleNamespace(post=fake_post))
    settings = proxy_runtime.ProxyRuntimeSettings(
        enabled=True,
        clearance_mode="flaresolverr",
        flaresolverr_url="http://solver:8191",
        refresh_interval_sec=300,
    )
    monkeypatch.setattr(proxy_runtime.ProxyRuntimeSettings, "load", classmethod(lambda cls: settings))

    result = proxy_runtime.get_clearance_bundle(
        "auth.openai.com",
        profile={"fingerprint_id": "fp", "user_agent": "UA/1"},
        require_clearance=True,
    )

    assert result["status"] == "clearance_missing"
    assert result["has_cf_clearance"] is False
    cached = proxy_runtime.get_clearance_bundle(
        "auth.openai.com",
        profile={"fingerprint_id": "fp", "user_agent": "UA/1"},
        require_clearance=True,
    )
    assert cached["status"] == "clearance_missing"
    assert cached["negative_cached"] is True


def test_required_clearance_accepts_solver_normal_page_without_cf_cookie(monkeypatch):
    """A normal 200 solver response is not a clearance failure."""
    proxy_runtime.invalidate_clearance()
    with proxy_runtime._LOCK:
        proxy_runtime._FS_SESSIONS.clear()
        proxy_runtime._FS_SESSION_SCOPES.clear()

    class Response:
        ok = True

        def __init__(self, body):
            self._body = body

        def json(self):
            return self._body

    def fake_post(_url, *, json, timeout):
        del timeout
        if json["cmd"] == "sessions.create":
            return Response({"status": "ok"})
        return Response(
            {
                "status": "ok",
                "solution": {
                    "status": 200,
                    "response": "<html><title>OpenAI</title></html>",
                    "userAgent": "UA/solver",
                    "cookies": [{"name": "__cf_bm", "value": "ordinary"}],
                },
            }
        )

    monkeypatch.setitem(__import__("sys").modules, "requests", SimpleNamespace(post=fake_post))
    settings = proxy_runtime.ProxyRuntimeSettings(
        enabled=True,
        clearance_mode="flaresolverr",
        flaresolverr_url="http://solver:8191",
        refresh_interval_sec=300,
    )
    monkeypatch.setattr(proxy_runtime.ProxyRuntimeSettings, "load", classmethod(lambda cls: settings))

    result = proxy_runtime.get_clearance_bundle(
        "auth.openai.com",
        profile={"proxy_lease_id": "lease-normal", "fingerprint_id": "fp-normal", "user_agent": "UA/1"},
        require_clearance=True,
    )

    assert result["status"] == "not_required"
    assert result["challenge_detected"] is False
    assert result["has_cf_clearance"] is False


def test_flaresolverr_solves_the_exact_challenged_authorize_url(monkeypatch):
    proxy_runtime.invalidate_clearance()
    with proxy_runtime._LOCK:
        proxy_runtime._FS_SESSIONS.clear()
        proxy_runtime._FS_SESSION_SCOPES.clear()
        proxy_runtime._FS_SESSION_LAST_USED.clear()
    request_urls = []

    class Response:
        ok = True

        def __init__(self, body):
            self._body = body

        def json(self):
            return self._body

    def fake_post(_url, *, json, timeout):
        del timeout
        if json["cmd"] == "sessions.create":
            return Response({"status": "ok"})
        request_urls.append(json["url"])
        return Response(
            {
                "status": "ok",
                "solution": {
                    "status": 200,
                    "response": "ok",
                    "userAgent": "UA/solver",
                    "cookies": [{"name": "cf_clearance", "value": "value"}],
                },
            }
        )

    monkeypatch.setitem(__import__("sys").modules, "requests", SimpleNamespace(post=fake_post))
    settings = proxy_runtime.ProxyRuntimeSettings(
        enabled=True,
        clearance_mode="flaresolverr",
        flaresolverr_url="http://solver:8191",
        refresh_interval_sec=0,
    )
    monkeypatch.setattr(proxy_runtime.ProxyRuntimeSettings, "load", classmethod(lambda cls: settings))
    target = "https://auth.openai.com/authorize?client_id=test&state=redacted#local-fragment"
    expected_target = "https://auth.openai.com/authorize?client_id=test&state=redacted"

    result = proxy_runtime.get_clearance_bundle(
        "auth.openai.com",
        profile={"proxy_lease_id": "lease-target", "fingerprint_id": "chrome145:a", "user_agent": "UA/1"},
        require_clearance=True,
        target_url=target,
    )

    assert result["status"] == "valid_clearance"
    assert request_urls == [expected_target]


@pytest.mark.parametrize(
    ("host", "target"),
    [
        ("auth.openai.com", "http://auth.openai.com/authorize"),
        ("auth.openai.com", "https://evil.example/authorize"),
        ("auth.openai.com", "https://user:pass@auth.openai.com/authorize"),
        ("auth.openai.com", "https://auth.openai.com:444/authorize"),
        ("evil.example", "https://evil.example/authorize"),
    ],
)
def test_flaresolverr_rejects_non_upstream_or_unsafe_target_before_request(
    monkeypatch,
    host,
    target,
):
    calls = []

    def fake_post(*_args, **_kwargs):
        calls.append(True)
        raise AssertionError("unsafe target reached FlareSolverr")

    monkeypatch.setitem(__import__("sys").modules, "requests", SimpleNamespace(post=fake_post))
    settings = proxy_runtime.ProxyRuntimeSettings(
        enabled=True,
        clearance_mode="flaresolverr",
        flaresolverr_url="http://solver:8191",
    )
    monkeypatch.setattr(proxy_runtime.ProxyRuntimeSettings, "load", classmethod(lambda cls: settings))

    with pytest.raises(ValueError, match="AUTH_REDIRECT"):
        proxy_runtime.get_clearance_bundle(
            host,
            profile={
                "proxy_lease_id": "lease-invalid-target",
                "fingerprint_id": "chrome145:invalid",
                "user_agent": "UA/1",
            },
            require_clearance=True,
            target_url=target,
        )

    assert calls == []


def test_not_required_solver_result_does_not_bind_ordinary_cookie_or_ua(monkeypatch):
    """An ordinary solver page is observational, never transferable clearance."""
    proxy_runtime.invalidate_clearance()
    with proxy_runtime._LOCK:
        proxy_runtime._FS_SESSIONS.clear()
        proxy_runtime._FS_SESSION_SCOPES.clear()
    calls = []

    class Response:
        ok = True

        def __init__(self, body):
            self._body = body

        def json(self):
            return self._body

    def fake_post(_url, *, json, timeout):
        del timeout
        calls.append(json["cmd"])
        if json["cmd"] == "sessions.create":
            return Response({"status": "ok"})
        return Response(
            {
                "status": "ok",
                "solution": {
                    "status": 200,
                    "response": "<html><title>OpenAI</title></html>",
                    "userAgent": "UA/solver-ordinary",
                    "cookies": [{"name": "__cf_bm", "value": "ordinary"}],
                },
            }
        )

    monkeypatch.setitem(__import__("sys").modules, "requests", SimpleNamespace(post=fake_post))
    settings = proxy_runtime.ProxyRuntimeSettings(
        enabled=True,
        clearance_mode="flaresolverr",
        flaresolverr_url="http://solver:8191",
        refresh_interval_sec=300,
    )
    monkeypatch.setattr(proxy_runtime.ProxyRuntimeSettings, "load", classmethod(lambda cls: settings))
    profile = {
        "proxy_lease_id": "lease-ordinary",
        "fingerprint_id": "chrome145:attempt",
        "user_agent": "UA/transport",
    }

    result = proxy_runtime.get_clearance_bundle("chatgpt.com", profile=profile)
    headers = proxy_runtime.clearance_headers_for_host("chatgpt.com", profile=profile)
    applied = proxy_runtime.apply_clearance_to_profile(profile, "chatgpt.com")
    merged = proxy_runtime.merge_clearance_into_headers({"accept": "text/html"}, profile=profile)

    assert result["status"] == "not_required"
    assert result["has_cf_clearance"] is False
    assert headers == {}
    assert applied["status"] == "not_required"
    assert "clearance_cookie" not in profile
    assert merged == {"accept": "text/html"}
    assert calls == ["sessions.create", "request.get"]


def test_plain_solver_403_without_cf_marker_is_clearance_missing(monkeypatch):
    """A bare 403 must not be treated as a normal page when clearance is required."""
    proxy_runtime.invalidate_clearance()
    with proxy_runtime._LOCK:
        proxy_runtime._FS_SESSIONS.clear()
        proxy_runtime._FS_SESSION_SCOPES.clear()

    class Response:
        ok = True

        def __init__(self, body):
            self._body = body

        def json(self):
            return self._body

    def fake_post(_url, *, json, timeout):
        del timeout
        if json["cmd"] == "sessions.create":
            return Response({"status": "ok"})
        return Response(
            {
                "status": "ok",
                "solution": {
                    "status": 403,
                    "response": "Forbidden",
                    "cookies": [{"name": "__cf_bm", "value": "ordinary"}],
                },
            }
        )

    monkeypatch.setitem(__import__("sys").modules, "requests", SimpleNamespace(post=fake_post))
    settings = proxy_runtime.ProxyRuntimeSettings(
        enabled=True,
        clearance_mode="flaresolverr",
        flaresolverr_url="http://solver:8191",
    )
    monkeypatch.setattr(proxy_runtime.ProxyRuntimeSettings, "load", classmethod(lambda cls: settings))

    result = proxy_runtime.get_clearance_bundle(
        "auth.openai.com",
        profile={"proxy_lease_id": "lease-403", "fingerprint_id": "fp-403", "user_agent": "UA/1"},
        require_clearance=True,
    )

    assert result["status"] == "clearance_missing"
    assert result["challenge_detected"] is True


def test_known_solver_session_alias_is_reused_without_create(monkeypatch):
    """A persisted alias must use its server session id instead of creating a new one."""
    proxy_runtime.invalidate_clearance()
    with proxy_runtime._LOCK:
        proxy_runtime._FS_SESSIONS.clear()
        proxy_runtime._FS_SESSION_SCOPES.clear()
        seed_key = (
            "https://chatgpt.com",
            "lease-persist",
            "chrome145:first",
            proxy_runtime._short_hash("UA/1", empty="ua:none"),
        )
        proxy_runtime._FS_SESSIONS[seed_key] = "persisted-session"
        proxy_runtime._FS_SESSION_SCOPES["persisted-session"] = proxy_runtime._flaresolverr_scope(seed_key)
    calls = []

    class Response:
        ok = True

        @staticmethod
        def json():
            return {
                "status": "ok",
                "solution": {
                    "status": 200,
                    "response": "ok",
                    "cookies": [{"name": "cf_clearance", "value": "value"}],
                },
            }

    def fake_post(_url, *, json, timeout):
        del timeout
        calls.append(json)
        return Response()

    monkeypatch.setitem(__import__("sys").modules, "requests", SimpleNamespace(post=fake_post))
    settings = proxy_runtime.ProxyRuntimeSettings(
        enabled=True,
        clearance_mode="flaresolverr",
        flaresolverr_url="http://solver:8191",
        refresh_interval_sec=0,
    )
    monkeypatch.setattr(proxy_runtime.ProxyRuntimeSettings, "load", classmethod(lambda cls: settings))

    result = proxy_runtime.get_clearance_bundle(
        "chatgpt.com",
        profile={"proxy_lease_id": "lease-persist", "fingerprint_id": "chrome145:second", "user_agent": "UA/1"},
        user_agent="UA/1",
        require_clearance=True,
    )

    assert result["status"] == "valid_clearance"
    assert [item["cmd"] for item in calls] == ["request.get"]
    assert calls[0]["session"] == "persisted-session"


def test_stale_persisted_solver_session_is_recreated_once(monkeypatch):
    proxy_runtime.invalidate_clearance()
    with proxy_runtime._LOCK:
        proxy_runtime._FS_SESSIONS.clear()
        proxy_runtime._FS_SESSION_SCOPES.clear()
        proxy_runtime._FS_SESSION_LAST_USED.clear()
        seed_key = (
            "https://chatgpt.com",
            "lease-stale",
            "chrome145:first",
            proxy_runtime._short_hash("UA/old", empty="ua:none"),
        )
        proxy_runtime._FS_SESSIONS[seed_key] = "stale-session"
        proxy_runtime._FS_SESSION_SCOPES["stale-session"] = proxy_runtime._flaresolverr_scope(seed_key)
    calls = []

    class Response:
        ok = True

        def __init__(self, body):
            self._body = body

        def json(self):
            return self._body

    def fake_post(_url, *, json, timeout):
        del timeout
        calls.append(json["cmd"])
        if json["cmd"] == "request.get" and calls.count("request.get") == 1:
            return Response({"status": "error", "message": "session does not exist"})
        if json["cmd"] == "sessions.create":
            return Response({"status": "ok"})
        return Response(
            {
                "status": "ok",
                "solution": {
                    "status": 200,
                    "response": "ok",
                    "userAgent": "UA/solver",
                    "cookies": [{"name": "cf_clearance", "value": "value"}],
                },
            }
        )

    monkeypatch.setitem(__import__("sys").modules, "requests", SimpleNamespace(post=fake_post))
    settings = proxy_runtime.ProxyRuntimeSettings(
        enabled=True,
        clearance_mode="flaresolverr",
        flaresolverr_url="http://solver:8191",
        refresh_interval_sec=0,
    )
    monkeypatch.setattr(proxy_runtime.ProxyRuntimeSettings, "load", classmethod(lambda cls: settings))

    result = proxy_runtime.get_clearance_bundle(
        "chatgpt.com",
        profile={
            "proxy_lease_id": "lease-stale",
            "fingerprint_id": "chrome145:second",
            "user_agent": "UA/new",
        },
        require_clearance=True,
    )

    assert result["status"] == "valid_clearance"
    assert calls == ["request.get", "sessions.create", "request.get"]


def test_concurrent_stale_solver_recovery_is_serialized(monkeypatch):
    """A stale persistent session is recreated once before sibling requests run."""
    proxy_runtime.invalidate_clearance()
    with proxy_runtime._LOCK:
        proxy_runtime._FS_SESSIONS.clear()
        proxy_runtime._FS_SESSION_SCOPES.clear()
        proxy_runtime._FS_SESSION_LAST_USED.clear()
        proxy_runtime._FS_SCOPE_LOCKS.clear()
        seed_key = (
            "https://chatgpt.com",
            "egress:stale-shared",
            "chrome145:first",
            proxy_runtime._short_hash("UA/old", empty="ua:none"),
        )
        proxy_runtime._FS_SESSIONS[seed_key] = "stale-session"
        proxy_runtime._FS_SESSION_SCOPES["stale-session"] = proxy_runtime._flaresolverr_scope(seed_key)

    calls = []
    calls_lock = threading.Lock()
    active_requests = 0
    max_active_requests = 0

    class Response:
        ok = True

        def __init__(self, body):
            self._body = body

        def json(self):
            return self._body

    def fake_post(_url, *, json, timeout):
        nonlocal active_requests, max_active_requests
        del timeout
        with calls_lock:
            calls.append((json["cmd"], json.get("session")))
            request_index = sum(1 for command, _session in calls if command == "request.get")
        if json["cmd"] == "sessions.create":
            return Response({"status": "ok"})
        with calls_lock:
            active_requests += 1
            max_active_requests = max(max_active_requests, active_requests)
        time.sleep(0.03)
        with calls_lock:
            active_requests -= 1
        if request_index == 1:
            return Response({"status": "error", "message": "session does not exist"})
        return Response(
            {
                "status": "ok",
                "solution": {
                    "status": 200,
                    "response": "ok",
                    "userAgent": "UA/solver",
                    "cookies": [{"name": "cf_clearance", "value": "value"}],
                },
            }
        )

    monkeypatch.setitem(__import__("sys").modules, "requests", SimpleNamespace(post=fake_post))
    settings = proxy_runtime.ProxyRuntimeSettings(
        enabled=True,
        clearance_mode="flaresolverr",
        flaresolverr_url="http://solver:8191",
        refresh_interval_sec=0,
    )
    monkeypatch.setattr(proxy_runtime.ProxyRuntimeSettings, "load", classmethod(lambda cls: settings))
    barrier = threading.Barrier(3)
    results = []

    def worker(suffix):
        barrier.wait()
        results.append(
            proxy_runtime.get_clearance_bundle(
                "chatgpt.com",
                profile={
                    "proxy_lease_id": "egress:stale-shared",
                    "fingerprint_id": f"chrome145:{suffix}",
                    "user_agent": f"UA/{suffix}",
                },
                require_clearance=True,
            )
        )

    threads = [threading.Thread(target=worker, args=(suffix,)) for suffix in ("a", "b")]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join(timeout=3)

    assert all(not thread.is_alive() for thread in threads)
    assert [result["status"] for result in results] == ["valid_clearance", "valid_clearance"]
    assert max_active_requests == 1
    assert [command for command, _session in calls].count("sessions.create") == 1
    assert [command for command, _session in calls].count("request.get") == 3
    request_sessions = [session for command, session in calls if command == "request.get"]
    assert request_sessions[0] == "stale-session"
    assert len(set(request_sessions[1:])) == 1


def test_solver_session_scope_reused_across_attempt_fingerprints(monkeypatch):
    proxy_runtime.invalidate_clearance()
    with proxy_runtime._LOCK:
        proxy_runtime._FS_SESSIONS.clear()
        proxy_runtime._FS_SESSION_SCOPES.clear()
    calls = []

    class Response:
        ok = True

        def __init__(self, body):
            self._body = body

        def json(self):
            return self._body

    def fake_post(_url, *, json, timeout):
        del timeout
        calls.append(json["cmd"])
        if json["cmd"] == "sessions.create":
            return Response({"status": "ok"})
        return Response(
            {
                "status": "ok",
                "solution": {
                    "status": 200,
                    "response": "ok",
                    "userAgent": "UA/solver",
                    "cookies": [{"name": "cf_clearance", "value": "value"}],
                },
            }
        )

    monkeypatch.setitem(__import__("sys").modules, "requests", SimpleNamespace(post=fake_post))
    settings = proxy_runtime.ProxyRuntimeSettings(
        enabled=True,
        clearance_mode="flaresolverr",
        flaresolverr_url="http://solver:8191",
        refresh_interval_sec=0,
    )
    monkeypatch.setattr(proxy_runtime.ProxyRuntimeSettings, "load", classmethod(lambda cls: settings))
    base = {"proxy_lease_id": "lease-scope"}
    first = proxy_runtime.get_clearance_bundle(
        "chatgpt.com",
        profile={**base, "fingerprint_id": "chrome145:attempt-a", "user_agent": "UA/first"},
        require_clearance=True,
    )
    second = proxy_runtime.get_clearance_bundle(
        "chatgpt.com",
        profile={**base, "fingerprint_id": "chrome145:attempt-b", "user_agent": "UA/second"},
        require_clearance=True,
    )

    assert first["status"] == second["status"] == "valid_clearance"
    assert calls.count("sessions.create") == 1
    assert calls.count("request.get") == 2


def test_shared_solver_session_serializes_concurrent_attempt_requests(monkeypatch):
    proxy_runtime.invalidate_clearance()
    with proxy_runtime._LOCK:
        proxy_runtime._FS_SESSIONS.clear()
        proxy_runtime._FS_SESSION_SCOPES.clear()
        proxy_runtime._FS_SESSION_LAST_USED.clear()
        proxy_runtime._FS_SCOPE_LOCKS.clear()
    calls = []
    activity_lock = threading.Lock()
    active_requests = 0
    max_active_requests = 0

    class Response:
        ok = True

        def __init__(self, body):
            self._body = body

        def json(self):
            return self._body

    def fake_post(_url, *, json, timeout):
        nonlocal active_requests, max_active_requests
        del timeout
        calls.append((json["cmd"], json.get("session"), json.get("url")))
        if json["cmd"] == "sessions.create":
            return Response({"status": "ok"})
        with activity_lock:
            active_requests += 1
            max_active_requests = max(max_active_requests, active_requests)
        time.sleep(0.05)
        with activity_lock:
            active_requests -= 1
        return Response(
            {
                "status": "ok",
                "solution": {
                    "status": 200,
                    "response": "ok",
                    "userAgent": "UA/solver",
                    "cookies": [{"name": "cf_clearance", "value": "value"}],
                },
            }
        )

    monkeypatch.setitem(__import__("sys").modules, "requests", SimpleNamespace(post=fake_post))
    settings = proxy_runtime.ProxyRuntimeSettings(
        enabled=True,
        clearance_mode="flaresolverr",
        flaresolverr_url="http://solver:8191",
        refresh_interval_sec=0,
    )
    monkeypatch.setattr(proxy_runtime.ProxyRuntimeSettings, "load", classmethod(lambda cls: settings))
    barrier = threading.Barrier(3)
    results = []

    def worker(suffix, target):
        barrier.wait()
        results.append(
            proxy_runtime.get_clearance_bundle(
                "auth.openai.com",
                profile={
                    "proxy_lease_id": "egress:shared",
                    "fingerprint_id": f"chrome145:{suffix}",
                    "user_agent": f"UA/{suffix}",
                },
                require_clearance=True,
                target_url=target,
            )
        )

    threads = [
        threading.Thread(
            target=worker,
            args=("a", "https://auth.openai.com/authorize?state=a"),
        ),
        threading.Thread(
            target=worker,
            args=("b", "https://auth.openai.com/authorize?state=b"),
        ),
    ]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join(timeout=3)

    assert all(not thread.is_alive() for thread in threads)
    assert [item["status"] for item in results] == ["valid_clearance", "valid_clearance"]
    assert max_active_requests == 1
    assert [item[0] for item in calls].count("sessions.create") == 1
    request_calls = [item for item in calls if item[0] == "request.get"]
    assert len(request_calls) == 2
    assert len({item[1] for item in request_calls}) == 1
    assert {item[2] for item in request_calls} == {
        "https://auth.openai.com/authorize?state=a",
        "https://auth.openai.com/authorize?state=b",
    }


def test_destroy_shared_solver_session_waits_for_last_alias(monkeypatch):
    proxy_runtime.invalidate_clearance()
    with proxy_runtime._LOCK:
        proxy_runtime._FS_SESSIONS.clear()
        proxy_runtime._FS_SESSION_SCOPES.clear()

    destroyed = []

    class Response:
        ok = True

        def __init__(self, body):
            self._body = body

        def json(self):
            return self._body

    def fake_post(_url, *, json, timeout):
        del timeout
        if json["cmd"] == "sessions.create":
            return Response({"status": "ok"})
        if json["cmd"] == "sessions.destroy":
            destroyed.append(json["session"])
            return Response({"status": "ok"})
        return Response(
            {
                "status": "ok",
                "solution": {
                    "status": 200,
                    "response": "ok",
                    "userAgent": "UA/solver",
                    "cookies": [{"name": "cf_clearance", "value": "value"}],
                },
            }
        )

    monkeypatch.setitem(__import__("sys").modules, "requests", SimpleNamespace(post=fake_post))
    settings = proxy_runtime.ProxyRuntimeSettings(
        enabled=True,
        clearance_mode="flaresolverr",
        flaresolverr_url="http://solver:8191",
        refresh_interval_sec=300,
    )
    monkeypatch.setattr(proxy_runtime.ProxyRuntimeSettings, "load", classmethod(lambda cls: settings))
    proxy_runtime.get_clearance_bundle(
        "chatgpt.com",
        profile={"proxy_lease_id": "lease-destroy", "fingerprint_id": "chrome145:a", "user_agent": "UA/1"},
        require_clearance=True,
    )
    proxy_runtime.get_clearance_bundle(
        "chatgpt.com",
        profile={"proxy_lease_id": "lease-destroy", "fingerprint_id": "chrome145:b", "user_agent": "UA/1"},
        require_clearance=True,
    )
    proxy_runtime.destroy_clearance_sessions(proxy_lease_id="lease-destroy", fingerprint_id="chrome145:a")
    assert destroyed == []
    proxy_runtime.destroy_clearance_sessions(proxy_lease_id="lease-destroy", fingerprint_id="chrome145:b")
    assert len(destroyed) == 1


def test_release_attempt_alias_retains_solver_session_for_next_attempt(monkeypatch):
    proxy_runtime.invalidate_clearance()
    with proxy_runtime._LOCK:
        proxy_runtime._FS_SESSIONS.clear()
        proxy_runtime._FS_SESSION_SCOPES.clear()
        proxy_runtime._FS_SESSION_LAST_USED.clear()
    calls = []

    class Response:
        ok = True

        def __init__(self, body):
            self._body = body

        def json(self):
            return self._body

    def fake_post(_url, *, json, timeout):
        del timeout
        calls.append(json["cmd"])
        if json["cmd"] == "sessions.create":
            return Response({"status": "ok"})
        return Response(
            {
                "status": "ok",
                "solution": {
                    "status": 200,
                    "response": "ok",
                    "userAgent": "UA/solver",
                    "cookies": [{"name": "cf_clearance", "value": "value"}],
                },
            }
        )

    monkeypatch.setitem(__import__("sys").modules, "requests", SimpleNamespace(post=fake_post))
    settings = proxy_runtime.ProxyRuntimeSettings(
        enabled=True,
        clearance_mode="flaresolverr",
        flaresolverr_url="http://solver:8191",
        refresh_interval_sec=0,
    )
    monkeypatch.setattr(proxy_runtime.ProxyRuntimeSettings, "load", classmethod(lambda cls: settings))
    first_profile = {
        "proxy_lease_id": "lease-retain",
        "fingerprint_id": "chrome145:first",
        "user_agent": "UA/first",
    }
    second_profile = {
        "proxy_lease_id": "lease-retain",
        "fingerprint_id": "chrome145:second",
        "user_agent": "UA/second",
    }

    proxy_runtime.get_clearance_bundle("chatgpt.com", profile=first_profile, require_clearance=True)
    proxy_runtime.release_clearance_aliases(
        proxy_lease_id="lease-retain",
        fingerprint_id="chrome145:first",
    )
    proxy_runtime.get_clearance_bundle("chatgpt.com", profile=second_profile, require_clearance=True)

    assert calls.count("sessions.create") == 1
    assert calls.count("request.get") == 2


def test_idle_released_solver_session_is_destroyed_after_ttl(monkeypatch):
    proxy_runtime.invalidate_clearance()
    with proxy_runtime._LOCK:
        proxy_runtime._FS_SESSIONS.clear()
        proxy_runtime._FS_SESSION_SCOPES.clear()
        proxy_runtime._FS_SESSION_LAST_USED.clear()
        proxy_runtime._FS_SCOPE_LOCKS.clear()
    calls = []

    class Response:
        ok = True

        def __init__(self, body):
            self._body = body

        def json(self):
            return self._body

    def fake_post(_url, *, json, timeout):
        del timeout
        calls.append((json["cmd"], json.get("session")))
        if json["cmd"] in {"sessions.create", "sessions.destroy"}:
            return Response({"status": "ok"})
        return Response(
            {
                "status": "ok",
                "solution": {
                    "status": 200,
                    "response": "ok",
                    "userAgent": "UA/solver",
                    "cookies": [{"name": "cf_clearance", "value": "value"}],
                },
            }
        )

    monkeypatch.setitem(__import__("sys").modules, "requests", SimpleNamespace(post=fake_post))
    settings = proxy_runtime.ProxyRuntimeSettings(
        enabled=True,
        clearance_mode="flaresolverr",
        flaresolverr_url="http://solver:8191",
        refresh_interval_sec=0,
    )
    monkeypatch.setattr(proxy_runtime.ProxyRuntimeSettings, "load", classmethod(lambda cls: settings))
    profile = {
        "proxy_lease_id": "egress:idle",
        "fingerprint_id": "chrome145:idle",
        "user_agent": "UA/idle",
    }

    proxy_runtime.get_clearance_bundle("chatgpt.com", profile=profile, require_clearance=True)
    proxy_runtime.release_clearance_aliases(
        proxy_lease_id="egress:idle",
        fingerprint_id="chrome145:idle",
    )
    with proxy_runtime._LOCK:
        session_id = next(iter(proxy_runtime._FS_SESSION_SCOPES))
        proxy_runtime._FS_SESSION_LAST_USED[session_id] = 100.0

    destroyed = proxy_runtime.cleanup_idle_clearance_sessions(
        max_idle_seconds=60,
        force=True,
        settings=settings,
        now=1000.0,
    )

    assert destroyed == 1
    assert calls[-1] == ("sessions.destroy", session_id)
    with proxy_runtime._LOCK:
        assert session_id not in proxy_runtime._FS_SESSION_SCOPES
        assert session_id not in proxy_runtime._FS_SESSION_LAST_USED


def test_flaresolverr_clearance_uses_singleflight_for_same_identity(monkeypatch):
    proxy_runtime.invalidate_clearance("auth.openai.com")
    calls = []
    calls_lock = threading.Lock()

    class Response:
        ok = True

        def __init__(self, body):
            self._body = body

        def json(self):
            return self._body

    def fake_post(_url, *, json, timeout):
        del timeout
        with calls_lock:
            calls.append(json["cmd"])
        if json["cmd"] == "sessions.create":
            return Response({"status": "ok"})
        time.sleep(0.05)
        return Response(
            {
                "status": "ok",
                "solution": {
                    "status": 200,
                    "response": "ok",
                    "userAgent": "UA/solver",
                    "cookies": [{"name": "cf_clearance", "value": "value"}],
                },
            }
        )

    monkeypatch.setitem(__import__("sys").modules, "requests", SimpleNamespace(post=fake_post))
    settings = proxy_runtime.ProxyRuntimeSettings(
        enabled=True,
        clearance_mode="flaresolverr",
        flaresolverr_url="http://solver:8191",
        refresh_interval_sec=300,
    )
    monkeypatch.setattr(proxy_runtime.ProxyRuntimeSettings, "load", classmethod(lambda cls: settings))
    barrier = threading.Barrier(3)
    results = []

    def worker():
        barrier.wait()
        results.append(
                proxy_runtime.get_clearance_bundle(
                    "auth.openai.com",
                profile={"proxy_lease_id": "lease", "fingerprint_id": "fp-single", "user_agent": "UA/1"},
                require_clearance=True,
            )
        )

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join(timeout=2)

    assert all(not thread.is_alive() for thread in threads)
    assert [item["status"] for item in results] == ["valid_clearance", "valid_clearance"]
    assert calls.count("sessions.create") == 1
    assert calls.count("request.get") == 1


def test_solver_supported_chrome_uses_exact_curl_profile():
    user_agent = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/146.0.7680.42 Safari/537.36"
    )

    aligned = align_chrome_profile_to_user_agent(
        {"key": "chrome136", "impersonate": "chrome136"},
        user_agent,
    )

    assert aligned["user_agent"] == user_agent
    assert aligned["major"] == 146
    assert aligned["tls_profile_major"] == 146
    assert aligned["impersonate"] == "chrome146"
    assert 'v="146"' in aligned["sec_ch_ua"]


def test_solver_unsupported_chrome_is_rejected_instead_of_mixing_tls_identity():
    user_agent = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/148.0.7715.42 Safari/537.36"
    )

    with pytest.raises(ValueError, match="AUTH_SESSION_DESYNC"):
        align_chrome_profile_to_user_agent(
            {"key": "chrome142", "impersonate": "chrome142"},
            user_agent,
        )


def test_clearance_alignment_aliases_cache_without_second_solver_request(monkeypatch):
    proxy_runtime.invalidate_clearance()
    with proxy_runtime._LOCK:
        proxy_runtime._FS_SESSIONS.clear()
    calls = []

    class Response:
        ok = True

        def __init__(self, body):
            self._body = body

        def json(self):
            return self._body

    def fake_post(_url, *, json, timeout):
        del timeout
        calls.append(json["cmd"])
        if json["cmd"] == "sessions.create":
            return Response({"status": "ok"})
        return Response(
            {
                "status": "ok",
                "solution": {
                    "status": 200,
                    "response": "ok",
                    "userAgent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/146.0.7680.42 Safari/537.36"
                    ),
                    "cookies": [{"name": "cf_clearance", "value": "value"}],
                },
            }
        )

    monkeypatch.setitem(__import__("sys").modules, "requests", SimpleNamespace(post=fake_post))
    settings = proxy_runtime.ProxyRuntimeSettings(
        enabled=True,
        clearance_mode="flaresolverr",
        flaresolverr_url="http://solver:8191",
        refresh_interval_sec=300,
    )
    monkeypatch.setattr(proxy_runtime.ProxyRuntimeSettings, "load", classmethod(lambda cls: settings))
    profile = {
        "key": "chrome136",
        "impersonate": "chrome136",
        "major": 136,
        "user_agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/136.0.7103.80 Safari/537.36"
        ),
        "proxy_lease_id": "lease-alias",
        "fingerprint_id": "fp-alias",
    }

    first = proxy_runtime.apply_clearance_to_profile(profile, "chatgpt.com", require_clearance=True)
    second = proxy_runtime.get_clearance_bundle("chatgpt.com", profile=profile, require_clearance=True)

    assert first["status"] == "valid_clearance"
    assert second["status"] == "valid_clearance"
    assert profile["major"] == 146
    assert calls == ["sessions.create", "request.get"]


def test_flaresolverr_session_is_shared_across_chatgpt_and_auth_origins(monkeypatch):
    proxy_runtime.invalidate_clearance()
    with proxy_runtime._LOCK:
        proxy_runtime._FS_SESSIONS.clear()
    calls = []

    class Response:
        ok = True

        def __init__(self, body):
            self._body = body

        def json(self):
            return self._body

    def fake_post(_url, *, json, timeout):
        del timeout
        calls.append((json["cmd"], json.get("session"), json.get("url")))
        if json["cmd"] == "sessions.create":
            return Response({"status": "ok"})
        return Response(
            {
                "status": "ok",
                "solution": {
                    "status": 200,
                    "response": "ok",
                    "userAgent": "UA/solver",
                    "cookies": [{"name": "cf_clearance", "value": "value"}],
                },
            }
        )

    monkeypatch.setitem(__import__("sys").modules, "requests", SimpleNamespace(post=fake_post))
    settings = proxy_runtime.ProxyRuntimeSettings(
        enabled=True,
        clearance_mode="flaresolverr",
        flaresolverr_url="http://solver:8191",
        refresh_interval_sec=300,
    )
    monkeypatch.setattr(proxy_runtime.ProxyRuntimeSettings, "load", classmethod(lambda cls: settings))
    profile = {"proxy_lease_id": "lease-shared", "fingerprint_id": "fp-shared", "user_agent": "UA/1"}

    proxy_runtime.get_clearance_bundle("chatgpt.com", profile=profile)
    proxy_runtime.get_clearance_bundle("auth.openai.com", profile=profile)

    assert [item[0] for item in calls].count("sessions.create") == 1
    request_sessions = [item[1] for item in calls if item[0] == "request.get"]
    assert len(request_sessions) == 2
    assert len(set(request_sessions)) == 1

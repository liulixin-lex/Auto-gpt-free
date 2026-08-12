from __future__ import annotations

import base64
import hashlib
import json
from urllib.parse import parse_qs, urlencode, urlparse

import pytest

from domain.registration_runtime import RegistrationStage
from platforms.chatgpt.constants import (
    CHATGPT_APP,
    CODEX_REDIRECT_URI,
    OPENAI_API_ENDPOINTS,
    OPENAI_AUTH,
    OAUTH_TOKEN_URL,
    SENTINEL_REQ_URL,
)
from platforms.chatgpt.plugin import ChatGPTPlatform
from platforms.chatgpt.protocol_register import ChatGPTProtocolRegister, OpenAISentinelClient


class _FakeCookies:
    def __init__(self):
        self._values = {"oai-did": "device-from-cookie"}

    def get(self, key):
        return self._values.get(key)

    def set(self, key, value, **_kwargs):
        self._values[key] = value

    def get_dict(self):
        return dict(self._values)


class _FakeResponse:
    def __init__(self, status_code=200, payload=None, *, headers=None, text=""):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.headers = headers or {}
        self.text = text

    def json(self):
        return self._payload


class _FakeSession:
    def __init__(self):
        self.cookies = _FakeCookies()
        self.calls = []
        self.create_headers = {}
        self.password_body = {}
        self.oauth_authorize_params = {}
        self.oauth_token_body = {}
        self.signin_query = {}
        self.submitted_email = ""
        self.otp_send_calls = 0
        self.account_created = False
        self.closed = False

    def get(self, url, **kwargs):
        self.calls.append(("GET", url, kwargs))
        if url == f"{CHATGPT_APP}/api/auth/csrf":
            return _FakeResponse(payload={"csrfToken": "csrf-token"})
        if url == "https://auth.openai.com/authorize-start":
            return _FakeResponse(headers={"location": "/create-account/password"})
        if url == f"{OPENAI_AUTH}/create-account/password":
            return _FakeResponse()
        if url == OPENAI_API_ENDPOINTS["send_otp"]:
            self.otp_send_calls += 1
            return _FakeResponse(status_code=204)
        if url.startswith(f"{OPENAI_AUTH}/oauth/authorize?"):
            self.oauth_authorize_params = parse_qs(urlparse(url).query)
            callback = CODEX_REDIRECT_URI + "?" + urlencode(
                {
                    "code": "oauth-code",
                    "state": self.oauth_authorize_params["state"][0],
                    "scope": "openid profile email offline_access",
                }
            )
            return _FakeResponse(status_code=302, headers={"location": callback})
        if url == f"{CHATGPT_APP}/api/auth/session":
            if not self.account_created:
                return _FakeResponse(payload={})
            return _FakeResponse(
                payload={
                    "accessToken": "header.payload.signature",
                    "sessionToken": "session-token",
                    "expires": "2026-08-01T00:00:00Z",
                    "account": {"id": "account-123", "planType": "free"},
                }
            )
        return _FakeResponse()

    def post(self, url, **kwargs):
        self.calls.append(("POST", url, kwargs))
        if url.startswith(f"{CHATGPT_APP}/api/auth/signin/openai?"):
            self.signin_query = parse_qs(urlparse(url).query)
            return _FakeResponse(payload={"url": "https://auth.openai.com/authorize-start"})
        if url == OPENAI_API_ENDPOINTS["signup"]:
            assert kwargs["json"].get("screen_hint") == "signup"
            username = kwargs["json"].get("username") or {}
            assert set(username) == {"value", "kind"}
            assert username["kind"] == "email"
            self.submitted_email = str(username.get("value") or "")
            return _FakeResponse(
                payload={
                    "page": {"type": "create_account_password"},
                    "continue_url": "/create-account/password",
                    "method": "GET",
                }
            )
        if url == OPENAI_API_ENDPOINTS["validate_otp"]:
            assert kwargs["json"] == {"code": "123456"}
            return _FakeResponse(payload={"continue_url": "/about-you"})
        if url == SENTINEL_REQ_URL:
            request_payload = json.loads(kwargs["data"])
            return _FakeResponse(
                payload={
                    "token": "challenge-token",
                    "proofofwork": {"required": False},
                    "flow": request_payload["flow"],
                }
            )
        if url == OPENAI_API_ENDPOINTS["create_account"]:
            self.create_headers = kwargs["headers"]
            self.account_created = True
            return _FakeResponse(
                payload={
                    "continue_url": f"{CHATGPT_APP}/api/auth/callback/openai?code=ok&state=test"
                }
            )
        if url == OPENAI_API_ENDPOINTS["register"]:
            self.password_body = kwargs["json"]
            return _FakeResponse(
                payload={
                    "page": {"type": "email_otp_send"},
                    "continue_url": "/email-otp/send",
                }
            )
        if url == OAUTH_TOKEN_URL:
            self.oauth_token_body = dict(kwargs["data"])
            return _FakeResponse(
                payload={
                    "access_token": "oauth-access-token",
                    "refresh_token": "oauth-refresh-token",
                    "id_token": "oauth-id-token",
                    "expires_in": 3600,
                }
            )
        raise AssertionError(f"unexpected POST {url}")

    def close(self):
        self.closed = True


class _FakeSentinelRuntime:
    def vm_tokens(self, _chat_req, _cached_proof):
        return {"t": "turnstile-proof", "so": "observer-proof"}

    def close(self):
        return None


def _attach_fake_sentinel_runtime(worker: ChatGPTProtocolRegister) -> None:
    worker.sentinel.use_browser_runtime = True
    worker.sentinel._browser_runtime = _FakeSentinelRuntime()


def test_protocol_attempts_get_distinct_clearance_fingerprints():
    profile = {
        "key": "chrome142",
        "impersonate": "chrome142",
        "user_agent": "Mozilla/5.0 Chrome/142.0.7444.100 Safari/537.36",
    }
    first = ChatGPTProtocolRegister(
        session=_FakeSession(),
        browser_profile=profile,
        attempt_id="attempt-a",
        sentinel_runtime=False,
    )
    second = ChatGPTProtocolRegister(
        session=_FakeSession(),
        browser_profile=profile,
        attempt_id="attempt-b",
        sentinel_runtime=False,
    )

    assert first.transport_identity.fingerprint_id != second.transport_identity.fingerprint_id
    assert first.transport_identity.fingerprint_id.startswith("chrome142:")
    assert second.transport_identity.fingerprint_id.startswith("chrome142:")


def test_protocol_register_completes_email_flow_without_browser():
    session = _FakeSession()
    logs = []
    worker = ChatGPTProtocolRegister(
        session=session,
        otp_callback=lambda: "123456",
        log_fn=logs.append,
        sentinel_runtime=True,
    )
    _attach_fake_sentinel_runtime(worker)

    result = worker.run(email="user@outlook.com", password="StrongPass123!")

    assert result["email"] == "user@outlook.com"
    assert result["password"] == "StrongPass123!"
    assert result["access_token"] == "oauth-access-token"
    assert result["session_access_token"] == "header.payload.signature"
    assert result["session_token"] == "session-token"
    assert result["refresh_token"] == "oauth-refresh-token"
    assert result["id_token"] == "oauth-id-token"
    assert result["oauth_status"] == "complete"
    assert result["account_id"] == "account-123"
    assert session.password_body == {
        "username": "user@outlook.com",
        "password": "StrongPass123!",
    }
    assert session.signin_query["ext-oai-did"] == ["device-from-cookie"]
    assert session.otp_send_calls == 1
    registration_calls = [
        (method, url)
        for method, url, _kwargs in session.calls
        if url in {
            OPENAI_API_ENDPOINTS["register"],
            OPENAI_API_ENDPOINTS["send_otp"],
            OPENAI_API_ENDPOINTS["validate_otp"],
            OPENAI_API_ENDPOINTS["create_account"],
        }
    ]
    assert registration_calls == [
        ("POST", OPENAI_API_ENDPOINTS["register"]),
        ("GET", OPENAI_API_ENDPOINTS["send_otp"]),
        ("POST", OPENAI_API_ENDPOINTS["validate_otp"]),
        ("POST", OPENAI_API_ENDPOINTS["create_account"]),
    ]
    assert session.closed is True
    sentinel = json.loads(session.create_headers["openai-sentinel-token"])
    assert sentinel["flow"] == "oauth_create_account"
    assert sentinel["c"] == "challenge-token"
    verifier = session.oauth_token_body["code_verifier"]
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode("ascii")).digest()
    ).decode("ascii").rstrip("=")
    assert session.oauth_authorize_params["code_challenge"] == [challenge]
    assert session.oauth_token_body["code"] == "oauth-code"
    assert any("协议注册完成" in line for line in logs)


def test_protocol_registration_keeps_web_session_when_pkce_is_recoverable():
    class _PkceUnavailableSession(_FakeSession):
        def get(self, url, **kwargs):
            if url.startswith(f"{OPENAI_AUTH}/oauth/authorize?"):
                self.calls.append(("GET", url, kwargs))
                return _FakeResponse(status_code=200)
            return super().get(url, **kwargs)

    session = _PkceUnavailableSession()
    worker = ChatGPTProtocolRegister(
        session=session,
        otp_callback=lambda: "123456",
        sentinel_runtime=True,
    )
    _attach_fake_sentinel_runtime(worker)

    result = worker.run(email="user@outlook.com", password="StrongPass123!")

    assert result["access_token"] == "header.payload.signature"
    assert result["refresh_token"] == ""
    assert result["oauth_status"] == "recoverable"
    assert result["oauth_error_code"] == "AUTH_REDIRECT"


def test_protocol_registration_accepts_current_chatgpt_otp_subjects():
    adapter = ChatGPTPlatform().build_protocol_mailbox_adapter()

    # Current messages are titled "Your temporary ChatGPT ... code" and may
    # not contain the old OpenAI brand keyword.
    assert adapter.otp_spec is not None
    assert adapter.otp_spec.keyword == ""


def test_protocol_does_not_duplicate_otp_when_authorize_already_triggered_email():
    class _OtpVerificationLandingSession(_FakeSession):
        def get(self, url, **kwargs):
            if url == "https://auth.openai.com/authorize-start":
                self.calls.append(("GET", url, kwargs))
                return _FakeResponse(headers={"location": "/email-verification"})
            if url == f"{OPENAI_AUTH}/email-verification":
                self.calls.append(("GET", url, kwargs))
                return _FakeResponse()
            return super().get(url, **kwargs)

    session = _OtpVerificationLandingSession()
    received = []
    worker = ChatGPTProtocolRegister(
        session=session,
        otp_callback=lambda: received.append(True) or "123456",
        sentinel_runtime=True,
    )
    _attach_fake_sentinel_runtime(worker)

    worker.run(email="fresh@example.com", password="StrongPass123!")

    assert received == [True]
    assert session.otp_send_calls == 0
    assert not any(
        url == OPENAI_API_ENDPOINTS["register"]
        for method, url, _kwargs in session.calls
        if method == "POST"
    )


def test_protocol_submits_email_when_authorize_chain_stops_at_login():
    class _LoginLandingSession(_FakeSession):
        def get(self, url, **kwargs):
            if url == "https://auth.openai.com/authorize-start":
                self.calls.append(("GET", url, kwargs))
                return _FakeResponse(headers={"location": "/log-in"})
            if url == f"{OPENAI_AUTH}/log-in":
                self.calls.append(("GET", url, kwargs))
                return _FakeResponse()
            return super().get(url, **kwargs)

    session = _LoginLandingSession()
    worker = ChatGPTProtocolRegister(
        session=session,
        otp_callback=lambda: "123456",
        sentinel_runtime=True,
    )
    _attach_fake_sentinel_runtime(worker)

    worker.run(email="fresh@example.com", password="StrongPass123!")

    assert session.submitted_email == "fresh@example.com"
    assert session.otp_send_calls == 1


def test_protocol_retries_nextauth_when_first_signin_returns_html():
    class _TransientHtmlSigninSession(_FakeSession):
        def __init__(self):
            super().__init__()
            self.signin_calls = 0

        def post(self, url, **kwargs):
            if url.startswith(f"{CHATGPT_APP}/api/auth/signin/openai?"):
                self.signin_calls += 1
                if self.signin_calls == 1:
                    self.calls.append(("POST", url, kwargs))
                    return _FakeResponse(
                        status_code=200,
                        headers={"content-type": "text/html; charset=utf-8"},
                        text="<html>temporary upstream page</html>",
                    )
            return super().post(url, **kwargs)

    session = _TransientHtmlSigninSession()
    worker = ChatGPTProtocolRegister(
        session=session,
        otp_callback=lambda: "123456",
        sentinel_runtime=True,
    )
    _attach_fake_sentinel_runtime(worker)

    worker.run(email="fresh@example.com", password="StrongPass123!")

    assert session.signin_calls == 2
    assert session.otp_send_calls == 1


def test_protocol_refreshes_auth_clearance_after_cloudflare_challenge(monkeypatch):
    class _CloudflareOnceSession(_FakeSession):
        def __init__(self):
            super().__init__()
            self.authorize_calls = 0

        def get(self, url, **kwargs):
            if url == "https://auth.openai.com/authorize-start":
                self.calls.append(("GET", url, kwargs))
                self.authorize_calls += 1
                if self.authorize_calls == 1:
                    return _FakeResponse(
                        status_code=403,
                        headers={"content-type": "text/html; charset=UTF-8"},
                        text="<html><title>Just a moment...</title><body>cf-chl</body></html>",
                    )
                return _FakeResponse(headers={"location": "/create-account/password"})
            return super().get(url, **kwargs)

    clearance_calls = []

    def fake_apply_clearance(profile, host, *, force=False, proxy_url=None):
        del profile, proxy_url
        clearance_calls.append((host, force))
        return {
            "cookie": "cf_clearance=test-only",
            "source": "test",
            "has_cf_clearance": True,
        }

    monkeypatch.setattr(
        "core.proxy_runtime.apply_clearance_to_profile",
        fake_apply_clearance,
    )
    session = _CloudflareOnceSession()
    worker = ChatGPTProtocolRegister(
        session=session,
        otp_callback=lambda: "123456",
        sentinel_runtime=True,
    )
    _attach_fake_sentinel_runtime(worker)

    worker.run(email="fresh@example.com", password="StrongPass123!")

    assert session.authorize_calls == 2
    assert ("auth.openai.com", True) in clearance_calls
    assert session.otp_send_calls == 1


def test_protocol_accepts_normal_auth_page_with_sentinel_references(monkeypatch):
    class _SentinelPageSession(_FakeSession):
        def get(self, url, **kwargs):
            if url == "https://auth.openai.com/authorize-start":
                self.calls.append(("GET", url, kwargs))
                return _FakeResponse(headers={"location": "/email-verification"})
            if url == f"{OPENAI_AUTH}/email-verification":
                self.calls.append(("GET", url, kwargs))
                return _FakeResponse(
                    status_code=200,
                    headers={"content-type": "text/html; charset=UTF-8"},
                    text="<html><body><script src='/sentinel/sdk.js'></script></body></html>",
                )
            return super().get(url, **kwargs)

    clearance_calls = []

    def fake_apply_clearance(profile, host, *, force=False, proxy_url=None):
        del profile, proxy_url
        clearance_calls.append((host, force))
        return {
            "cookie": "cf_clearance=test-only",
            "source": "test",
            "has_cf_clearance": True,
        }

    monkeypatch.setattr(
        "core.proxy_runtime.apply_clearance_to_profile",
        fake_apply_clearance,
    )
    worker = ChatGPTProtocolRegister(
        session=_SentinelPageSession(),
        otp_callback=lambda: "123456",
        sentinel_runtime=False,
    )

    state = worker._initialize_signup("fresh@example.com")

    assert state["page_type"] == "email_otp_verification"
    assert clearance_calls == []


def test_protocol_ignores_plain_solver_ua_when_clearance_is_not_required(monkeypatch):
    monkeypatch.setattr(
        "core.proxy_runtime.apply_clearance_to_profile",
        lambda *_args, **_kwargs: {
            "status": "not_required",
            "cookie": "__cf_bm=ordinary",
            "user_agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/148.0.7715.42 Safari/537.36"
            ),
            "source": "test",
            "has_cf_clearance": False,
            "challenge_detected": False,
        },
    )
    worker = ChatGPTProtocolRegister(
        session=_FakeSession(),
        otp_callback=lambda: "123456",
        sentinel_runtime=False,
    )
    original_ua = worker.user_agent

    assert worker._bind_clearance(host="chatgpt.com") is True
    assert worker.user_agent == original_ua


def test_protocol_aligns_solver_ua_before_retry_when_required_without_cookie(monkeypatch):
    created = []

    class FactorySession(_FakeSession):
        def __init__(self, **kwargs):
            super().__init__()
            self.kwargs = kwargs
            created.append(self)

    solver_ua = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/146.0.7680.42 Safari/537.36"
    )

    monkeypatch.setattr(
        "platforms.chatgpt.protocol_register.requests.Session",
        FactorySession,
    )
    monkeypatch.setattr(
        "core.proxy_runtime.apply_clearance_to_profile",
        lambda *_args, **_kwargs: {
            "status": "not_required",
            "cookie": "__cf_bm=ordinary",
            "user_agent": solver_ua,
            "source": "test",
            "has_cf_clearance": False,
            "challenge_detected": False,
        },
    )
    worker = ChatGPTProtocolRegister(
        otp_callback=lambda: "123456",
        sentinel_runtime=False,
    )
    original = worker.session

    assert worker._bind_clearance(
        host="auth.openai.com",
        required=True,
        target_url="https://auth.openai.com/api/accounts/authorize?state=fixture",
    ) is True
    assert original.closed is True
    assert worker.session is created[-1]
    assert worker.user_agent == solver_ua
    assert worker.profile["impersonate"] == "chrome146"
    assert worker.transport_identity.curl_impersonate == "chrome146"


def test_protocol_api_first_skips_full_chatgpt_homepage():
    session = _FakeSession()
    worker = ChatGPTProtocolRegister(
        session=session,
        otp_callback=lambda: "123456",
        sentinel_runtime=False,
    )

    state = worker._initialize_signup("fresh@example.com")

    assert state["page_type"] == "create_account_password"
    assert not any(method == "GET" and url == CHATGPT_APP for method, url, _kwargs in session.calls)


def test_protocol_recreates_owned_transport_when_real_clearance_changes_ua(monkeypatch):
    created = []

    class FactorySession(_FakeSession):
        def __init__(self, **kwargs):
            super().__init__()
            self.kwargs = kwargs
            created.append(self)

    solver_ua = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/146.0.7680.42 Safari/537.36"
    )

    def fake_apply(profile, *_args, **_kwargs):
        from platforms.chatgpt.browser_profiles import align_chrome_profile_to_user_agent

        profile.update(align_chrome_profile_to_user_agent(profile, solver_ua))
        return {
            "status": "valid_clearance",
            "cookie": "cf_clearance=test-only",
            "user_agent": solver_ua,
            "source": "test",
            "has_cf_clearance": True,
        }

    monkeypatch.setattr("platforms.chatgpt.protocol_register.requests.Session", FactorySession)
    monkeypatch.setattr("core.proxy_runtime.apply_clearance_to_profile", fake_apply)
    worker = ChatGPTProtocolRegister(
        otp_callback=lambda: "123456",
        sentinel_runtime=False,
    )
    original = worker.session

    assert worker._bind_clearance(host="auth.openai.com", required=True) is True
    assert len(created) == 2
    assert original.closed is True
    assert worker.session is created[-1]
    assert worker.user_agent == solver_ua
    assert worker.profile["impersonate"] == "chrome146"
    assert created[-1].kwargs["impersonate"] == worker.transport_identity.curl_impersonate == "chrome146"
    assert worker.session.cookies.get("cf_clearance") == "test-only"


def test_protocol_owned_session_uses_transport_identity_default(monkeypatch):
    created = []

    class FactorySession(_FakeSession):
        def __init__(self, **kwargs):
            super().__init__()
            self.kwargs = kwargs
            created.append(self)

    monkeypatch.setattr("platforms.chatgpt.protocol_register.requests.Session", FactorySession)
    worker = ChatGPTProtocolRegister(otp_callback=lambda: "123456", sentinel_runtime=False)

    assert created
    assert created[0].kwargs["impersonate"] == worker.transport_identity.curl_impersonate
    assert created[0].kwargs["impersonate"] == worker.profile["impersonate"]


def test_protocol_stops_when_auth_cloudflare_challenge_persists(monkeypatch):
    clearance_targets = []

    class _PersistentCloudflareSession(_FakeSession):
        def __init__(self):
            super().__init__()
            self.authorize_calls = 0

        def get(self, url, **kwargs):
            if url == "https://auth.openai.com/authorize-start":
                self.calls.append(("GET", url, kwargs))
                self.authorize_calls += 1
                return _FakeResponse(
                    status_code=403,
                    headers={"content-type": "text/html; charset=UTF-8"},
                    text="<html><title>Just a moment...</title><body>cf-chl</body></html>",
                )
            return super().get(url, **kwargs)

    def fake_apply(*_args, **kwargs):
        clearance_targets.append(str(kwargs.get("target_url") or ""))
        return {
            "cookie": "cf_clearance=test-only",
            "source": "test",
            "has_cf_clearance": True,
        }

    monkeypatch.setattr("core.proxy_runtime.apply_clearance_to_profile", fake_apply)
    session = _PersistentCloudflareSession()
    worker = ChatGPTProtocolRegister(
        session=session,
        otp_callback=lambda: "123456",
        sentinel_runtime=False,
    )

    with pytest.raises(RuntimeError, match="CF_CHALLENGE"):
        worker._initialize_signup("fresh@example.com")

    assert session.authorize_calls == 2
    assert clearance_targets == ["https://auth.openai.com/authorize-start"]
    assert not any(
        url == OPENAI_API_ENDPOINTS["signup"]
        for method, url, _kwargs in session.calls
        if method == "POST"
    )


def test_protocol_exact_challenge_target_keeps_query_out_of_logs(monkeypatch):
    clearance_targets = []
    logs = []
    sensitive_state = "state-do-not-log"
    sensitive_challenge = "challenge-do-not-log"
    target = (
        "https://auth.openai.com/authorize-start"
        f"?state={sensitive_state}&code_challenge={sensitive_challenge}#local-fragment"
    )
    sanitized_target = target.split("#", 1)[0]

    class QueryChallengeSession(_FakeSession):
        def get(self, url, **kwargs):
            if url.startswith("https://auth.openai.com/authorize-start?"):
                self.calls.append(("GET", url, kwargs))
                return _FakeResponse(
                    status_code=403,
                    headers={"content-type": "text/html; charset=UTF-8"},
                    text="<html><title>Just a moment...</title><body>cf-chl</body></html>",
                )
            return super().get(url, **kwargs)

    def fake_apply(*_args, **kwargs):
        clearance_targets.append(str(kwargs.get("target_url") or ""))
        return {
            "status": "valid_clearance",
            "cookie": "cf_clearance=test-only",
            "source": "test",
            "has_cf_clearance": True,
        }

    monkeypatch.setattr("core.proxy_runtime.apply_clearance_to_profile", fake_apply)
    worker = ChatGPTProtocolRegister(
        session=QueryChallengeSession(),
        otp_callback=lambda: "123456",
        sentinel_runtime=False,
        log_fn=logs.append,
    )

    with pytest.raises(RuntimeError, match="CF_CHALLENGE"):
        worker._follow_authorize_chain(target)

    assert clearance_targets == [sanitized_target]
    rendered_logs = "\n".join(logs)
    assert sensitive_state not in rendered_logs
    assert sensitive_challenge not in rendered_logs
    assert "local-fragment" not in rendered_logs


def test_protocol_rejects_external_authorize_redirect_before_request():
    session = _FakeSession()
    logs = []
    worker = ChatGPTProtocolRegister(
        session=session,
        otp_callback=lambda: "123456",
        sentinel_runtime=False,
        log_fn=logs.append,
    )

    with pytest.raises(RuntimeError, match="AUTH_REDIRECT"):
        worker._follow_authorize_chain(
            "https://evil.example/authorize?state=redirect-secret"
        )

    assert session.calls == []
    assert "redirect-secret" not in "\n".join(logs)


def test_protocol_clearance_binding_keeps_foreign_identity_cookies_out(monkeypatch):
    monkeypatch.setattr(
        "core.proxy_runtime.apply_clearance_to_profile",
        lambda *_args, **_kwargs: {
            "cookie": (
                "cf_clearance=test-only; __cf_bm=transport-only; "
                "oai-did=foreign-device; oaicom-stable-id=foreign-stable"
            ),
            "source": "test",
            "has_cf_clearance": True,
        },
    )
    session = _FakeSession()
    worker = ChatGPTProtocolRegister(
        session=session,
        otp_callback=lambda: "123456",
        sentinel_runtime=False,
    )

    assert worker._bind_clearance(host="auth.openai.com") is True

    assert session.cookies.get("cf_clearance") == "test-only"
    assert session.cookies.get("__cf_bm") == "transport-only"
    assert session.cookies.get("oai-did") == "device-from-cookie"
    assert session.cookies.get("oaicom-stable-id") is None
    assert "cookie" not in {
        key.lower(): value
        for key, value in worker._common_headers(f"{OPENAI_AUTH}/email-verification").items()
    }


def test_protocol_reinitializes_once_after_authorize_invalid_state():
    class _InvalidStateOnceSession(_FakeSession):
        def __init__(self):
            super().__init__()
            self.signup_calls = 0

        def get(self, url, **kwargs):
            if url == "https://auth.openai.com/authorize-start":
                self.calls.append(("GET", url, kwargs))
                return _FakeResponse(headers={"location": "/log-in"})
            if url == f"{OPENAI_AUTH}/log-in":
                self.calls.append(("GET", url, kwargs))
                return _FakeResponse()
            return super().get(url, **kwargs)

        def post(self, url, **kwargs):
            if url == OPENAI_API_ENDPOINTS["signup"]:
                self.signup_calls += 1
                if self.signup_calls == 1:
                    self.calls.append(("POST", url, kwargs))
                    return _FakeResponse(
                        status_code=400,
                        payload={
                            "error": {
                                "code": "invalid_state",
                                "message": "login session expired",
                            }
                        },
                    )
            return super().post(url, **kwargs)

    session = _InvalidStateOnceSession()
    worker = ChatGPTProtocolRegister(
        session=session,
        otp_callback=lambda: "123456",
        sentinel_runtime=True,
    )
    _attach_fake_sentinel_runtime(worker)

    worker.run(email="fresh@example.com", password="StrongPass123!")

    assert session.signup_calls == 2
    assert session.otp_send_calls == 1


def test_protocol_does_not_wait_for_mail_when_otp_trigger_fails():
    class _SendFailureSession(_FakeSession):
        def get(self, url, **kwargs):
            if url == OPENAI_API_ENDPOINTS["send_otp"]:
                self.calls.append(("GET", url, kwargs))
                self.otp_send_calls += 1
                return _FakeResponse(status_code=503, text="temporary failure")
            return super().get(url, **kwargs)

    session = _SendFailureSession()
    receive_calls = []
    stages = []
    worker = ChatGPTProtocolRegister(
        session=session,
        otp_callback=lambda: receive_calls.append(True) or "123456",
        sentinel_runtime=True,
        stage_callback=lambda stage, _message, _action: stages.append(stage),
    )
    _attach_fake_sentinel_runtime(worker)

    with pytest.raises(RuntimeError, match="触发邮箱验证码失败"):
        worker.run(email="fresh@example.com", password="StrongPass123!")

    assert session.otp_send_calls == 1
    assert receive_calls == []
    assert RegistrationStage.OTP_TRIGGER not in stages


def test_sentinel_headers_include_vm_and_session_observer_tokens():
    class _FakeRuntime:
        def vm_tokens(self, chat_req, cached_proof):
            return {"t": "turnstile-proof", "so": "observer-proof"}

    session = type(
        "NoNetworkSession",
        (),
        {"post": lambda *args, **kwargs: None},
    )()
    client = OpenAISentinelClient(
        session=session,
        user_agent="test-agent",
        use_browser_runtime=True,
    )
    client._browser_runtime = _FakeRuntime()

    # Bypass the network challenge and exercise the header assembly using a
    # deterministic VM result.
    def fake_post(*args, **kwargs):
        return _FakeResponse(
            payload={
                "token": "challenge",
                "proofofwork": {"required": False},
            }
        )

    session.post = fake_post
    headers = client.build_headers("device-1", "oauth_create_account")
    assert set(headers) == {
        "openai-sentinel-token",
        "openai-sentinel-so-token",
    }
    token = json.loads(headers["openai-sentinel-token"])
    so_token = json.loads(headers["openai-sentinel-so-token"])
    assert token["t"] == "turnstile-proof"
    assert so_token["so"] == "observer-proof"


def test_sentinel_rebuilds_runtime_after_transient_failure():
    class _ClosableRuntime:
        def __init__(self):
            self.closed = False

        def close(self):
            self.closed = True

    runtime = _ClosableRuntime()
    client = OpenAISentinelClient(
        session=object(),
        user_agent="test-agent",
        use_browser_runtime=True,
    )
    client._browser_runtime = runtime
    calls = []

    def flaky_build(_device_id, _flow):
        calls.append(True)
        if len(calls) == 1:
            raise RuntimeError("temporary VM context failure")
        return {"openai-sentinel-token": "test-token"}

    client._build_browser_headers = flaky_build

    headers = client.build_headers("device-1", "oauth_create_account")

    assert headers == {"openai-sentinel-token": "test-token"}
    assert len(calls) == 2
    assert runtime.closed is True
    assert client._browser_failed is False


def test_sentinel_persistent_failure_reports_cause_after_one_rebuild():
    client = OpenAISentinelClient(
        session=object(),
        user_agent="test-agent",
        use_browser_runtime=True,
    )
    calls = []

    def broken_build(_device_id, _flow):
        calls.append(True)
        raise ValueError("VM execution rejected")

    client._build_browser_headers = broken_build

    with pytest.raises(RuntimeError, match="type=ValueError"):
        client.build_headers("device-1", "oauth_create_account")

    assert len(calls) == 2
    assert client._browser_failed is True

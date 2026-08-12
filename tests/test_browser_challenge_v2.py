from __future__ import annotations

from types import SimpleNamespace

import pytest

from domain.challenge_runtime import ChallengeKind
from domain.registration_runtime import RegistrationStage
from platforms.chatgpt import browser_register
from platforms.chatgpt.browser_challenge import BrowserChallengeGuard, classify_browser_page
from providers.captcha.twocaptcha import TwoCaptcha
from providers.captcha.yescaptcha import YesCaptcha


class _ChallengePage:
    def __init__(self, content: str, *, url: str = "https://auth.openai.com/signup") -> None:
        self.url = url
        self._content = content
        self.injected = ""
        self._callbacks = {}

    def content(self):
        return self._content

    def evaluate(self, script, value=None):
        if "data-sitekey" in script and value is None:
            return "site-key"
        if "cf-turnstile-response" in script:
            self.injected = str(value or "")
            return True
        return ""

    def wait_for_timeout(self, _milliseconds):
        return None

    def on(self, event, callback):
        self._callbacks[event] = callback


def test_browser_page_classifier_detects_managed_challenge():
    result = classify_browser_page(_ChallengePage("<title>Just a moment</title><div>cf-chl-test</div>"))
    assert result.kind is ChallengeKind.CLOUDFLARE_MANAGED


def test_browser_page_keeps_managed_precedence_over_embedded_turnstile():
    page = _ChallengePage(
        "<title>Performing security verification</title>"
        "<div class='cf-turnstile'></div><div>cf-chl-test</div>"
    )

    assert classify_browser_page(page).kind is ChallengeKind.CLOUDFLARE_MANAGED


def test_browser_page_ignores_background_cloudflare_detection_on_otp_form():
    page = _ChallengePage(
        "<title>Check your inbox - OpenAI</title>"
        "<form><input name='code' autocomplete='one-time-code' inputmode='numeric' maxlength='6'>"
        "<button>Continue</button></form>"
        "<iframe hidden src='/cdn-cgi/challenge-platform/h/g/jsd/r/test'></iframe>"
        "<script src='/cdn-cgi/challenge-platform/scripts/jsd/main.js'></script>"
        "<script>window._cf_chl_opt={cType:'non-interactive',chlApiRq:'cf-chl-test'}</script>",
        url="https://auth.openai.com/email-verification",
    )

    assert classify_browser_page(page).kind is ChallengeKind.NONE


def test_react_aria_date_validation_rejects_truncated_year_fixture():
    assert browser_register._react_aria_date_values_valid(
        month_value="2",
        day_value="19",
        year_value="91",
        hidden_value="",
        expected_month="02",
        expected_day="19",
        expected_year="1991",
    ) is False


def test_react_aria_date_validation_accepts_complete_iso_value():
    assert browser_register._react_aria_date_values_valid(
        month_value="2",
        day_value="19",
        year_value="1991",
        hidden_value="1991-02-19",
        expected_month="02",
        expected_day="19",
        expected_year="1991",
    ) is True


class _EmptyLocator:
    def count(self):
        return 0


class _OtpPage:
    url = "https://auth.openai.com/email-verification"

    def __init__(self, native_result):
        self.native_result = native_result

    def evaluate(self, _script, value=None):
        assert value["code"] == "123456"
        return dict(self.native_result)

    def locator(self, _selector):
        return _EmptyLocator()


def test_otp_native_input_accepts_real_single_field_contract_without_logging_code():
    logs = []
    result = browser_register._fill_otp_input(
        _OtpPage(
            {
                "ok": True,
                "method": "native-single",
                "candidates": 1,
                "visible": 1,
                "enabled": 1,
                "readbackLength": 6,
            }
        ),
        "123456",
        logs.append,
    )

    assert result["ok"] is True
    assert result["method"] == "native-single"
    assert all("123456" not in line for line in logs)


def test_otp_interaction_failure_retains_safe_candidate_diagnostics():
    logs = []
    result = browser_register._fill_otp_input(
        _OtpPage(
            {
                "ok": False,
                "method": "none",
                "candidates": 1,
                "visible": 1,
                "enabled": 0,
                "readbackLength": 0,
            }
        ),
        "123456",
        logs.append,
    )

    assert result["ok"] is False
    assert result["candidates"] == 1
    assert result["visible"] == 1
    assert result["enabled"] == 0
    assert all("123456" not in line for line in logs)


def test_turnstile_guard_uses_solver_and_injects_token():
    page = _ChallengePage('<div class="cf-turnstile" data-sitekey="site-key"></div>')
    calls = []
    guard = BrowserChallengeGuard(
        solver=lambda url, sitekey: calls.append((url, sitekey)) or "solved-token",
        log=lambda _message: None,
        managed_wait_seconds=0,
    )

    assert guard.resolve(page) is True
    assert calls == [(page.url, "site-key")]
    assert page.injected == "solved-token"
    with pytest.raises(RuntimeError, match="remained after token injection"):
        guard.resolve(page)


def test_browser_http_429_is_reported_as_stable_error_code():
    page = _ChallengePage("<html>normal</html>")
    guard = BrowserChallengeGuard(solver=None, log=lambda _message: None)
    guard.observe(page)
    page._callbacks["response"](
        SimpleNamespace(status=429, url=page.url, headers={})
    )
    with pytest.raises(RuntimeError, match="HTTP_RATE_LIMIT"):
        guard.resolve(page)


def test_browser_uses_cf_mitigated_response_when_dom_has_not_rendered_yet():
    page = _ChallengePage("<html>normal</html>")
    guard = BrowserChallengeGuard(
        solver=None,
        log=lambda _message: None,
        managed_wait_seconds=0,
    )
    guard.observe(page)
    page._callbacks["response"](
        SimpleNamespace(
            status=403,
            url=page.url,
            headers={"cf-mitigated": "challenge"},
        )
    )

    with pytest.raises(RuntimeError, match="managed challenge did not clear"):
        guard.resolve(page)


def test_cloud_solvers_bind_turnstile_task_to_attempt_proxy_and_user_agent():
    yes_task = YesCaptcha._turnstile_task(
        "https://auth.openai.com/signup",
        "site-key",
        proxy_url="socks5://user:pass@127.0.0.1:1080",
        user_agent="UA/attempt",
    )
    two_payload = TwoCaptcha._turnstile_payload(
        "https://auth.openai.com/signup",
        "site-key",
        proxy_url="http://user:pass@proxy.test:8080",
        user_agent="UA/attempt",
    )

    assert yes_task == {
        "type": "TurnstileTask",
        "websiteURL": "https://auth.openai.com/signup",
        "websiteKey": "site-key",
        "proxyType": "socks5",
        "proxyAddress": "127.0.0.1",
        "proxyPort": 1080,
        "proxyLogin": "user",
        "proxyPassword": "pass",
        "userAgent": "UA/attempt",
    }
    assert two_payload["proxy"] == "user:pass@proxy.test:8080"
    assert two_payload["proxytype"] == "HTTP"
    assert two_payload["userAgent"] == "UA/attempt"


def test_browser_state_machine_emits_shared_registration_stages(monkeypatch):
    page = SimpleNamespace(url="https://auth.openai.com/about-you")
    states = []
    monkeypatch.setattr(
        browser_register,
        "_start_browser_signup_via_authorize",
        lambda *_args, **_kwargs: {"page_type": "create_account_password"},
    )
    monkeypatch.setattr(browser_register, "_get_cookies", lambda _page: {})
    monkeypatch.setattr(
        browser_register,
        "_extract_flow_state",
        lambda data, url: data or {"page_type": "oauth_callback", "current_url": url},
    )
    monkeypatch.setattr(
        browser_register,
        "_submit_password_via_page",
        lambda *_args, **_kwargs: {"ok": True, "status": 200, "data": {"page_type": "email_otp_verification"}},
    )
    monkeypatch.setattr(
        browser_register,
        "_submit_otp_via_page",
        lambda *_args, **_kwargs: {"ok": True, "status": 200, "data": {"page_type": "about_you"}},
    )
    monkeypatch.setattr(
        browser_register,
        "_submit_about_you_via_page",
        lambda *_args, **_kwargs: {"ok": True, "status": 200, "data": {"page_type": "oauth_callback"}},
    )
    monkeypatch.setattr(browser_register, "_handle_post_signup_onboarding", lambda *_args: None)
    monkeypatch.setattr(browser_register, "_seed_browser_device_id", lambda *_args: None)

    browser_register._browser_registration_flow(
        page,
        "user@example.com",
        "Password!123",
        lambda: "123456",
        lambda _message: None,
        stage_callback=lambda stage, _message, _action: states.append(stage),
    )

    assert states == [
        RegistrationStage.EMAIL_SUBMIT,
        RegistrationStage.OTP_TRIGGER,
        RegistrationStage.OTP_WAIT,
        RegistrationStage.OTP_SUBMIT,
        RegistrationStage.PROFILE_CREATE,
        RegistrationStage.CALLBACK,
    ]


def test_auth_response_observer_classifies_nested_identity_mismatch():
    observer = browser_register.AuthResponseObserver(flow_epoch="fixture-flow")

    class Response:
        url = "https://auth.openai.com/api/accounts/create_account"
        status = 400
        headers = {"content-type": "application/json"}

        @staticmethod
        def json():
            return {"error": {"code": "identity_provider_mismatch"}}

    observer._on_response(Response())
    failure = browser_register._observed_auth_failure(
        observer,
        since=0,
        current_url="https://auth.openai.com/about-you",
    )

    assert failure["text"] == "AUTH_IDENTITY_PROVIDER_MISMATCH: identity_provider_mismatch"
    assert failure["data"]["flow_epoch"] == "fixture-flow"


def test_auth_response_observer_classifies_plain_429_without_json_error_code():
    observer = browser_register.AuthResponseObserver(flow_epoch="rate-limit-flow")

    class Response:
        url = "https://auth.openai.com/api/accounts/create_account"
        status = 429
        headers = {"content-type": "text/html"}

    observer._on_response(Response())
    failure = browser_register._observed_auth_failure(
        observer,
        since=0,
        current_url="https://auth.openai.com/create-account/password",
    )

    assert failure["status"] == 429
    assert failure["text"] == "HTTP_RATE_LIMIT: auth request failed"
    assert failure["data"]["error_code"] == ""


def test_auth_response_observer_captures_redirect_location():
    observer = browser_register.AuthResponseObserver(flow_epoch="redirect-flow")

    class Response:
        url = "https://auth.openai.com/api/accounts/email-otp/validate"
        status = 302
        headers = {"location": "/about-you"}

    observer._on_response(Response())
    observed = observer.latest(since=0)

    assert observed["http_status"] == 302
    assert observed["continue_path"] == "/about-you"


def test_otp_auto_submit_accepts_auth_redirect_without_continue_button(monkeypatch):
    page = SimpleNamespace(
        url="https://auth.openai.com/email-verification",
        wait_for_load_state=lambda *_args, **_kwargs: None,
    )

    class Observer:
        @staticmethod
        def latest_error(*, since=0):
            return None

        @staticmethod
        def latest(*, since=0):
            return {
                "http_status": 302,
                "page_type": "",
                "continue_path": "/about-you",
                "at": since,
            }

    monkeypatch.setattr(
        browser_register,
        "_fill_otp_input",
        lambda *_args, **_kwargs: {"ok": True},
    )
    monkeypatch.setattr(browser_register, "_browser_pause", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(browser_register, "_click_first", lambda *_args, **_kwargs: None)

    result = browser_register._submit_otp_via_page(
        page,
        "123456",
        lambda _message: None,
        Observer(),
    )

    assert result == {
        "ok": True,
        "status": 302,
        "url": "https://auth.openai.com/email-verification",
        "data": {"page_type": "", "continue_url": "/about-you"},
        "text": "",
    }


def test_auth_response_observer_classifies_invalid_otp_business_error():
    observer = browser_register.AuthResponseObserver(flow_epoch="invalid-otp-flow")

    class Response:
        url = "https://auth.openai.com/api/accounts/email-otp/validate"
        status = 400
        headers = {"content-type": "application/json"}

        @staticmethod
        def json():
            return {"error": {"code": "invalid_code"}}

    observer._on_response(Response())
    failure = browser_register._observed_auth_failure(
        observer,
        since=0,
        current_url="https://auth.openai.com/email-verification",
    )

    assert failure["status"] == 400
    assert failure["text"] == "OTP_INVALID: invalid_code"


def test_authentication_error_page_is_not_treated_as_registration_success():
    class Body:
        def inner_text(self, timeout):
            assert timeout == 500
            return "Authentication Error identity_provider_mismatch"

    page = SimpleNamespace(
        url="https://auth.openai.com/error",
        locator=lambda _selector: Body(),
    )

    assert browser_register._classify_authentication_error_page(page).startswith(
        "AUTH_IDENTITY_PROVIDER_MISMATCH"
    )
    assert browser_register._is_registration_complete(
        {"page_type": "", "current_url": "https://chatgpt.com/sign-in-with-chatgpt/error"}
    ) is False

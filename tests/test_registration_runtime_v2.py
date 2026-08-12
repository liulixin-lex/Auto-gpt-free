from __future__ import annotations

import time

import pytest

from domain.registration_runtime import (
    AttemptContext,
    Deadline,
    RegistrationErrorCode,
    RegistrationMode,
    RegistrationStage,
    RetryPolicy,
    classify_registration_error,
    redact_registration_text,
    redact_registration_data,
    stable_resource_ref,
)


def test_registration_stage_contract_and_attempt_default_state():
    assert [stage.value for stage in RegistrationStage] == [
        "prepare",
        "preflight",
        "auth_begin",
        "email_submit",
        "otp_trigger",
        "otp_wait",
        "otp_submit",
        "profile_create",
        "callback",
        "session_validate",
        "persist",
        "done",
    ]

    context = AttemptContext(
        task_id="task-runtime",
        ordinal=1,
        requested_mode=RegistrationMode.PROTOCOL,
        effective_mode=RegistrationMode.PROTOCOL,
        deadline_monotonic=time.monotonic() + 10,
    )

    assert context.stage is RegistrationStage.PREPARE
    assert context.remaining_seconds() > 0
    assert context.expired() is False
    assert context.requested_mode is context.effective_mode


def test_deadline_observes_external_and_local_cancellation():
    external = False
    deadline = Deadline(10, external_cancel=lambda: external)
    assert deadline.should_stop() is False

    external = True
    assert deadline.is_cancelled() is True
    assert deadline.should_stop() is True

    local = Deadline(10)
    local.cancel()
    assert local.is_cancelled() is True


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("DNS name resolution failed", RegistrationErrorCode.NET_DNS),
        ("proxy CONNECT failed", RegistrationErrorCode.NET_PROXY),
        ("TLS certificate verify failed", RegistrationErrorCode.NET_TLS),
        ("HTTP 429 Too Many Requests", RegistrationErrorCode.HTTP_RATE_LIMIT),
        ("Cloudflare Turnstile challenge", RegistrationErrorCode.CF_CHALLENGE),
        ("invalid_auth_step", RegistrationErrorCode.AUTH_INVALID_STEP),
        ("OTP timeout while polling", RegistrationErrorCode.OTP_TIMEOUT),
        ("未获取到验证码", RegistrationErrorCode.OTP_TIMEOUT),
        ("验证码校验失败: Invalid code", RegistrationErrorCode.OTP_INVALID),
        ("invalid OTP", RegistrationErrorCode.OTP_INVALID),
        ("OTP_INVALID: invalid_code", RegistrationErrorCode.OTP_INVALID),
        ("Sentinel SDK drift detected", RegistrationErrorCode.SENTINEL_SDK_DRIFT),
        ("page.goto navigation failed", RegistrationErrorCode.BROWSER_NAVIGATION),
        ("database is locked", RegistrationErrorCode.DB_BUSY),
        ("missing access_token", RegistrationErrorCode.TOKEN_MISSING),
        ("registration deadline exceeded", RegistrationErrorCode.DEADLINE_EXCEEDED),
        ("registration queue deadline exceeded", RegistrationErrorCode.DEADLINE_EXCEEDED),
        ("registration capacity wait timed out", RegistrationErrorCode.RESOURCE_EXHAUSTED),
        ("cancel requested", RegistrationErrorCode.CANCELLED),
        ("unclassified failure", RegistrationErrorCode.INTERNAL_ERROR),
    ],
)
def test_error_classifier_returns_stable_codes(message, expected):
    assert classify_registration_error(message) is expected


def test_about_you_validation_failure_is_not_internal_error():
    assert classify_registration_error(
        "about_you 提交失败: Hmm, that doesn't look right. Try again?"
    ) is RegistrationErrorCode.AUTH_INVALID_STEP


def test_registration_log_redaction_covers_credentials_and_codes():
    raw = (
        "access_token=access-secret refresh-token:refresh-secret "
        "cookie=session-secret pkce_verifier=pkce-secret "
        "authorization_code=auth-secret otp=654321 "
        "Bearer bearer-secret https://alice:proxy-secret@proxy.example:8443"
    )

    redacted = redact_registration_text(raw)

    for secret in (
        "access-secret",
        "refresh-secret",
        "session-secret",
        "pkce-secret",
        "auth-secret",
        "654321",
        "bearer-secret",
        "proxy-secret",
    ):
        assert secret not in redacted
    assert redacted.count("***") >= 8


def test_structured_event_redaction_cleans_nested_secret_fields_and_json_text():
    payload = {
        "attempt_id": "attempt-1",
        "password": "plain-password",
        "nested": {
            "otp": "123456",
            "message": 'response={"access_token": "token-value"}',
            "proxy": "socks5://user:proxy-password@proxy.example:1080",
        },
    }

    cleaned = redact_registration_data(payload)

    assert cleaned["attempt_id"] == "attempt-1"
    assert cleaned["password"] == "***"
    assert cleaned["nested"]["otp"] == "***"
    combined = str(cleaned)
    assert "plain-password" not in combined
    assert "123456" not in combined
    assert "token-value" not in combined
    assert "proxy-password" not in combined


def test_resource_references_are_stable_and_do_not_expose_input():
    value = "http://user:password@proxy.example:8080"

    first = stable_resource_ref(value)
    second = stable_resource_ref(value)

    assert first == second
    assert first.startswith("sha256:")
    assert value not in first
    assert stable_resource_ref(None) == "direct"


def test_retry_policy_uses_bounded_full_jitter():
    policy = RetryPolicy(max_attempts=3, base_delay_seconds=2, max_delay_seconds=5)

    assert policy.delay(0, random_value=0.5) == 1
    assert policy.delay(1, random_value=1) == 4
    assert policy.delay(5, random_value=1) == 5
    assert policy.delay(1, random_value=-1) == 0

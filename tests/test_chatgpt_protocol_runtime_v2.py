from __future__ import annotations

import pytest

from application.registration_queries import RegistrationCapabilitiesService
from platforms.chatgpt.protocol.otp import OtpCoordinator, OtpInvalidError, OtpTimeoutError
from platforms.chatgpt.protocol.sdk import SentinelSdkDriftError, SentinelSdkResolver
from platforms.chatgpt.protocol_register import _SentinelBrowserRuntime


class _Response:
    def __init__(self, *, status_code=200, text=""):
        self.status_code = status_code
        self.text = text


def test_otp_coordinator_resends_once_after_empty_result():
    received = iter(("", "123456"))
    resends = []
    result = OtpCoordinator(
        receive=lambda: next(received),
        validate=lambda code: {"ok": code == "123456"},
        resend=lambda: resends.append(True),
    ).run()

    assert result.code == "123456"
    assert result.resend_count == 1
    assert resends == [True]


def test_otp_coordinator_never_replays_resend_more_than_once():
    received = iter(("111111", "222222"))
    resends = []

    with pytest.raises(OtpInvalidError):
        OtpCoordinator(
            receive=lambda: next(received),
            validate=lambda _code: (_ for _ in ()).throw(RuntimeError("invalid otp")),
            resend=lambda: resends.append(True),
        ).run()

    assert resends == [True]


def test_otp_coordinator_resends_once_after_real_mailbox_timeout():
    calls = 0
    resends = []

    def receive():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise TimeoutError("等待验证码超时 (90s)")
        return "123456"

    result = OtpCoordinator(
        receive=receive,
        validate=lambda code: {"ok": code == "123456"},
        resend=lambda: resends.append(True),
    ).run()

    assert result.code == "123456"
    assert result.resend_count == 1
    assert resends == [True]


def test_otp_coordinator_advances_message_cursor_before_resend():
    order = []
    values = iter((TimeoutError("mailbox timeout"), "654321"))

    def receive():
        value = next(values)
        if isinstance(value, Exception):
            raise value
        return value

    result = OtpCoordinator(
        receive=receive,
        validate=lambda code: {"code_length": len(code)},
        advance_cursor=lambda: order.append("cursor"),
        resend=lambda: order.append("resend"),
    ).run()

    assert result.code == "654321"
    assert order == ["cursor", "resend"]


def test_otp_coordinator_stops_after_second_mailbox_timeout():
    resends = []

    with pytest.raises(OtpTimeoutError, match="等待验证码超时"):
        OtpCoordinator(
            receive=lambda: (_ for _ in ()).throw(TimeoutError("等待验证码超时")),
            validate=lambda _code: {},
            resend=lambda: resends.append(True),
        ).run()

    assert resends == [True]


def test_otp_coordinator_does_not_treat_invalid_state_as_invalid_code():
    resends = []

    with pytest.raises(RuntimeError, match="invalid_state"):
        OtpCoordinator(
            receive=lambda: "123456",
            validate=lambda _code: (_ for _ in ()).throw(
                RuntimeError("invalid_state: sign-in session expired")
            ),
            resend=lambda: resends.append(True),
        ).run()

    assert resends == []


def test_sentinel_sdk_drift_opens_a_short_circuit(monkeypatch):
    calls = []

    class _Session:
        def get(self, url, **_kwargs):
            calls.append(url)
            return _Response(text="incompatible sdk")

    monkeypatch.setattr(SentinelSdkResolver, "_cache", {})
    monkeypatch.setattr(SentinelSdkResolver, "_drift_until", 0.0)
    monkeypatch.setattr(SentinelSdkResolver, "_drift_hash", "")
    resolver = SentinelSdkResolver(
        _Session(),
        fallback_url="https://sentinel.test/sdk.js",
        bootstrap_url="https://auth.test/about-you",
    )

    with pytest.raises(SentinelSdkDriftError, match="接口漂移"):
        resolver.load(compatibility_hook="expected-hook")
    first_call_count = len(calls)
    with pytest.raises(SentinelSdkDriftError, match="circuit is open"):
        resolver.load(compatibility_hook="expected-hook")

    assert len(calls) == first_call_count
    assert SentinelSdkResolver.drift_status()["open"] is True


def test_capabilities_report_protocol_degraded_while_sdk_circuit_is_open(monkeypatch):
    monkeypatch.setattr(
        SentinelSdkResolver,
        "drift_status",
        classmethod(
            lambda cls: {
                "open": True,
                "remaining_seconds": 120,
                "sdk_hash": "hash",
            }
        ),
    )
    result = RegistrationCapabilitiesService().inspect()
    protocol = next(item for item in result["items"] if item["mode"] == "protocol")

    assert protocol["status"] == "degraded"
    assert protocol["sentinel_sdk_drift"]["remaining_seconds"] == 120


def test_sentinel_runtime_closes_playwright_in_lifecycle_order():
    calls = []

    class _Closable:
        def __init__(self, name):
            self.name = name

        def close(self):
            calls.append(f"{self.name}.close")

    class _Playwright:
        def stop(self):
            calls.append("playwright.stop")

    runtime = object.__new__(_SentinelBrowserRuntime)
    runtime._context = _Closable("context")
    runtime._browser = _Closable("browser")
    runtime._playwright = _Playwright()

    runtime.close()

    assert calls == ["context.close", "browser.close", "playwright.stop"]
    assert runtime._context is None
    assert runtime._browser is None
    assert runtime._playwright is None


def test_sentinel_runtime_cleanup_failure_is_observable_and_continues():
    calls = []

    class _BrokenContext:
        def close(self):
            calls.append("context.close")
            raise ValueError("close failed")

    class _Browser:
        def close(self):
            calls.append("browser.close")

    class _Playwright:
        def stop(self):
            calls.append("playwright.stop")

    runtime = object.__new__(_SentinelBrowserRuntime)
    runtime._context = _BrokenContext()
    runtime._browser = _Browser()
    runtime._playwright = _Playwright()

    with pytest.raises(RuntimeError, match="context:ValueError"):
        runtime.close()

    assert calls == ["context.close", "browser.close", "playwright.stop"]


class _RuntimePage:
    def __init__(self, *, missing=()):
        self.missing = set(missing)
        self.closed = False

    def goto(self, *_args, **_kwargs):
        return None

    def close(self):
        self.closed = True

    def evaluate(self, script, value=None):
        if "window.__RegistrationSentinelSDK =" in script:
            return None
        if "const result = { sdk:" in script:
            return {
                "sdk": "object",
                **{
                    name: "undefined" if name in self.missing else "function"
                    for name in value
                },
            }
        if "async ({ chatReq, cachedProof })" in script:
            return {"t": "turnstile-proof", "so": "", "soFallback": ""}
        raise AssertionError("unexpected evaluate call")


class _RuntimeContext:
    def __init__(self, *, missing=()):
        self.missing = missing
        self.pages = []

    def new_page(self):
        page = _RuntimePage(missing=self.missing)
        self.pages.append(page)
        return page


def _runtime_with_context(context):
    runtime = object.__new__(_SentinelBrowserRuntime)
    runtime._context = context
    runtime._sdk_code = "sdk-code"
    runtime._sdk_hash = "a" * 64
    return runtime


def test_sentinel_vm_uses_a_fresh_captured_sdk_page_per_proof():
    context = _RuntimeContext()
    runtime = _runtime_with_context(context)

    first = runtime.vm_tokens({"turnstile": {"required": True, "dx": "dx"}}, "proof")
    second = runtime.vm_tokens({"turnstile": {"required": True, "dx": "dx"}}, "proof")

    assert first["t"] == "turnstile-proof"
    assert second["t"] == "turnstile-proof"
    assert len(context.pages) == 2
    assert all(page.closed for page in context.pages)


def test_sentinel_missing_runtime_contract_opens_drift_circuit(monkeypatch):
    monkeypatch.setattr(SentinelSdkResolver, "_drift_until", 0.0)
    monkeypatch.setattr(SentinelSdkResolver, "_drift_hash", "")
    context = _RuntimeContext(missing=("__D",))
    runtime = _runtime_with_context(context)

    with pytest.raises(SentinelSdkDriftError, match="missing=__D"):
        runtime.vm_tokens({"turnstile": {}}, "proof")

    assert SentinelSdkResolver.drift_status()["open"] is True
    assert context.pages[0].closed is True

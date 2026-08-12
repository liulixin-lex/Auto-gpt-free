"""Shared contracts for ChatGPT registration attempts.

The protocol and browser executors intentionally use the same stages, error
codes and event envelope.  Keeping these values in the domain layer prevents
transport-specific strings from leaking into persistence and the UI.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import StrEnum
import hashlib
import re
import threading
import time
from typing import Any, Callable
from uuid import uuid4


class RegistrationMode(StrEnum):
    PROTOCOL = "protocol"
    HEADLESS = "headless"
    HEADED = "headed"


class RegistrationStage(StrEnum):
    PREPARE = "prepare"
    PREFLIGHT = "preflight"
    AUTH_BEGIN = "auth_begin"
    EMAIL_SUBMIT = "email_submit"
    OTP_TRIGGER = "otp_trigger"
    OTP_WAIT = "otp_wait"
    OTP_SUBMIT = "otp_submit"
    PROFILE_CREATE = "profile_create"
    CALLBACK = "callback"
    SESSION_VALIDATE = "session_validate"
    PERSIST = "persist"
    DONE = "done"


class RegistrationErrorCode(StrEnum):
    NET_DNS = "NET_DNS"
    NET_PROXY = "NET_PROXY"
    NET_TLS = "NET_TLS"
    HTTP_RATE_LIMIT = "HTTP_RATE_LIMIT"
    CF_CHALLENGE = "CF_CHALLENGE"
    CF_CLEARANCE_UNAVAILABLE = "CF_CLEARANCE_UNAVAILABLE"
    EGRESS_COOLDOWN = "EGRESS_COOLDOWN"
    AUTH_CSRF = "AUTH_CSRF"
    AUTH_REDIRECT = "AUTH_REDIRECT"
    AUTH_INVALID_STEP = "AUTH_INVALID_STEP"
    AUTH_IDENTITY_PROVIDER_MISMATCH = "AUTH_IDENTITY_PROVIDER_MISMATCH"
    AUTH_SESSION_DESYNC = "AUTH_SESSION_DESYNC"
    MAILBOX_REUSED = "MAILBOX_REUSED"
    OTP_PROVIDER = "OTP_PROVIDER"
    OTP_TIMEOUT = "OTP_TIMEOUT"
    OTP_STALE = "OTP_STALE"
    OTP_INVALID = "OTP_INVALID"
    SENTINEL_SDK_DRIFT = "SENTINEL_SDK_DRIFT"
    SENTINEL_PROOF = "SENTINEL_PROOF"
    BROWSER_START = "BROWSER_START"
    BROWSER_NAVIGATION = "BROWSER_NAVIGATION"
    BROWSER_STATE_UNKNOWN = "BROWSER_STATE_UNKNOWN"
    TOKEN_MISSING = "TOKEN_MISSING"
    SESSION_INVALID = "SESSION_INVALID"
    DB_BUSY = "DB_BUSY"
    RESOURCE_EXHAUSTED = "RESOURCE_EXHAUSTED"
    DEADLINE_EXCEEDED = "DEADLINE_EXCEEDED"
    CANCELLED = "CANCELLED"
    INTERNAL_ERROR = "INTERNAL_ERROR"


class RegistrationEventKind(StrEnum):
    STATE = "state"
    STAGE = "stage"
    RETRY = "retry"
    RESULT = "result"
    DIAGNOSTIC = "diagnostic"
    SUMMARY = "summary"


class RegistrationAttemptStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"
    INTERRUPTED = "interrupted"


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    max_attempts: int
    base_delay_seconds: float = 0.5
    max_delay_seconds: float = 8.0
    retryable: bool = True
    respect_retry_after: bool = True

    def delay(self, retry_index: int, *, random_value: float = 0.5) -> float:
        """Return exponential full-jitter delay for a zero-based retry index."""
        ceiling = min(
            self.max_delay_seconds,
            self.base_delay_seconds * (2 ** max(int(retry_index), 0)),
        )
        return max(0.0, ceiling * min(max(float(random_value), 0.0), 1.0))


IDEMPOTENT_RETRY = RetryPolicy(max_attempts=3)
SIDE_EFFECT_RETRY = RetryPolicy(max_attempts=1, retryable=False)
OTP_POLL_RETRY = RetryPolicy(max_attempts=60, base_delay_seconds=2.0, max_delay_seconds=5.0)


@dataclass(slots=True)
class AttemptContext:
    task_id: str
    ordinal: int
    requested_mode: RegistrationMode
    effective_mode: RegistrationMode
    deadline_monotonic: float
    attempt_id: str = field(default_factory=lambda: uuid4().hex)
    stage: RegistrationStage = RegistrationStage.PREPARE
    mail_provider: str = ""
    proxy_ref: str = "direct"
    fingerprint_id: str = ""
    retry_count: int = 0
    replacement_count: int = 0

    def remaining_seconds(self) -> float:
        return max(0.0, self.deadline_monotonic - time.monotonic())

    def expired(self) -> bool:
        return self.remaining_seconds() <= 0


class Deadline:
    """Cooperative deadline/cancellation primitive shared by sync executors."""

    def __init__(
        self,
        timeout_seconds: float,
        *,
        external_cancel: Callable[[], bool] | None = None,
    ) -> None:
        self.started_monotonic = time.monotonic()
        self.ends_monotonic = self.started_monotonic + max(float(timeout_seconds), 0.0)
        self._external_cancel = external_cancel
        self._cancelled = threading.Event()

    def cancel(self) -> None:
        self._cancelled.set()

    def is_cancelled(self) -> bool:
        return self._cancelled.is_set() or bool(
            self._external_cancel and self._external_cancel()
        )

    def is_expired(self) -> bool:
        return time.monotonic() >= self.ends_monotonic

    def should_stop(self) -> bool:
        return self.is_cancelled() or self.is_expired()

    def remaining_seconds(self) -> float:
        return max(0.0, self.ends_monotonic - time.monotonic())


@dataclass(slots=True)
class StructuredRegistrationEvent:
    task_id: str
    attempt_id: str
    kind: RegistrationEventKind
    stage: RegistrationStage
    action: str
    message: str
    level: str = "info"
    event_code: str = ""
    error_code: str = ""
    retryable: bool = False
    retry_index: int = 0
    duration_ms: int = 0
    attributes: dict[str, Any] = field(default_factory=dict)
    schema_version: int = 2

    def detail(self) -> dict[str, Any]:
        data = asdict(self)
        data.pop("task_id", None)
        data.pop("message", None)
        data.pop("level", None)
        data["kind"] = self.kind.value
        data["stage"] = self.stage.value
        return data


_SECRET_KEY = (
    r"access[_-]?token|refresh[_-]?token|id[_-]?token|session[_-]?token|"
    r"cookie|pkce[_-]?verifier|authorization[_-]?code|verification[_-]?code|"
    r"one[_-]?time[_-]?code|proxy[_-]?password|password|otp|token|authorization"
)

_SECRET_PATTERNS = (
    re.compile(
        rf"(?i)([\"']?(?:{_SECRET_KEY})[\"']?\s*[:=]\s*)([\"'])(.*?)(\2)"
    ),
    re.compile(
        rf"(?i)((?:{_SECRET_KEY}))"
        r"\s*[:=]\s*([^\s,;]+)"
    ),
    re.compile(r"(?i)([a-z][a-z0-9+.-]*://[^\s:/]+:)([^@\s]+)(@)"),
)

_BEARER_PATTERN = re.compile(r"(?i)(bearer)\s+([^\s,;]+)")
_SENSITIVE_FIELD_PATTERN = re.compile(rf"(?i)^(?:{_SECRET_KEY})$")


def redact_registration_text(value: Any) -> str:
    text = str(value or "")
    for pattern in _SECRET_PATTERNS:
        if pattern.groups == 4:
            text = pattern.sub(r"\1\2***\4", text)
        elif pattern.groups >= 3:
            text = pattern.sub(r"\1***\3", text)
        else:
            text = pattern.sub(r"\1=***", text)
    text = _BEARER_PATTERN.sub(r"\1 ***", text)
    return text


def redact_registration_data(value: Any) -> Any:
    """Recursively redact structured event attributes before persistence."""
    if isinstance(value, dict):
        cleaned: dict[Any, Any] = {}
        for key, item in value.items():
            if _SENSITIVE_FIELD_PATTERN.fullmatch(str(key or "")):
                cleaned[key] = "***"
            else:
                cleaned[key] = redact_registration_data(item)
        return cleaned
    if isinstance(value, list):
        return [redact_registration_data(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_registration_data(item) for item in value)
    if isinstance(value, str):
        return redact_registration_text(value)
    return value


def stable_resource_ref(value: str | None) -> str:
    raw = str(value or "").strip()
    if not raw:
        return "direct"
    return "sha256:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def classify_registration_error(error: Any) -> RegistrationErrorCode:
    text = str(error or "").lower()
    checks: tuple[tuple[tuple[str, ...], RegistrationErrorCode], ...] = (
        (
            (
                "registration capacity wait timed out",
                "capacity wait timed out",
                "capacity timeout",
            ),
            RegistrationErrorCode.RESOURCE_EXHAUSTED,
        ),
        (("deadline", "slot timeout", "超时丢弃"), RegistrationErrorCode.DEADLINE_EXCEEDED),
        (("cancel", "取消"), RegistrationErrorCode.CANCELLED),
        (("sdk drift", "sdk 版本", "sentinel sdk"), RegistrationErrorCode.SENTINEL_SDK_DRIFT),
        (("sentinel", "proof"), RegistrationErrorCode.SENTINEL_PROOF),
        (("429", "rate limit", "too many requests"), RegistrationErrorCode.HTTP_RATE_LIMIT),
        (("cf_clearance_unavailable", "clearance unavailable", "clearance missing"), RegistrationErrorCode.CF_CLEARANCE_UNAVAILABLE),
        (("egress_cooldown", "egress cooling", "resource cooling down: egress"), RegistrationErrorCode.EGRESS_COOLDOWN),
        (("cloudflare", "turnstile", "cf-ray", "challenge"), RegistrationErrorCode.CF_CHALLENGE),
        (
            (
                "otp timeout",
                "otp_timeout",
                "验证码超时",
                "未收到验证码",
                "未获取到验证码",
                "verification code timeout",
            ),
            RegistrationErrorCode.OTP_TIMEOUT,
        ),
        (("stale otp", "旧验证码"), RegistrationErrorCode.OTP_STALE),
        (
            (
                "invalid otp",
                "invalid_otp",
                "invalid code",
                "invalid_code",
                "invalid verification code",
                "invalid one-time password",
                "验证码无效",
                "验证码错误",
            ),
            RegistrationErrorCode.OTP_INVALID,
        ),
        (("mailbox_reused", "mailbox reused"), RegistrationErrorCode.MAILBOX_REUSED),
        (("mailbox", "邮箱服务", "邮件提供商"), RegistrationErrorCode.OTP_PROVIDER),
        (("identity_provider_mismatch", "auth_identity_provider_mismatch"), RegistrationErrorCode.AUTH_IDENTITY_PROVIDER_MISMATCH),
        (("session_desync", "auth_session_desync"), RegistrationErrorCode.AUTH_SESSION_DESYNC),
        (
            (
                "invalid_auth_step",
                "about_you 提交失败",
                "doesn't look right",
                "valid age to continue",
            ),
            RegistrationErrorCode.AUTH_INVALID_STEP,
        ),
        (("csrf",), RegistrationErrorCode.AUTH_CSRF),
        (("redirect", "callback state"), RegistrationErrorCode.AUTH_REDIRECT),
        (("access_token", "missing token", "缺少 access_token"), RegistrationErrorCode.TOKEN_MISSING),
        (("session", "登录态"), RegistrationErrorCode.SESSION_INVALID),
        (("browser", "camoufox", "浏览器启动"), RegistrationErrorCode.BROWSER_START),
        (("navigation", "page.goto", "页面加载"), RegistrationErrorCode.BROWSER_NAVIGATION),
        (("unknown page", "未知页面", "selector"), RegistrationErrorCode.BROWSER_STATE_UNKNOWN),
        (("database is locked", "database busy", "sqlite busy"), RegistrationErrorCode.DB_BUSY),
        (("proxy", "代理"), RegistrationErrorCode.NET_PROXY),
        (("dns", "name resolution", "getaddrinfo"), RegistrationErrorCode.NET_DNS),
        (("tls", "ssl", "certificate"), RegistrationErrorCode.NET_TLS),
        (("connection", "network", "连接", "网络"), RegistrationErrorCode.NET_PROXY),
        (("resource", "容量", "no slot"), RegistrationErrorCode.RESOURCE_EXHAUSTED),
    )
    for needles, code in checks:
        if any(needle in text for needle in needles):
            return code
    return RegistrationErrorCode.INTERNAL_ERROR

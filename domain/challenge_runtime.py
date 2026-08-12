"""Challenge classification shared by protocol and browser registration.

The classifier deliberately consumes transport-neutral evidence. Executors can
therefore make retry and reporting decisions without parsing localized log
messages or treating every 403 as the same failure.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import json
from typing import Any, Mapping

from domain.registration_runtime import RegistrationErrorCode


class ChallengeKind(StrEnum):
    NONE = "none"
    CLOUDFLARE_MANAGED = "cloudflare_managed"
    TURNSTILE = "turnstile"
    SENTINEL_POW = "sentinel_pow"
    HTTP_FORBIDDEN = "http_forbidden"
    HTTP_RATE_LIMIT = "http_rate_limit"
    NETWORK_ERROR = "network_error"
    OPENAI_AUTH_ERROR = "openai_auth_error"


@dataclass(frozen=True, slots=True)
class ChallengeClassification:
    kind: ChallengeKind
    error_code: RegistrationErrorCode | None
    retryable: bool
    status_code: int = 0
    reason: str = ""

    @property
    def challenged(self) -> bool:
        return self.kind in {
            ChallengeKind.CLOUDFLARE_MANAGED,
            ChallengeKind.TURNSTILE,
            ChallengeKind.SENTINEL_POW,
        }


class ChallengeClassifier:
    """Classify HTTP, DOM, JSON and exception evidence in stable priority order."""

    _NETWORK_MARKERS = (
        "connection reset",
        "connection refused",
        "connect timeout",
        "read timeout",
        "name resolution",
        "getaddrinfo",
        "dns",
        "ssl",
        "tls",
        "certificate verify",
        "proxy error",
    )
    _TURNSTILE_MARKERS = (
        "cf-turnstile",
        "turnstile.render",
        "turnstile sitekey",
        "data-sitekey",
    )
    _CF_MANAGED_MARKERS = (
        "just a moment",
        "cf-browser-verification",
        "attention required",
        "performing security verification",
        "verify you are human",
    )
    _SENTINEL_MARKERS = (
        "sentinel",
        "proof-of-work",
        "proof_of_work",
        "proofofwork",
        "proof requirements",
        "proofofworktoken",
    )

    @classmethod
    def classify(
        cls,
        *,
        status_code: int | None = None,
        url: str = "",
        headers: Mapping[str, Any] | None = None,
        body: Any = "",
        payload: Mapping[str, Any] | None = None,
        error: BaseException | str | None = None,
    ) -> ChallengeClassification:
        try:
            code = int(status_code or 0)
        except (TypeError, ValueError):
            code = 0
        normalized_headers = {
            str(key).lower(): str(value).lower()
            for key, value in (headers or {}).items()
        }
        if isinstance(body, (dict, list)):
            body_text = json.dumps(
                body,
                ensure_ascii=True,
                separators=(",", ":"),
                default=str,
            )
        else:
            body_text = str(body or "")
        if payload:
            body_text = (
                f"{body_text} "
                f"{json.dumps(payload, ensure_ascii=True, separators=(',', ':'), default=str)}"
            )
        evidence = " ".join((str(url or ""), body_text, str(error or ""))).lower()

        cf_mitigated = normalized_headers.get("cf-mitigated") == "challenge"
        cf_ray = bool(normalized_headers.get("cf-ray"))
        if cf_mitigated or any(marker in evidence for marker in cls._CF_MANAGED_MARKERS):
            return ChallengeClassification(
                ChallengeKind.CLOUDFLARE_MANAGED,
                RegistrationErrorCode.CF_CHALLENGE,
                True,
                code,
                "cf_mitigated" if cf_mitigated else "managed_challenge_evidence",
            )

        # Generic Turnstile script imports also exist on ordinary auth pages.
        # Require widget/render evidence after ruling out the enclosing managed page.
        if any(marker in evidence for marker in cls._TURNSTILE_MARKERS):
            return ChallengeClassification(
                ChallengeKind.TURNSTILE,
                RegistrationErrorCode.CF_CHALLENGE,
                True,
                code,
                "turnstile_evidence",
            )

        if any(marker in evidence for marker in cls._SENTINEL_MARKERS):
            sdk_drift = "sdk drift" in evidence or "sdk version" in evidence
            return ChallengeClassification(
                ChallengeKind.SENTINEL_POW,
                (
                    RegistrationErrorCode.SENTINEL_SDK_DRIFT
                    if sdk_drift
                    else RegistrationErrorCode.SENTINEL_PROOF
                ),
                not sdk_drift,
                code,
                "sentinel_evidence",
            )

        if code == 429:
            return ChallengeClassification(
                ChallengeKind.HTTP_RATE_LIMIT,
                RegistrationErrorCode.HTTP_RATE_LIMIT,
                True,
                code,
                "http_429",
            )

        if code == 403:
            # A plain 403 remains distinct. A cf-ray alone is useful evidence but
            # is not sufficient to claim that a managed challenge was rendered.
            return ChallengeClassification(
                ChallengeKind.HTTP_FORBIDDEN,
                RegistrationErrorCode.CF_CHALLENGE if cf_ray else RegistrationErrorCode.AUTH_INVALID_STEP,
                False,
                code,
                "http_403_cf" if cf_ray else "http_403",
            )

        auth_markers = {
            "invalid_auth_step": RegistrationErrorCode.AUTH_INVALID_STEP,
            "csrf": RegistrationErrorCode.AUTH_CSRF,
            "callback state": RegistrationErrorCode.AUTH_REDIRECT,
            "redirect_uri": RegistrationErrorCode.AUTH_REDIRECT,
        }
        for marker, error_code in auth_markers.items():
            if marker in evidence:
                return ChallengeClassification(
                    ChallengeKind.OPENAI_AUTH_ERROR,
                    error_code,
                    False,
                    code,
                    marker,
                )

        if error and any(marker in evidence for marker in cls._NETWORK_MARKERS):
            if any(marker in evidence for marker in ("dns", "name resolution", "getaddrinfo")):
                error_code = RegistrationErrorCode.NET_DNS
            elif any(marker in evidence for marker in ("ssl", "tls", "certificate")):
                error_code = RegistrationErrorCode.NET_TLS
            else:
                error_code = RegistrationErrorCode.NET_PROXY
            return ChallengeClassification(
                ChallengeKind.NETWORK_ERROR,
                error_code,
                True,
                code,
                "transport_error",
            )

        return ChallengeClassification(ChallengeKind.NONE, None, False, code, "no_challenge")


__all__ = [
    "ChallengeClassification",
    "ChallengeClassifier",
    "ChallengeKind",
]

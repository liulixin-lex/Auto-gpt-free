from __future__ import annotations

import base64
from dataclasses import dataclass, field
import hashlib
import secrets
from urllib.parse import parse_qs, urlparse


class OAuthCallbackError(RuntimeError):
    """The authorization callback is missing, malformed, or rejected."""

    error_code = "AUTH_REDIRECT"


class OAuthStateMismatchError(OAuthCallbackError):
    """The callback does not belong to the active PKCE transaction."""


@dataclass(frozen=True, slots=True)
class OAuthCallback:
    code: str = field(repr=False)
    state: str = field(repr=False)
    scope: str = ""


def _base64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


@dataclass(frozen=True, slots=True)
class PkceTransaction:
    verifier: str = field(repr=False)
    challenge: str
    state: str = field(repr=False)
    nonce: str = field(repr=False)

    @classmethod
    def create(cls) -> "PkceTransaction":
        verifier = _base64url(secrets.token_bytes(64))[:96]
        challenge = _base64url(hashlib.sha256(verifier.encode("ascii")).digest())
        return cls(
            verifier=verifier,
            challenge=challenge,
            state=secrets.token_urlsafe(32),
            nonce=secrets.token_urlsafe(32),
        )

    def validate_state(self, returned_state: str) -> None:
        if not secrets.compare_digest(self.state, str(returned_state or "")):
            raise OAuthStateMismatchError("OAuth callback state mismatch")

    def parse_callback(self, callback_url: str, *, redirect_uri: str) -> OAuthCallback:
        """Validate the redirect target and bind it to this transaction.

        The authorization code, state, and PKCE verifier are deliberately kept
        out of exception messages and dataclass repr output.
        """
        callback = urlparse(str(callback_url or ""))
        expected = urlparse(str(redirect_uri or ""))
        callback_origin = (callback.scheme.lower(), callback.hostname or "", callback.port)
        expected_origin = (expected.scheme.lower(), expected.hostname or "", expected.port)
        if callback_origin != expected_origin or callback.path.rstrip("/") != expected.path.rstrip("/"):
            raise OAuthCallbackError("OAuth callback redirect URI mismatch")

        values = parse_qs(callback.query, keep_blank_values=True)
        error = str((values.get("error") or [""])[0]).strip()
        if error:
            raise OAuthCallbackError(f"OAuth authorization failed: {error[:80]}")

        returned_state = str((values.get("state") or [""])[0]).strip()
        self.validate_state(returned_state)
        code = str((values.get("code") or [""])[0]).strip()
        if not code:
            raise OAuthCallbackError("OAuth callback missing authorization code")
        return OAuthCallback(
            code=code,
            state=returned_state,
            scope=str((values.get("scope") or [""])[0]).strip(),
        )

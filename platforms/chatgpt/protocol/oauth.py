from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable
from urllib.parse import urlencode, urljoin, urlparse

from .pkce import OAuthCallback, OAuthCallbackError, PkceTransaction
from .transport import ProtocolTransport


class OAuthAuthorizationError(RuntimeError):
    """The authenticated session did not complete the authorization redirect."""

    error_code = "AUTH_REDIRECT"


class OAuthTokenExchangeError(RuntimeError):
    """The one-shot authorization-code exchange failed."""

    error_code = "TOKEN_MISSING"


@dataclass(frozen=True, slots=True)
class OAuthAuthorization:
    url: str = field(repr=False)
    transaction: PkceTransaction = field(repr=False)


@dataclass(frozen=True, slots=True)
class OAuthTokens:
    access_token: str = field(repr=False)
    refresh_token: str = field(repr=False)
    id_token: str = field(repr=False)
    token_type: str = "Bearer"
    expires_in: int = 0
    scope: str = ""

    def as_dict(self) -> dict:
        return {
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "id_token": self.id_token,
            "token_type": self.token_type,
            "expires_in": self.expires_in,
            "scope": self.scope,
        }


class OAuthPkceClient:
    """Execute Codex OAuth as a second phase on an authenticated session.

    Authorization redirects are followed manually so the localhost callback is
    captured without making a network request to the callback listener.  The
    token exchange is a one-shot side-effect request and is never retried.
    """

    def __init__(
        self,
        transport: ProtocolTransport,
        *,
        auth_url: str,
        token_url: str,
        client_id: str,
        redirect_uri: str,
        scope: str,
        originator: str = "codex_cli_rs",
        max_redirects: int = 15,
        header_factory: Callable[[str], dict] | None = None,
        transaction_factory: Callable[[], PkceTransaction] = PkceTransaction.create,
    ) -> None:
        self.transport = transport
        self.auth_url = str(auth_url).rstrip("/")
        self.token_url = str(token_url)
        self.client_id = str(client_id)
        self.redirect_uri = str(redirect_uri)
        self.scope = str(scope)
        self.originator = str(originator)
        self.max_redirects = max(int(max_redirects), 1)
        self.header_factory = header_factory or (lambda _referer: {})
        self.transaction_factory = transaction_factory

    def begin(self) -> OAuthAuthorization:
        transaction = self.transaction_factory()
        query = urlencode(
            {
                "response_type": "code",
                "client_id": self.client_id,
                "redirect_uri": self.redirect_uri,
                "scope": self.scope,
                "code_challenge": transaction.challenge,
                "code_challenge_method": "S256",
                "id_token_add_organizations": "true",
                "codex_cli_simplified_flow": "true",
                "state": transaction.state,
                "originator": self.originator,
            }
        )
        return OAuthAuthorization(
            url=f"{self.auth_url}?{query}",
            transaction=transaction,
        )

    def _is_callback(self, value: str) -> bool:
        target = urlparse(str(value or ""))
        expected = urlparse(self.redirect_uri)
        return (
            target.scheme.lower() == expected.scheme.lower()
            and (target.hostname or "").lower() == (expected.hostname or "").lower()
            and target.port == expected.port
            and target.path.rstrip("/") == expected.path.rstrip("/")
        )

    def authorize(self, authorization: OAuthAuthorization) -> OAuthCallback:
        current = authorization.url
        referer = ""
        for _ in range(self.max_redirects):
            response = self.transport.get(
                current,
                headers=self.header_factory(referer),
                allow_redirects=False,
            )
            status = int(getattr(response, "status_code", 0) or 0)
            if status >= 400:
                raise OAuthAuthorizationError(f"OAuth authorization failed: HTTP {status}")

            response_url = str(getattr(response, "url", "") or "")
            if self._is_callback(response_url):
                return authorization.transaction.parse_callback(
                    response_url,
                    redirect_uri=self.redirect_uri,
                )

            location = str(getattr(response, "headers", {}).get("location") or "").strip()
            if not location:
                raise OAuthAuthorizationError("OAuth authorization did not return a callback redirect")
            next_url = urljoin(current, location)
            if self._is_callback(next_url):
                return authorization.transaction.parse_callback(
                    next_url,
                    redirect_uri=self.redirect_uri,
                )
            referer, current = current, next_url
        raise OAuthAuthorizationError("OAuth authorization redirect limit exceeded")

    @staticmethod
    def _response_payload(response) -> dict:
        try:
            value = response.json()
            return value if isinstance(value, dict) else {}
        except Exception:
            return {}

    def exchange(
        self,
        authorization: OAuthAuthorization,
        callback: OAuthCallback,
    ) -> OAuthTokens:
        # parse_callback already validated state, but keep the invariant local
        # to the exchange boundary as well.
        authorization.transaction.validate_state(callback.state)
        response = self.transport.post(
            self.token_url,
            side_effect=True,
            data={
                "grant_type": "authorization_code",
                "code": callback.code,
                "redirect_uri": self.redirect_uri,
                "client_id": self.client_id,
                "code_verifier": authorization.transaction.verifier,
            },
            headers={"content-type": "application/x-www-form-urlencoded"},
            allow_redirects=False,
        )
        status = int(getattr(response, "status_code", 0) or 0)
        payload = self._response_payload(response)
        if status < 200 or status >= 300:
            error = payload.get("error")
            if isinstance(error, dict):
                error = error.get("code") or error.get("type")
            error_code = str(error or "unknown")[:80]
            raise OAuthTokenExchangeError(
                f"OAuth token exchange failed: HTTP {status} ({error_code})"
            )

        access_token = str(payload.get("access_token") or "").strip()
        refresh_token = str(payload.get("refresh_token") or "").strip()
        id_token = str(payload.get("id_token") or "").strip()
        if not access_token or not refresh_token or not id_token:
            raise OAuthTokenExchangeError("OAuth token exchange returned incomplete credentials")
        try:
            expires_in = max(int(payload.get("expires_in") or 0), 0)
        except (TypeError, ValueError):
            expires_in = 0
        return OAuthTokens(
            access_token=access_token,
            refresh_token=refresh_token,
            id_token=id_token,
            token_type=str(payload.get("token_type") or "Bearer"),
            expires_in=expires_in,
            scope=str(payload.get("scope") or callback.scope or ""),
        )

    def run(self) -> OAuthTokens:
        authorization = self.begin()
        callback = self.authorize(authorization)
        return self.exchange(authorization, callback)

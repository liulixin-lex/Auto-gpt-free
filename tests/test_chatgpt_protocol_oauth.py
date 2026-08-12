from __future__ import annotations

import base64
import hashlib
from urllib.parse import parse_qs, urlencode, urlparse

import pytest

from platforms.chatgpt.protocol import (
    OAuthPkceClient,
    OAuthStateMismatchError,
    OAuthTokenExchangeError,
    PkceTransaction,
)
from platforms.chatgpt.protocol.transport import ProtocolTransport


AUTH_URL = "https://auth.openai.com/oauth/authorize"
TOKEN_URL = "https://auth.openai.com/oauth/token"
REDIRECT_URI = "http://localhost:1455/auth/callback"


class _Response:
    def __init__(self, status_code=200, payload=None, *, headers=None, url=""):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.headers = headers or {}
        self.url = url

    def json(self):
        return self._payload


class _ZeroRandom:
    @staticmethod
    def random() -> float:
        return 0.0


class _OAuthSession:
    def __init__(self, *, token_status: int = 200, wrong_state: bool = False):
        self.token_status = token_status
        self.wrong_state = wrong_state
        self.get_calls: list[tuple[str, dict]] = []
        self.post_calls: list[tuple[str, dict]] = []
        self.state = ""

    def get(self, url, **kwargs):
        self.get_calls.append((url, kwargs))
        if url.startswith(AUTH_URL):
            self.state = parse_qs(urlparse(url).query)["state"][0]
            return _Response(status_code=302, headers={"location": "/oauth/continue"})
        if url == "https://auth.openai.com/oauth/continue":
            state = "different-state" if self.wrong_state else self.state
            callback = REDIRECT_URI + "?" + urlencode(
                {
                    "code": "authorization-code",
                    "state": state,
                    "scope": "openid profile email offline_access",
                }
            )
            return _Response(status_code=302, headers={"location": callback})
        raise AssertionError(f"unexpected GET {url}")

    def post(self, url, **kwargs):
        self.post_calls.append((url, kwargs))
        assert url == TOKEN_URL
        if self.token_status != 200:
            return _Response(
                status_code=self.token_status,
                payload={"error": {"code": "temporary_failure"}},
            )
        return _Response(
            payload={
                "access_token": "oauth-access",
                "refresh_token": "oauth-refresh",
                "id_token": "oauth-id",
                "token_type": "Bearer",
                "expires_in": 3600,
            }
        )


def _client(session: _OAuthSession) -> OAuthPkceClient:
    return OAuthPkceClient(
        ProtocolTransport(session, random_source=_ZeroRandom()),
        auth_url=AUTH_URL,
        token_url=TOKEN_URL,
        client_id="codex-client",
        redirect_uri=REDIRECT_URI,
        scope="openid profile email offline_access",
    )


def test_pkce_transactions_are_unique_and_use_s256():
    first = PkceTransaction.create()
    second = PkceTransaction.create()

    assert first.verifier != second.verifier
    assert first.state != second.state
    assert 43 <= len(first.verifier) <= 128
    expected = base64.urlsafe_b64encode(
        hashlib.sha256(first.verifier.encode("ascii")).digest()
    ).decode("ascii").rstrip("=")
    assert first.challenge == expected
    assert first.verifier not in repr(first)
    assert first.state not in repr(first)


def test_authorization_url_contains_fresh_pkce_transaction_fields():
    client = _client(_OAuthSession())
    first = client.begin()
    second = client.begin()
    params = parse_qs(urlparse(first.url).query)

    assert first.transaction.verifier != second.transaction.verifier
    assert first.transaction.state != second.transaction.state
    assert params["response_type"] == ["code"]
    assert params["code_challenge_method"] == ["S256"]
    assert params["code_challenge"] == [first.transaction.challenge]
    assert params["state"] == [first.transaction.state]
    assert params["codex_cli_simplified_flow"] == ["true"]
    assert params["originator"] == ["codex_cli_rs"]
    assert first.transaction.state not in repr(first)


def test_oauth_flow_captures_callback_without_requesting_localhost_and_exchanges_once():
    session = _OAuthSession()
    tokens = _client(session).run()

    assert tokens.access_token == "oauth-access"
    assert tokens.refresh_token == "oauth-refresh"
    assert tokens.id_token == "oauth-id"
    assert len(session.post_calls) == 1
    assert all(not url.startswith(REDIRECT_URI) for url, _kwargs in session.get_calls)
    token_body = session.post_calls[0][1]["data"]
    assert token_body["grant_type"] == "authorization_code"
    assert token_body["code"] == "authorization-code"
    assert token_body["redirect_uri"] == REDIRECT_URI
    assert token_body["client_id"] == "codex-client"
    assert token_body["code_verifier"]


def test_callback_state_mismatch_stops_before_token_exchange():
    session = _OAuthSession(wrong_state=True)

    with pytest.raises(OAuthStateMismatchError, match="state mismatch"):
        _client(session).run()

    assert session.post_calls == []


def test_token_exchange_is_not_retried_on_retryable_http_status():
    session = _OAuthSession(token_status=503)

    with pytest.raises(OAuthTokenExchangeError, match="HTTP 503"):
        _client(session).run()

    assert len(session.post_calls) == 1


def test_callback_parser_rejects_a_different_redirect_origin():
    transaction = PkceTransaction.create()
    callback = "http://example.com:1455/auth/callback?" + urlencode(
        {"code": "code", "state": transaction.state}
    )

    with pytest.raises(RuntimeError, match="redirect URI mismatch"):
        transaction.parse_callback(callback, redirect_uri=REDIRECT_URI)

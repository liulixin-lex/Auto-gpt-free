from __future__ import annotations

import os

from core import access_auth as auth


def test_setup_and_login_session_flow(monkeypatch):
    monkeypatch.delenv("APP_PASSWORD", raising=False)
    monkeypatch.delenv("MANAGEMENT_PASSWORD", raising=False)

    # Isolate config keys used by this test.
    store: dict[str, str] = {}

    def fake_get(key: str, default: str = "") -> str:
        return store.get(key, default)

    def fake_set(key: str, value: str) -> None:
        store[key] = value

    def fake_set_many(data: dict) -> None:
        store.update({k: str(v) for k, v in data.items()})

    monkeypatch.setattr(auth.config_store, "get", fake_get)
    monkeypatch.setattr(auth.config_store, "set", fake_set)
    monkeypatch.setattr(auth.config_store, "set_many", fake_set_many)

    assert auth.setup_required() is True
    assert auth.password_configured() is False

    auth.set_password("secret1")
    assert auth.setup_required() is False
    assert auth.verify_password("secret1") is True
    assert auth.verify_password("wrong") is False

    token = auth.create_session()
    assert auth.validate_session_token(token) is True
    assert auth.validate_session_token("not-a-token") is False

    # Backward-compatible: raw password still accepted as bearer (legacy clients).
    assert auth.validate_session_token("secret1") is True


def test_env_password_bootstrap(monkeypatch):
    store: dict[str, str] = {}
    monkeypatch.setenv("APP_PASSWORD", "env-pass-99")

    def fake_get(key: str, default: str = "") -> str:
        return store.get(key, default)

    def fake_set(key: str, value: str) -> None:
        store[key] = value

    def fake_set_many(data: dict) -> None:
        store.update({k: str(v) for k, v in data.items()})

    monkeypatch.setattr(auth.config_store, "get", fake_get)
    monkeypatch.setattr(auth.config_store, "set", fake_set)
    monkeypatch.setattr(auth.config_store, "set_many", fake_set_many)

    assert auth.setup_required() is False
    assert auth.password_configured() is True
    assert auth.verify_password("env-pass-99") is True
    assert auth.verify_password("other") is False

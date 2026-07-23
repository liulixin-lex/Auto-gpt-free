from __future__ import annotations

import base64

from platforms.chatgpt.from_credentials import (
    certificate_to_sub2api_export,
    get_thread_proxy_url,
    is_agent_registry_disabled_error,
    oauth_tokens_to_sub2api_export,
    set_thread_proxy_url,
)


def test_explicit_empty_proxy_disables_environment_proxy(monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://environment-proxy.test:8080")
    set_thread_proxy_url("")
    try:
        assert get_thread_proxy_url() is None
    finally:
        set_thread_proxy_url(None)


def test_proxy_falls_back_to_environment_without_thread_override(monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://environment-proxy.test:8080")
    set_thread_proxy_url(None)
    assert get_thread_proxy_url() == "http://environment-proxy.test:8080"


def test_sub2api_export_uses_direct_agent_identity_auth_json():
    payload = certificate_to_sub2api_export(
        {
            "private_key_seed": base64.b64encode(b"z" * 32).decode("ascii"),
            "agent_runtime_id": "agent-test",
            "task_id": "task-test",
            "account_id": "account-test",
            "chatgpt_user_id": "user-test",
            "email": "identity@test.com",
            "plan_type": "free",
            "chatgpt_account_is_fedramp": False,
        }
    )

    assert payload["auth_mode"] == "agentIdentity"
    assert payload["OPENAI_API_KEY"] is None
    assert payload["agent_identity"]["account_id"] == "account-test"
    assert payload["agent_identity"]["agent_private_key"]
    assert payload["type"] == "sub2api-data"
    assert payload["version"] == 1
    assert payload["proxies"] == []
    assert payload["accounts"][0]["credentials"]["auth_mode"] == "agentIdentity"
    assert payload["accounts"][0]["credentials"]["chatgpt_account_id"] == "account-test"


def test_is_agent_registry_disabled_error():
    assert is_agent_registry_disabled_error(
        'code": "agent_registry_not_enabled"'
    )
    assert is_agent_registry_disabled_error("Agent registry is not enabled.")
    assert not is_agent_registry_disabled_error("token_revoked")


def test_oauth_fallback_sub2api_export_shape():
    payload = oauth_tokens_to_sub2api_export(
        access_token="at-test",
        refresh_token="rt-test",
        email="oauth@test.com",
        account_id="acct-1",
        chatgpt_user_id="user-1",
        plan_type="free",
    )
    assert payload["type"] == "sub2api-data"
    assert payload["auth_mode"] == "oauth"
    assert payload["_export_mode"] == "codex_oauth"
    assert payload["tokens"]["access_token"] == "at-test"
    assert payload["accounts"][0]["credentials"]["auth_mode"] == "oauth"
    assert payload["accounts"][0]["credentials"]["access_token"] == "at-test"
    assert "Agent Registry" in (payload.get("_warning") or "")


def test_upstream_style_sub2api_oauth_export_fields():
    """Match asz798838958/freeAgentIdentity _make_sub2api_json credential keys."""
    from application.account_exports import _make_sub2api_json
    from domain.accounts import AccountRecord
    from datetime import datetime, timezone

    item = AccountRecord(
        id=1,
        platform="chatgpt",
        email="free@test.com",
        password="x",
        user_id="acct-xyz",
        primary_token="at-free",
        trial_end_time=0,
        cashier_url="",
        lifecycle_status="registered",
        validity_status="valid",
        plan_state="free",
        plan_name="free",
        display_status="registered",
        overview={},
        display_summary={},
        credentials=[
            {"scope": "platform", "key": "access_token", "value": "at-free"},
            {"scope": "platform", "key": "refresh_token", "value": "rt-free"},
            {"scope": "platform", "key": "account_id", "value": "acct-xyz"},
            {"scope": "platform", "key": "client_id", "value": "app_test"},
        ],
        provider_accounts=[],
        provider_resources=[],
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )
    payload = _make_sub2api_json(item)
    creds = payload["accounts"][0]["credentials"]
    assert creds["access_token"] == "at-free"
    assert creds["refresh_token"] == "rt-free"
    assert creds["chatgpt_account_id"] == "acct-xyz"
    assert "expires_at" in creds
    assert "model_mapping" in creds
    assert payload["_export_mode"] == "codex_oauth"

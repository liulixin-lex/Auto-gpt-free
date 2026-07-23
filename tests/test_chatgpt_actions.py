from core.base_platform import Account
from platforms.chatgpt.plugin import ChatGPTPlatform


def _account() -> Account:
    return Account(
        platform="chatgpt",
        email="test@example.com",
        password="Secret123!",
        token="access-token",
        extra={
            "access_token": "access-token",
            "session_token": "session-token",
            "cookies": "cookie=value",
        },
    )


def test_removed_actions_are_not_exposed():
    action_ids = {item["id"] for item in ChatGPTPlatform().get_platform_actions()}

    assert action_ids == {"upload_cpa"}


def test_upload_cpa_reaches_platform_implementation(monkeypatch):
    platform = ChatGPTPlatform()
    captured = []

    def fake_platform_action(action_id, account, params):
        captured.append((action_id, params))
        return {"ok": True, "data": {"action": action_id}}

    monkeypatch.setattr(platform, "_execute_platform_action", fake_platform_action)

    cpa = platform.execute_action("upload_cpa", _account(), {"api_url": "https://cpa.example"})

    assert cpa["ok"] is True
    assert captured == [
        ("upload_cpa", {"api_url": "https://cpa.example"}),
    ]

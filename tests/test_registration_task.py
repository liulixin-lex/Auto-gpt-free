from __future__ import annotations

from types import SimpleNamespace

import pytest

from application import tasks as tasks_module
from core.base_platform import Account
from core.db import Session, TaskModel, engine
from infrastructure.registration_repository import ResourceLeaseConflict


class _FakeLogger:
    def __init__(self):
        self.events = []
        self.result_data = None
        self.finished = None
        self.cancel_requested = False

    def log(self, message, **kwargs):
        self.events.append(("log", message, kwargs))

    def record_error(self, error):
        self.events.append(("error", error, {}))

    def record_success(self):
        self.events.append(("success", "", {}))

    def set_result_data(self, data):
        current = self.result_data if isinstance(self.result_data, dict) else {}
        self.result_data = {**current, **data} if isinstance(data, dict) else data

    def set_progress(self, current, total):
        self.events.append(("progress", current, {"total": total}))

    def is_cancel_requested(self):
        return self.cancel_requested

    def set_subtask(self, subtask_id, label=""):
        self.events.append(("subtask", subtask_id, {"label": label}))

    def clear_subtask(self):
        self.events.append(("clear_subtask", "", {}))

    def finish(self, status, *, error=""):
        self.finished = (status, error)


def test_chatgpt_register_task_succeeds_without_post_processing(monkeypatch):
    class FakePlatform:
        def register(self, email=None, password=None):
            return Account(
                platform="chatgpt",
                email=email or "registered@example.com",
                password=password or "Secret123!",
                user_id="acct_123",
                extra={"access_token": "access-token"},
            )

    monkeypatch.setattr(tasks_module, "get", lambda platform_name: object)
    monkeypatch.setattr(
        tasks_module,
        "_resolve_registration_proxy_for_platform",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        tasks_module,
        "_build_platform_instance",
        lambda *args, **kwargs: FakePlatform(),
    )
    monkeypatch.setattr(
        tasks_module,
        "save_account",
        lambda account: type("SavedAccount", (), {"id": 123})(),
    )
    monkeypatch.setattr(
        "core.base_mailbox.create_mailbox", lambda *args, **kwargs: object()
    )

    logger = _FakeLogger()
    tasks_module._execute_register_task(
        {
            "platform": "chatgpt",
            "count": 1,
            "concurrency": 1,
            "email": "registered@example.com",
            "password": "Secret123!",
            "extra": {
                "identity_provider": "mailbox",
                "auto_download_agent_identity": True,
            },
        },
        logger,
    )

    assert logger.finished == (tasks_module.TASK_STATUS_SUCCEEDED, "")
    assert logger.result_data["success"] == 1
    assert logger.result_data["fail"] == 0
    assert logger.result_data["account_ids"] == [123]
    assert logger.result_data["accounts"] == [
        {"account_id": 123, "email": "registered@example.com"}
    ]
    assert "sub2api_agent_identity_upload" not in logger.result_data
    assert logger.result_data["active_concurrency"] == 0
    assert logger.result_data["peak_active_concurrency"] == 1
    assert logger.result_data["effective_concurrency"] == 1


def test_protocol_task_reuses_one_transport_profile_per_egress(monkeypatch):
    profiles = []
    profile_calls = []

    class FakePlatform:
        def register(self, email=None, password=None):
            return Account(
                platform="chatgpt",
                email=email or f"registered-{len(profiles)}@example.com",
                password=password or "Secret123!",
                user_id=f"acct_{len(profiles)}",
                extra={"access_token": "access-token"},
            )

    def fake_profile():
        profile_calls.append(True)
        return {
            "key": "chrome145",
            "impersonate": "chrome145",
            "user_agent": "UA/stable-egress",
            "fingerprint_id": "chrome145",
        }

    def fake_build(_platform_name, payload, *_args, **_kwargs):
        profiles.append(dict(payload["extra"]["browser_profile"]))
        return FakePlatform()

    monkeypatch.setattr(tasks_module, "get", lambda _platform_name: object)
    monkeypatch.setattr(
        tasks_module,
        "_resolve_registration_proxy_for_platform",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(tasks_module, "_registration_concurrency", lambda *args, **kwargs: 1)
    monkeypatch.setattr(
        "core.proxy_runtime.resolve_egress_ref",
        lambda *_args, **_kwargs: "egress:stable-direct",
    )
    monkeypatch.setattr("platforms.chatgpt.browser_profiles.pick_chrome_profile", fake_profile)
    monkeypatch.setattr(tasks_module, "_build_platform_instance", fake_build)
    monkeypatch.setattr(
        tasks_module,
        "save_account",
        lambda _account: SimpleNamespace(id=100 + len(profiles)),
    )

    logger = _FakeLogger()
    logger.task_id = "stable-egress-profile-task"
    tasks_module._execute_register_task(
        {
            "platform": "chatgpt",
            "count": 2,
            "concurrency": 1,
            "executor_type": "protocol",
            "password": "Secret123!",
            "extra": {
                "identity_provider": "mailbox",
                "disable_register_jitter": True,
                "disable_register_cooldown": True,
            },
        },
        logger,
    )

    assert logger.finished == (tasks_module.TASK_STATUS_SUCCEEDED, "")
    assert len(profile_calls) == 1
    assert len(profiles) == 2
    assert profiles[0] == profiles[1]
    assert profiles[0]["egress_id"] == "egress:stable-direct"
    assert profiles[0]["proxy_lease_id"] == "egress:stable-direct"
    assert profiles[0]["sticky_session_id"] == "egress:stable-direct"


def test_register_task_rechecks_adaptive_limit_before_refill(monkeypatch):
    class FakePlatform:
        def register(self, email=None, password=None):
            return Account(
                platform="chatgpt",
                email=email or "registered@example.com",
                password=password or "Secret123!",
                user_id="acct_dynamic",
                extra={"access_token": "access-token"},
            )

    limits = iter((2, 2, 1, 1, 1, 1))
    saved_ids = iter((201, 202, 203))
    monkeypatch.setattr(tasks_module, "get", lambda _platform_name: object)
    monkeypatch.setattr(
        tasks_module,
        "_registration_concurrency",
        lambda *_args, **_kwargs: next(limits, 1),
    )
    monkeypatch.setattr(
        tasks_module,
        "_resolve_registration_proxy_for_platform",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        tasks_module,
        "_build_platform_instance",
        lambda *args, **kwargs: FakePlatform(),
    )
    monkeypatch.setattr(
        tasks_module,
        "save_account",
        lambda _account: type("SavedAccount", (), {"id": next(saved_ids)})(),
    )

    logger = _FakeLogger()
    logger.task_id = "dynamic-limit-task"
    tasks_module._execute_register_task(
        {
            "platform": "chatgpt",
            "count": 3,
            "concurrency": 2,
            "password": "Secret123!",
            "extra": {
                "identity_provider": "mailbox",
                "disable_register_jitter": True,
                "disable_register_cooldown": True,
            },
        },
        logger,
    )

    messages = [event[1] for event in logger.events if event[0] == "log"]
    assert "并发上限动态调整 2 -> 1" in messages
    assert logger.result_data["success"] == 3
    assert logger.result_data["current_concurrency_limit"] == 1


def test_register_task_waits_for_cross_task_egress_lease(monkeypatch):
    class FakePlatform:
        def register(self, email=None, password=None):
            return Account(
                platform="chatgpt",
                email=email or "queued@example.com",
                password=password or "Secret123!",
                extra={"access_token": "access-token"},
            )

    calls = {"egress": 0}

    def acquire(**kwargs):
        if kwargs["resource_type"] == "egress":
            calls["egress"] += 1
            if calls["egress"] == 1:
                raise ResourceLeaseConflict("resource already leased: egress")
        return SimpleNamespace(id=f"lease-{kwargs['resource_type']}")

    monkeypatch.setattr(tasks_module, "get", lambda _platform_name: object)
    monkeypatch.setattr(tasks_module, "_resolve_registration_proxy_for_platform", lambda *a, **k: None)
    monkeypatch.setattr(tasks_module, "_build_platform_instance", lambda *a, **k: FakePlatform())
    monkeypatch.setattr(tasks_module, "save_account", lambda _account: SimpleNamespace(id=301))
    monkeypatch.setattr(tasks_module.resource_leases, "acquire", acquire)
    monkeypatch.setattr(tasks_module.resource_leases, "release_owner", lambda *_a, **_k: None)
    monkeypatch.setattr(tasks_module.resource_leases, "mark_resource", lambda **_k: True)

    logger = _FakeLogger()
    logger.task_id = "egress-wait-task"
    tasks_module._execute_register_task(
        {
            "platform": "chatgpt",
            "count": 1,
            "concurrency": 2,
            "email": "queued@example.com",
            "password": "Secret123!",
            "extra": {"disable_register_jitter": True, "disable_register_cooldown": True},
        },
        logger,
    )

    assert calls["egress"] == 2
    assert logger.finished == (tasks_module.TASK_STATUS_SUCCEEDED, "")
    assert any("继续排队" in event[1] for event in logger.events if event[0] == "log")


def test_direct_attempts_share_one_egress_pacing_key(monkeypatch):
    from itertools import count

    account_ids = count(401)
    account_ordinals = count(1)
    lease_ids = count(1)
    pace_keys: list[str] = []

    class FakePlatform:
        def register(self, email=None, password=None):
            ordinal = next(account_ordinals)
            return Account(
                platform="chatgpt",
                email=email or f"paced-{ordinal}@example.com",
                password=password or "Secret123!",
                extra={"access_token": "access-token"},
            )

    def acquire(**kwargs):
        return SimpleNamespace(id=f"lease-{kwargs['resource_type']}-{next(lease_ids)}")

    def pace(key, _gap, **_kwargs):
        pace_keys.append(key)
        return 0.0

    monkeypatch.setattr(tasks_module, "get", lambda _platform_name: object)
    monkeypatch.setattr(tasks_module, "_registration_concurrency", lambda *_a, **_k: 2)
    monkeypatch.setattr(tasks_module, "_resolve_registration_proxy_for_platform", lambda *a, **k: None)
    monkeypatch.setattr("core.proxy_runtime.resolve_egress_ref", lambda *_a, **_k: "egress:shared")
    monkeypatch.setattr(tasks_module, "_build_platform_instance", lambda *a, **k: FakePlatform())
    monkeypatch.setattr(tasks_module, "save_account", lambda _account: SimpleNamespace(id=next(account_ids)))
    monkeypatch.setattr(tasks_module.registration_capacity, "pace", pace)
    monkeypatch.setattr(tasks_module.registration_capacity, "record_outcome", lambda **_k: None)
    monkeypatch.setattr(tasks_module.resource_leases, "acquire", acquire)
    monkeypatch.setattr(tasks_module.resource_leases, "release", lambda *_a, **_k: True)
    monkeypatch.setattr(tasks_module.resource_leases, "release_owner", lambda *_a, **_k: 0)
    monkeypatch.setattr(tasks_module.resource_leases, "mark_resource", lambda **_k: True)

    logger = _FakeLogger()
    logger.task_id = "shared-pacing-task"
    tasks_module._execute_register_task(
        {
            "platform": "chatgpt",
            "count": 2,
            "concurrency": 2,
            "password": "Secret123!",
            "extra": {
                "identity_provider": "mailbox",
                "disable_register_jitter": True,
                "disable_register_cooldown": True,
            },
        },
        logger,
    )

    assert logger.finished == (tasks_module.TASK_STATUS_SUCCEEDED, "")
    assert pace_keys == ["egress:shared", "egress:shared"]


@pytest.mark.parametrize(
    ("mode", "healthy_concurrency", "expected_low", "expected_high"),
    [
        (tasks_module.RegistrationMode.PROTOCOL, 1, 30, 45),
        (tasks_module.RegistrationMode.PROTOCOL, 2, 8, 12),
        (tasks_module.RegistrationMode.HEADLESS, 2, 15, 22),
    ],
)
def test_direct_start_stagger_tracks_proven_egress_health(
    monkeypatch, mode, healthy_concurrency, expected_low, expected_high
):
    monkeypatch.setattr("random.uniform", lambda lo, hi: (lo + hi) / 2)

    value = tasks_module._inter_account_jitter_seconds(
        {},
        has_proxy=False,
        mode=mode,
        healthy_concurrency=healthy_concurrency,
    )

    assert expected_low <= value <= expected_high


def test_proxy_pool_concurrency_uses_available_independent_exit_count():
    assert tasks_module._registration_concurrency(
        3,
        10,
        executor_type="protocol",
        proxy_count=2,
        egress_ref="proxy-pool",
    ) == 2
    assert tasks_module._registration_concurrency(
        3,
        10,
        executor_type="protocol",
        proxy_count=5,
        egress_ref="proxy-pool",
    ) == 3


def test_lease_cleanup_failures_do_not_override_success(monkeypatch):
    class FakePlatform:
        def register(self, email=None, password=None):
            return Account(
                platform="chatgpt",
                email=email or "cleanup@example.com",
                password=password or "Secret123!",
                extra={"access_token": "access-token"},
            )

    monkeypatch.setattr(tasks_module, "get", lambda _platform_name: object)
    monkeypatch.setattr(tasks_module, "_resolve_registration_proxy_for_platform", lambda *a, **k: None)
    monkeypatch.setattr("core.proxy_runtime.resolve_egress_ref", lambda *_a, **_k: "egress:cleanup")
    monkeypatch.setattr(tasks_module, "_build_platform_instance", lambda *a, **k: FakePlatform())
    monkeypatch.setattr(tasks_module, "save_account", lambda _account: SimpleNamespace(id=501))
    monkeypatch.setattr(
        tasks_module.resource_leases,
        "acquire",
        lambda **kwargs: SimpleNamespace(id=f"lease-{kwargs['resource_type']}"),
    )
    monkeypatch.setattr(tasks_module.resource_leases, "mark_resource", lambda **_k: True)
    monkeypatch.setattr(
        tasks_module.resource_leases,
        "release",
        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("database is locked")),
    )
    monkeypatch.setattr(
        tasks_module.resource_leases,
        "release_owner",
        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("owner cleanup failed")),
    )

    logger = _FakeLogger()
    logger.task_id = "cleanup-failure-task"
    tasks_module._execute_register_task(
        {
            "platform": "chatgpt",
            "count": 1,
            "concurrency": 1,
            "email": "cleanup@example.com",
            "password": "Secret123!",
            "extra": {
                "disable_register_jitter": True,
                "disable_register_cooldown": True,
            },
        },
        logger,
    )

    messages = [event[1] for event in logger.events if event[0] == "log"]
    assert logger.finished == (tasks_module.TASK_STATUS_SUCCEEDED, "")
    assert logger.result_data["success"] == 1
    assert any("出口租约释放失败" in message for message in messages)
    assert any("尝试资源释放失败" in message for message in messages)


def test_otp_timeout_replaces_mailbox_inside_same_logical_attempt(monkeypatch):
    builds = {"count": 0}

    class FakePlatform:
        def __init__(self, fail=False):
            self.fail = fail
            self._last_identity = SimpleNamespace(email="stale@example.com")

        def register(self, email=None, password=None):
            if self.fail:
                raise RuntimeError("验证码超时")
            return Account(
                platform="chatgpt",
                email="fresh@example.com",
                password=password or "Secret123!",
                extra={"access_token": "access-token"},
            )

    def build(*_args, **_kwargs):
        builds["count"] += 1
        return FakePlatform(fail=builds["count"] == 1)

    monkeypatch.setattr(tasks_module, "get", lambda _platform_name: object)
    monkeypatch.setattr(tasks_module, "_resolve_registration_proxy_for_platform", lambda *a, **k: None)
    monkeypatch.setattr(tasks_module, "_build_platform_instance", build)
    monkeypatch.setattr(tasks_module, "save_account", lambda _account: SimpleNamespace(id=302))

    logger = _FakeLogger()
    logger.task_id = "otp-replacement-task"
    tasks_module._execute_register_task(
        {
            "platform": "chatgpt",
            "count": 1,
            "concurrency": 1,
            "password": "Secret123!",
            "extra": {"disable_register_jitter": True, "disable_register_cooldown": True},
        },
        logger,
    )

    assert builds["count"] == 2
    assert logger.finished == (tasks_module.TASK_STATUS_SUCCEEDED, "")
    assert logger.result_data["success"] == 1
    assert logger.result_data["replacement_count"] == 1
    assert any("更换邮箱" in event[1] for event in logger.events if event[0] == "log")


def test_register_api_preserves_protocol_outlook_pool(client, monkeypatch):
    captured = {}

    def fake_create(payload):
        captured.update(payload)
        return {"task_id": "task_protocol"}

    monkeypatch.setattr(
        "api.task_commands.command_service.create_register_task", fake_create
    )
    pool_text = "user@outlook.com----mail-pass----client-id----refresh-token"

    response = client.post(
        "/api/tasks/register",
        json={
            "count": 1,
            "concurrency": 1,
            "executor_type": "protocol",
            "extra": {
                "local_ms_pool_text": pool_text,
                "auto_download_agent_identity": True,
            },
        },
    )

    assert response.status_code == 200
    assert captured["executor_type"] == "protocol"
    assert captured["extra"]["mail_provider"] == "local_ms_pool"
    assert captured["extra"]["local_ms_pool_text"] == pool_text


def test_register_api_rejects_protocol_outlook_without_pool_text(client):
    response = client.post(
        "/api/tasks/register",
        json={
            "executor_type": "protocol",
            "count": 1,
            "extra": {"mail_provider": "local_ms_pool"},
        },
    )

    assert response.status_code == 400
    assert "Outlook" in (response.json().get("detail") or "")


def test_cancelled_claimed_task_cannot_be_marked_running():
    task = tasks_module.create_task(
        task_type=tasks_module.TASK_TYPE_REGISTER,
        platform="chatgpt",
        payload={"count": 2, "concurrency": 1},
        progress_total=2,
    )
    task_id = task["id"]
    with Session(engine) as session:
        model = session.get(TaskModel, task_id)
        assert model is not None
        model.status = tasks_module.TASK_STATUS_CLAIMED
        session.add(model)
        session.commit()

    requested = tasks_module.request_cancel(task_id)
    assert requested is not None
    assert requested["status"] == tasks_module.TASK_STATUS_CANCEL_REQUESTED

    logger = tasks_module.TaskLogger(task_id)
    assert logger.mark_running() is False

    with Session(engine) as session:
        model = session.get(TaskModel, task_id)
        assert model is not None
        assert model.status == tasks_module.TASK_STATUS_CANCEL_REQUESTED

    logger.finish(tasks_module.TASK_STATUS_SUCCEEDED)
    with Session(engine) as session:
        model = session.get(TaskModel, task_id)
        assert model is not None
        assert model.status == tasks_module.TASK_STATUS_CANCELLED


def test_cancel_endpoint_marks_pending_task_cancelled_without_worker(client):
    response = client.post(
        "/api/tasks/register",
        json={"count": 3, "concurrency": 1, "executor_type": "headless"},
    )
    assert response.status_code == 200
    task_id = response.json()["id"]

    cancelled = client.post(f"/api/tasks/{task_id}/cancel")
    assert cancelled.status_code == 200
    body = cancelled.json()
    assert body["status"] == tasks_module.TASK_STATUS_CANCELLED
    assert body["terminal"] is True
    assert body["cancellable"] is False

    detail = client.get(f"/api/tasks/{task_id}")
    assert detail.status_code == 200
    detail_body = detail.json()
    assert detail_body["terminal"] is True
    assert detail_body["cancel_requested"] is False
    assert detail_body["cancellable"] is False


def test_task_detail_exposes_manual_stop_state_for_pending_task(client):
    response = client.post(
        "/api/tasks/register",
        json={"count": 2, "concurrency": 1, "executor_type": "headless"},
    )
    assert response.status_code == 200
    task_id = response.json()["id"]

    detail = client.get(f"/api/tasks/{task_id}")

    assert detail.status_code == 200
    body = detail.json()
    assert body["terminal"] is False
    assert body["cancel_requested"] is False
    assert body["cancellable"] is True


def test_register_batch_does_not_refill_after_cancel_signal(monkeypatch):
    calls = {"register": 0}

    class FakePlatform:
        def register(self, email=None, password=None):
            calls["register"] += 1
            logger.cancel_requested = True
            return Account(
                platform="chatgpt",
                email=email or "stop-after-first@example.com",
                password=password or "Secret123!",
                extra={"access_token": "access-token"},
            )

    monkeypatch.setattr(tasks_module, "get", lambda _platform_name: object)
    monkeypatch.setattr(
        tasks_module,
        "_resolve_registration_proxy_for_platform",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        tasks_module,
        "_registration_concurrency",
        lambda *_args, **_kwargs: 1,
    )
    monkeypatch.setattr(
        tasks_module,
        "_build_platform_instance",
        lambda *args, **kwargs: FakePlatform(),
    )
    monkeypatch.setattr(
        tasks_module,
        "save_account",
        lambda _account: SimpleNamespace(id=901),
    )

    logger = _FakeLogger()
    logger.task_id = "cancel-refill-task"
    tasks_module._execute_register_task(
        {
            "platform": "chatgpt",
            "count": 5,
            "concurrency": 1,
            "email": "stop-after-first@example.com",
            "password": "Secret123!",
            "extra": {
                "identity_provider": "mailbox",
                "disable_register_jitter": True,
                "disable_register_cooldown": True,
            },
        },
        logger,
    )

    assert calls["register"] == 1
    progress = [event for event in logger.events if event[0] == "progress"]
    assert [(event[1], event[2]["total"]) for event in progress] == [(0, 5), (1, 5)]
    assert logger.finished == (tasks_module.TASK_STATUS_CANCELLED, "任务已取消")

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import time

from application import tasks as tasks_module
from domain.registration_runtime import (
    AttemptContext,
    RegistrationAttemptStatus,
    RegistrationErrorCode,
    RegistrationMode,
    RegistrationStage,
)
from infrastructure.registration_repository import RegistrationAttemptRepository
from infrastructure.registration_repository import registration_artifacts


def _create_task(task_id_suffix: str = "observability") -> dict:
    return tasks_module.create_task(
        task_type=tasks_module.TASK_TYPE_REGISTER,
        platform="chatgpt",
        payload={"executor_type": "protocol", "count": 2},
        progress_total=2,
        result_seed={"test": task_id_suffix},
    )


def _attempt(task_id: str, ordinal: int, mode: RegistrationMode) -> AttemptContext:
    return AttemptContext(
        task_id=task_id,
        ordinal=ordinal,
        requested_mode=mode,
        effective_mode=mode,
        deadline_monotonic=time.monotonic() + 60,
        mail_provider="mail-test",
        proxy_ref=f"sha256:proxy-{ordinal}",
        fingerprint_id=f"fingerprint-{ordinal}",
    )


def test_capabilities_api_exposes_only_strict_independent_modes(client):
    response = client.get("/api/registration/capabilities")
    checked = client.post("/api/registration/capabilities/test")

    assert response.status_code == 200
    assert checked.status_code == 200
    payload = response.json()
    assert payload["automatic_probe"] is False
    assert payload["browser_engine"] == "camoufox"
    assert payload["run_checks"] is False
    assert [item["mode"] for item in payload["items"]] == [
        "protocol",
        "headless",
        "headed",
    ]
    assert all(item["strict_mode"] is True for item in payload["items"])
    assert checked.json()["run_checks"] is True


def test_attempts_and_summary_api_apply_structured_filters(client):
    task = _create_task("summary")
    repository = RegistrationAttemptRepository()
    succeeded = _attempt(task["id"], 1, RegistrationMode.PROTOCOL)
    failed = _attempt(task["id"], 2, RegistrationMode.HEADLESS)

    repository.start(succeeded)
    repository.finish(
        succeeded.attempt_id,
        status=RegistrationAttemptStatus.SUCCEEDED,
        metadata={"source": "test"},
    )
    repository.start(failed)
    repository.stage(failed.attempt_id, RegistrationStage.OTP_WAIT, retry_count=2)
    repository.finish(
        failed.attempt_id,
        status=RegistrationAttemptStatus.FAILED,
        error_code=RegistrationErrorCode.OTP_TIMEOUT,
        error_stage=RegistrationStage.OTP_WAIT,
        error_message="OTP timeout",
    )

    attempts = client.get(
        f"/api/tasks/{task['id']}/attempts",
        params={
            "status": "failed",
            "mode": "headless",
            "stage": "otp_wait",
            "error_code": "OTP_TIMEOUT",
        },
    )
    summary = client.get(f"/api/tasks/{task['id']}/summary")

    assert attempts.status_code == 200
    assert attempts.json()["total"] == 1
    assert attempts.json()["items"][0]["attempt_id"] == failed.attempt_id
    assert summary.status_code == 200
    payload = summary.json()
    assert payload["total"] == 2
    assert payload["completed"] == 2
    assert payload["success"] == 1
    assert payload["failed"] == 1
    assert payload["success_rate"] == 0.5
    assert payload["retry_count"] == 2
    assert payload["retry_rate"] == 0.5
    assert payload["error_codes"] == {"OTP_TIMEOUT": 1}
    assert payload["modes"] == {"protocol": 1, "headless": 1}
    assert payload["stages"]["done"] == 1
    assert payload["stages"]["otp_wait"] == 1


def test_event_filters_and_concurrent_batch_writer_do_not_lose_events(client):
    task = _create_task("events")
    attempt_id = "attempt-batch"
    total = 50

    def append(index: int):
        level = "error" if index % 2 else "info"
        error_code = "OTP_TIMEOUT" if level == "error" else ""
        return tasks_module.append_task_event(
            task["id"],
            f"event-{index}",
            event_type="retry",
            level=level,
            detail={
                "attempt_id": attempt_id,
                "kind": "retry",
                "stage": "otp_wait",
                "action": "poll",
                "error_code": error_code,
                "retryable": True,
                "retry_index": index,
                "schema_version": 2,
            },
        )

    with ThreadPoolExecutor(max_workers=10) as pool:
        written = list(pool.map(append, range(total)))

    assert len({item["id"] for item in written}) == total
    assert tasks_module.task_event_writer.flush(timeout=2) is True

    events = client.get(
        f"/api/tasks/{task['id']}/events",
        params={
            "attempt_id": attempt_id,
            "stage": "otp_wait",
            "level": "error",
            "error_code": "OTP_TIMEOUT",
            "limit": 200,
        },
    )
    assert events.status_code == 200
    items = events.json()["items"]
    assert len(items) == total // 2
    assert all(item["attempt_id"] == attempt_id for item in items)
    assert all(item["stage"] == "otp_wait" for item in items)
    assert all(item["level"] == "error" for item in items)
    assert all(item["error_code"] == "OTP_TIMEOUT" for item in items)
    assert all(item["schema_version"] == 2 for item in items)

    all_attempt_events = tasks_module.list_task_events(
        task["id"], attempt_id=attempt_id, limit=200
    )
    assert len(all_attempt_events) == total
    assert sorted(item["seq"] for item in all_attempt_events) == list(range(1, total + 1))


def test_recovered_error_is_not_reported_as_terminal_top_error(client):
    task = tasks_module.create_task(
        task_type=tasks_module.TASK_TYPE_REGISTER,
        platform="chatgpt",
        payload={"executor_type": "protocol", "count": 1},
        progress_total=1,
    )
    logger = tasks_module.TaskLogger(task["id"])
    logger.set_result_data(
        {
            "success": 1,
            "fail": 0,
            "error_classes": {"OTP_TIMEOUT": 1},
            "active_concurrency": 0,
            "peak_active_concurrency": 1,
        }
    )
    logger.set_progress(1, 1)
    logger.finish(tasks_module.TASK_STATUS_SUCCEEDED)

    detail = client.get(f"/api/tasks/{task['id']}").json()

    assert detail["status"] == "succeeded"
    assert detail["top_error_code"] == ""


def test_failure_artifacts_are_hashed_and_queryable(client, tmp_path):
    task = _create_task("artifacts")
    screenshot = tmp_path / "screenshot.png"
    diagnostic = tmp_path / "diagnostic.json"
    screenshot.write_bytes(b"redacted-image")
    diagnostic.write_text('{"redacted": true}', encoding="utf-8")

    written = registration_artifacts.add_bundle(
        task_id=task["id"],
        attempt_id="attempt-artifact",
        bundle={
            "screenshot": str(screenshot),
            "diagnostic": str(diagnostic),
        },
    )
    response = client.get(
        f"/api/tasks/{task['id']}/artifacts",
        params={"attempt_id": "attempt-artifact"},
    )

    assert len(written) == 2
    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] == 2
    assert {item["artifact_type"] for item in payload["items"]} == {
        "screenshot",
        "diagnostic",
    }
    assert all(len(item["sha256"]) == 64 for item in payload["items"])
    assert all(item["redacted"] is True for item in payload["items"])


def test_register_api_removes_legacy_fallback_and_locks_requested_mode(client, monkeypatch):
    captured: dict = {}

    def fake_create(payload):
        captured.update(payload)
        return {"task_id": "task-strict", "id": "task-strict"}

    monkeypatch.setattr("api.task_commands.command_service.create_register_task", fake_create)
    monkeypatch.setattr("core.proxy_pool.proxy_pool.count_available", lambda: 0)

    response = client.post(
        "/api/tasks/register",
        json={
            "executor_type": "headed",
            "count": 1,
            "concurrency": 8,
            "extra": {"browser_fallback_on_cf": True},
        },
    )

    assert response.status_code == 200
    assert captured["executor_type"] == "headed"
    assert captured["extra"]["strict_executor_mode"] is True
    assert "browser_fallback_on_cf" not in captured["extra"]
    assert response.json()["requested_concurrency"] == 8
    assert response.json()["effective_concurrency"] == 1


class _RuntimeLogger:
    task_id = "task-strict-runtime"

    def __init__(self):
        self.finished = None
        self.result_data = None

    def log(self, *args, **kwargs):
        return None

    def record_error(self, *args, **kwargs):
        return None

    def record_success(self):
        return None

    def set_result_data(self, data):
        current = self.result_data if isinstance(self.result_data, dict) else {}
        self.result_data = {**current, **data} if isinstance(data, dict) else data

    def set_progress(self, *args, **kwargs):
        return None

    def is_cancel_requested(self):
        return False

    def set_subtask(self, *args, **kwargs):
        return None

    def clear_subtask(self):
        return None

    def finish(self, status, *, error=""):
        self.finished = (status, error)


def test_protocol_failure_never_invokes_browser_supervisor(monkeypatch):
    seen_modes: list[str] = []

    class FailingProtocolPlatform:
        def register(self, email=None, password=None):
            raise RuntimeError("protocol fixture failed")

    def build_platform(_platform, payload, *_args, **_kwargs):
        seen_modes.append(payload["executor_type"])
        return FailingProtocolPlatform()

    monkeypatch.setattr(tasks_module, "get", lambda _name: object())
    monkeypatch.setattr(tasks_module, "_build_platform_instance", build_platform)
    monkeypatch.setattr(tasks_module.registration_capacity, "pace", lambda *_args, **_kwargs: 0)
    monkeypatch.setattr(tasks_module.registration_capacity.adaptive, "record", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        tasks_module.browser_process_supervisor,
        "run",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("browser fallback used")),
    )
    monkeypatch.setattr("core.base_mailbox.create_mailbox", lambda *args, **kwargs: object())
    monkeypatch.setattr("core.proxy_pool.proxy_pool.count_available", lambda: 0)
    monkeypatch.setattr("core.proxy_pool.proxy_pool.get_next", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        "platforms.chatgpt.browser_profiles.pick_chrome_profile",
        lambda: {"key": "fixture", "impersonate": "chrome"},
    )

    logger = _RuntimeLogger()
    tasks_module._execute_register_task(
        {
            "executor_type": "protocol",
            "count": 1,
            "concurrency": 1,
            "email": "fixture@example.com",
            "password": "fixture-password",
            "extra": {
                "mail_provider": "fixture-mail",
                "disable_register_jitter": True,
                "browser_fallback_on_cf": True,
            },
        },
        logger,
    )

    assert seen_modes == ["protocol"]
    assert logger.result_data["success"] == 0
    assert logger.result_data["fail"] == 1
    assert logger.result_data["requested_mode"] == "protocol"
    assert logger.result_data["effective_mode"] == "protocol"
    assert logger.finished[0] == tasks_module.TASK_STATUS_FAILED

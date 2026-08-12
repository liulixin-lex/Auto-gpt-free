from __future__ import annotations

import multiprocessing
import time

import pytest

from services.registration_process import (
    BrowserProcessRequest,
    BrowserProcessSupervisor,
    BrowserWorkerCancelled,
    BrowserWorkerError,
    BrowserWorkerTimeout,
)


def _request() -> BrowserProcessRequest:
    return BrowserProcessRequest(
        platform_name="chatgpt",
        payload={"executor_type": "headless", "extra": {}},
        resolved_proxy=None,
        email="fixture@example.com",
        password="fixture-password",
        heartbeat_seconds=1,
    )


def _success_worker(_request, messages, _stop_event):
    messages.put(
        {
            "type": "result",
            "account": {
                "platform": "chatgpt",
                "email": "fixture@example.com",
                "password": "fixture-password",
                "user_id": "acct-fixture",
                "region": "",
                "token": "token-fixture",
                "status": "registered",
                "trial_end_time": 0,
                "extra": {"access_token": "token-fixture"},
                "created_at": 1,
            },
        }
    )


def _error_worker(_request, messages, _stop_event):
    messages.put({"type": "error", "error": "fixture browser failed"})


def _hanging_worker(_request, messages, stop_event):
    messages.put({"type": "heartbeat"})
    while not stop_event.wait(0.05):
        pass


def _run(supervisor: BrowserProcessSupervisor, **kwargs):
    return supervisor.run(
        _request(),
        timeout_seconds=kwargs.pop("timeout_seconds", 3),
        cancel_check=kwargs.pop("cancel_check", lambda: False),
        log_callback=lambda *_args, **_kwargs: None,
        heartbeat_timeout_seconds=kwargs.pop("heartbeat_timeout_seconds", 2),
        **kwargs,
    )


def test_browser_supervisor_returns_serialized_account():
    account = _run(BrowserProcessSupervisor(worker_target=_success_worker))

    assert account.email == "fixture@example.com"
    assert account.user_id == "acct-fixture"
    assert account.extra["access_token"] == "token-fixture"


def test_browser_supervisor_propagates_worker_error():
    with pytest.raises(BrowserWorkerError, match="fixture browser failed"):
        _run(BrowserProcessSupervisor(worker_target=_error_worker))


def test_browser_supervisor_hard_timeout_reaps_child():
    before = {child.pid for child in multiprocessing.active_children()}
    with pytest.raises(BrowserWorkerTimeout):
        _run(
            BrowserProcessSupervisor(worker_target=_hanging_worker),
            timeout_seconds=1,
            heartbeat_timeout_seconds=5,
        )
    time.sleep(0.1)
    after = {child.pid for child in multiprocessing.active_children()}

    assert after <= before


def test_browser_supervisor_cancellation_reaps_child():
    started = time.monotonic()

    with pytest.raises(BrowserWorkerCancelled):
        _run(
            BrowserProcessSupervisor(worker_target=_hanging_worker),
            timeout_seconds=5,
            cancel_check=lambda: time.monotonic() - started > 0.2,
        )

    assert not any(child.name.startswith("browser-register-") for child in multiprocessing.active_children())

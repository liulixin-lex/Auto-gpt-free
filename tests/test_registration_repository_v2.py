from __future__ import annotations

from datetime import datetime, timedelta, timezone
import time

import pytest
from sqlmodel import Session

from core.db import ResourceLeaseModel, engine
from domain.registration_runtime import (
    AttemptContext,
    RegistrationAttemptStatus,
    RegistrationErrorCode,
    RegistrationMode,
    RegistrationStage,
)
from infrastructure.registration_repository import (
    RegistrationAttemptRepository,
    ResourceLeaseConflict,
    ResourceLeaseRepository,
)


def _context(*, ordinal: int = 1, attempt_id: str | None = None) -> AttemptContext:
    context = AttemptContext(
        task_id="task-repository",
        ordinal=ordinal,
        requested_mode=RegistrationMode.PROTOCOL,
        effective_mode=RegistrationMode.PROTOCOL,
        deadline_monotonic=time.monotonic() + 60,
        mail_provider="mail-test",
        proxy_ref="sha256:proxy",
        fingerprint_id="fingerprint-test",
    )
    if attempt_id:
        context.attempt_id = attempt_id
    return context


def test_attempt_lifecycle_is_idempotent_and_filterable():
    repository = RegistrationAttemptRepository()
    context = _context(attempt_id="attempt-original")

    started = repository.start(context)
    repository.stage(context.attempt_id, RegistrationStage.OTP_WAIT, retry_count=2)
    finished = repository.finish(
        context.attempt_id,
        status=RegistrationAttemptStatus.FAILED,
        error_code=RegistrationErrorCode.OTP_TIMEOUT,
        error_stage=RegistrationStage.OTP_WAIT,
        error_message="access_token=secret-token OTP timeout",
        metadata={"checkpoint": "otp_wait"},
    )

    assert started["status"] == "running"
    assert finished is not None
    assert finished["status"] == "failed"
    assert finished["current_stage"] == "otp_wait"
    assert finished["retry_count"] == 2
    assert finished["error_code"] == "OTP_TIMEOUT"
    assert finished["error_stage"] == "otp_wait"
    assert "secret-token" not in finished["error_message"]
    assert finished["metadata"] == {"checkpoint": "otp_wait"}
    assert finished["finished_at"] is not None
    assert finished["duration_ms"] >= 0

    same_ordinal = _context(attempt_id="attempt-duplicate")
    restarted = repository.start(same_ordinal)
    assert restarted["attempt_id"] == "attempt-original"
    assert same_ordinal.attempt_id == "attempt-original"

    assert len(repository.list_for_task("task-repository", status="running")) == 1
    repository.finish(
        same_ordinal.attempt_id,
        status=RegistrationAttemptStatus.SUCCEEDED,
        metadata={"checkpoint": "done"},
    )
    succeeded = repository.list_for_task(
        "task-repository",
        status="succeeded",
        mode="protocol",
        stage="done",
    )
    assert [item["attempt_id"] for item in succeeded] == ["attempt-original"]


def test_resource_lease_is_exclusive_and_release_allows_reacquire():
    repository = ResourceLeaseRepository()
    lease = repository.acquire(
        resource_type="proxy",
        resource_id="sha256:shared-proxy",
        owner_attempt_id="attempt-a",
        ttl_seconds=30,
        metadata={"source": "test"},
    )

    with pytest.raises(ResourceLeaseConflict):
        repository.acquire(
            resource_type="proxy",
            resource_id="sha256:shared-proxy",
            owner_attempt_id="attempt-b",
            ttl_seconds=30,
        )

    assert repository.heartbeat(lease.id, ttl_seconds=60) is True
    assert repository.release(lease.id) is True

    replacement = repository.acquire(
        resource_type="proxy",
        resource_id="sha256:shared-proxy",
        owner_attempt_id="attempt-b",
        ttl_seconds=30,
    )
    assert replacement.owner_attempt_id == "attempt-b"


def test_resource_lease_heartbeat_refreshes_every_live_lease_for_owner():
    repository = ResourceLeaseRepository()
    first = repository.acquire(
        resource_type="egress",
        resource_id="egress:heartbeat",
        owner_attempt_id="attempt-heartbeat",
        ttl_seconds=5,
    )
    second = repository.acquire(
        resource_type="browser_slot",
        resource_id="browser:heartbeat",
        owner_attempt_id="attempt-heartbeat",
        ttl_seconds=5,
    )

    assert repository.heartbeat_owner("attempt-heartbeat", ttl_seconds=120) == 2
    with Session(engine) as session:
        refreshed_first = session.get(ResourceLeaseModel, first.id)
        refreshed_second = session.get(ResourceLeaseModel, second.id)
        assert refreshed_first is not None and refreshed_second is not None
        now = datetime.now(timezone.utc)
        assert refreshed_first.lease_until.replace(tzinfo=timezone.utc) > now + timedelta(seconds=100)
        assert refreshed_second.lease_until.replace(tzinfo=timezone.utc) > now + timedelta(seconds=100)


def test_resource_cooldown_blocks_immediate_reacquire():
    repository = ResourceLeaseRepository()
    lease = repository.acquire(
        resource_type="captcha_provider",
        resource_id="local_solver:0",
        owner_attempt_id="attempt-cooldown-a",
        ttl_seconds=30,
    )
    assert repository.release(lease.id, status="cooldown", cooldown_seconds=10) is True

    with pytest.raises(ResourceLeaseConflict, match="cooling down"):
        repository.acquire(
            resource_type="captcha_provider",
            resource_id="local_solver:0",
            owner_attempt_id="attempt-cooldown-b",
            ttl_seconds=30,
        )


def test_resource_lease_ttl_is_reaped_and_owner_release_is_atomic():
    repository = ResourceLeaseRepository()
    expired = repository.acquire(
        resource_type="mailbox",
        resource_id="sha256:mailbox-a",
        owner_attempt_id="attempt-expired",
        ttl_seconds=30,
    )
    active = repository.acquire(
        resource_type="fingerprint",
        resource_id="fingerprint-a",
        owner_attempt_id="attempt-active",
        ttl_seconds=30,
    )
    repository.acquire(
        resource_type="browser_slot",
        resource_id="browser-a",
        owner_attempt_id="attempt-active",
        ttl_seconds=30,
    )

    with Session(engine) as session:
        model = session.get(ResourceLeaseModel, expired.id)
        assert model is not None
        model.lease_until = datetime.now(timezone.utc) - timedelta(seconds=1)
        session.add(model)
        session.commit()

    assert repository.reap_expired() == 1
    assert repository.release_owner("attempt-active") == 2
    assert repository.heartbeat(active.id, ttl_seconds=30) is False

    replacement = repository.acquire(
        resource_type="mailbox",
        resource_id="sha256:mailbox-a",
        owner_attempt_id="attempt-new",
        ttl_seconds=30,
    )
    assert replacement.owner_attempt_id == "attempt-new"

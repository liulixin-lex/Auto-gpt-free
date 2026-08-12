"""Persistence adapters for registration attempts and resource leases."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
from typing import Any
from uuid import uuid4

from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from core.db import (
    AccountModel,
    RegistrationArtifactModel,
    RegistrationAttemptModel,
    RegistrationResourceHealthModel,
    ResourceLeaseModel,
    engine,
)
from domain.registration_runtime import (
    AttemptContext,
    RegistrationAttemptStatus,
    RegistrationErrorCode,
    RegistrationStage,
    redact_registration_text,
    stable_resource_ref,
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def serialize_attempt(model: RegistrationAttemptModel) -> dict[str, Any]:
    return {
        "attempt_id": model.attempt_id,
        "task_id": model.task_id,
        "ordinal": model.ordinal,
        "requested_mode": model.requested_mode,
        "effective_mode": model.effective_mode,
        "status": model.status,
        "current_stage": model.current_stage,
        "mail_provider": model.mail_provider,
        "proxy_ref_hash": model.proxy_ref_hash,
        "fingerprint_id": model.fingerprint_id,
        "retry_count": model.retry_count,
        "replacement_count": model.replacement_count,
        "error_code": model.error_code,
        "error_stage": model.error_stage,
        "error_message": model.error_message,
        "account_id": model.account_id,
        "metadata": model.get_metadata(),
        "duration_ms": model.duration_ms,
        "started_at": _iso(model.started_at),
        "finished_at": _iso(model.finished_at),
        "created_at": _iso(model.created_at),
        "updated_at": _iso(model.updated_at),
    }


class RegistrationAttemptRepository:
    def start(self, context: AttemptContext) -> dict[str, Any]:
        now = _utcnow()
        with Session(engine) as session:
            model = session.exec(
                select(RegistrationAttemptModel)
                .where(RegistrationAttemptModel.task_id == context.task_id)
                .where(RegistrationAttemptModel.ordinal == context.ordinal)
            ).first()
            if model is None:
                model = RegistrationAttemptModel(
                    attempt_id=context.attempt_id,
                    task_id=context.task_id,
                    ordinal=context.ordinal,
                )
            else:
                context.attempt_id = model.attempt_id
            model.requested_mode = context.requested_mode.value
            model.effective_mode = context.effective_mode.value
            model.status = RegistrationAttemptStatus.RUNNING.value
            model.current_stage = context.stage.value
            model.mail_provider = context.mail_provider
            model.proxy_ref_hash = context.proxy_ref
            model.fingerprint_id = context.fingerprint_id
            model.started_at = model.started_at or now
            model.finished_at = None
            model.updated_at = now
            session.add(model)
            session.commit()
            session.refresh(model)
            return serialize_attempt(model)

    def stage(
        self,
        attempt_id: str,
        stage: RegistrationStage,
        *,
        retry_count: int | None = None,
        replacement_count: int | None = None,
    ) -> None:
        with Session(engine) as session:
            model = session.get(RegistrationAttemptModel, attempt_id)
            if model is None:
                return
            model.current_stage = stage.value
            if retry_count is not None:
                model.retry_count = max(int(retry_count), 0)
            if replacement_count is not None:
                model.replacement_count = max(int(replacement_count), 0)
            model.updated_at = _utcnow()
            session.add(model)
            session.commit()

    def finish(
        self,
        attempt_id: str,
        *,
        status: RegistrationAttemptStatus,
        account_id: int | None = None,
        error_code: RegistrationErrorCode | None = None,
        error_stage: RegistrationStage | None = None,
        error_message: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        now = _utcnow()
        with Session(engine) as session:
            model = session.get(RegistrationAttemptModel, attempt_id)
            if model is None:
                return None
            model.status = status.value
            model.current_stage = (
                RegistrationStage.DONE.value
                if status == RegistrationAttemptStatus.SUCCEEDED
                else model.current_stage
            )
            model.account_id = account_id
            model.error_code = error_code.value if error_code else ""
            model.error_stage = error_stage.value if error_stage else ""
            model.error_message = redact_registration_text(error_message)[:1000]
            model.finished_at = now
            started = model.started_at or model.created_at
            if started.tzinfo is None:
                started = started.replace(tzinfo=timezone.utc)
            model.duration_ms = max(int((now - started).total_seconds() * 1000), 0)
            if metadata is not None:
                model.set_metadata(metadata)
            model.updated_at = now
            session.add(model)
            session.commit()
            session.refresh(model)
            return serialize_attempt(model)

    def get(self, attempt_id: str) -> dict[str, Any] | None:
        with Session(engine) as session:
            model = session.get(RegistrationAttemptModel, attempt_id)
            return serialize_attempt(model) if model else None

    def list_for_task(
        self,
        task_id: str,
        *,
        status: str = "",
        mode: str = "",
        stage: str = "",
        error_code: str = "",
        limit: int = 200,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        query = select(RegistrationAttemptModel).where(RegistrationAttemptModel.task_id == task_id)
        if status:
            query = query.where(RegistrationAttemptModel.status == status)
        if mode:
            query = query.where(RegistrationAttemptModel.effective_mode == mode)
        if stage:
            query = query.where(RegistrationAttemptModel.current_stage == stage)
        if error_code:
            query = query.where(RegistrationAttemptModel.error_code == error_code)
        query = query.order_by(RegistrationAttemptModel.ordinal).offset(max(int(offset), 0)).limit(
            max(1, min(int(limit), 1000))
        )
        with Session(engine) as session:
            return [serialize_attempt(item) for item in session.exec(query).all()]


class ResourceLeaseConflict(RuntimeError):
    pass


LIVE_RESOURCE_STATES = {"active", "reserved", "side_effect_started"}


class ResourceLeaseRepository:
    def acquire(
        self,
        *,
        resource_type: str,
        resource_id: str,
        owner_attempt_id: str,
        ttl_seconds: int,
        metadata: dict[str, Any] | None = None,
    ) -> ResourceLeaseModel:
        now = _utcnow()
        active_key = f"{resource_type}:{resource_id}"
        with Session(engine) as session:
            if resource_type == "mailbox":
                # Backward-compatible identity registry: accounts saved before
                # mailbox lifecycle tracking must still never be allocated again.
                account_emails = session.exec(select(AccountModel.email)).all()
                reused = any(
                    stable_resource_ref(str(item or "").strip().lower()) == resource_id
                    for item in account_emails
                    if str(item or "").strip()
                )
                if reused:
                    raise ResourceLeaseConflict("MAILBOX_REUSED: mailbox already belongs to an account")
            permanently_unavailable = session.exec(
                select(ResourceLeaseModel)
                .where(ResourceLeaseModel.resource_type == resource_type)
                .where(ResourceLeaseModel.resource_id == resource_id)
                .where(ResourceLeaseModel.status == "consumed")
            ).first()
            if permanently_unavailable:
                if resource_type == "mailbox":
                    raise ResourceLeaseConflict("MAILBOX_REUSED: mailbox already consumed")
                raise ResourceLeaseConflict(f"resource consumed: {resource_type}")
            cooling = session.exec(
                select(ResourceLeaseModel)
                .where(ResourceLeaseModel.resource_type == resource_type)
                .where(ResourceLeaseModel.resource_id == resource_id)
                .where(ResourceLeaseModel.cooldown_until > now)
                .order_by(ResourceLeaseModel.cooldown_until.desc())
            ).first()
            if cooling:
                raise ResourceLeaseConflict(f"resource cooling down: {resource_type}")
            expired = session.exec(
                select(ResourceLeaseModel)
                .where(ResourceLeaseModel.active_key == active_key)
                .where(ResourceLeaseModel.lease_until <= now)
            ).all()
            for item in expired:
                item.status = "expired"
                item.active_key = None
                item.released_at = now
                session.add(item)
            session.commit()

            existing = session.exec(
                select(ResourceLeaseModel).where(ResourceLeaseModel.active_key == active_key)
            ).first()
            if existing:
                if existing.owner_attempt_id == owner_attempt_id:
                    existing.heartbeat_at = now
                    existing.lease_until = now + timedelta(seconds=max(int(ttl_seconds), 1))
                    session.add(existing)
                    session.commit()
                    session.refresh(existing)
                    return existing
                raise ResourceLeaseConflict(f"resource already leased: {resource_type}")

            model = ResourceLeaseModel(
                id=uuid4().hex,
                resource_type=resource_type,
                resource_id=resource_id,
                active_key=active_key,
                owner_attempt_id=owner_attempt_id,
                status="reserved" if resource_type == "mailbox" else "active",
                lease_until=now + timedelta(seconds=max(int(ttl_seconds), 1)),
            )
            model.set_metadata(metadata or {})
            session.add(model)
            try:
                session.commit()
            except IntegrityError as exc:
                session.rollback()
                raise ResourceLeaseConflict(f"resource already leased: {resource_type}") from exc
            session.refresh(model)
            return model

    def mark_resource(
        self,
        *,
        resource_type: str,
        resource_id: str,
        status: str,
        cooldown_seconds: int = 0,
        metadata: dict[str, Any] | None = None,
    ) -> bool:
        now = _utcnow()
        with Session(engine) as session:
            model = session.exec(
                select(ResourceLeaseModel)
                .where(ResourceLeaseModel.resource_type == resource_type)
                .where(ResourceLeaseModel.resource_id == resource_id)
                .order_by(ResourceLeaseModel.leased_at.desc())
            ).first()
            if model is None:
                model = ResourceLeaseModel(
                    id=uuid4().hex,
                    resource_type=resource_type,
                    resource_id=resource_id,
                    owner_attempt_id="policy",
                    lease_until=now,
                )
            next_status = str(status or "released")
            model.status = next_status
            if next_status in LIVE_RESOURCE_STATES:
                model.active_key = f"{resource_type}:{resource_id}"
                model.released_at = None
                model.cooldown_until = None
                model.heartbeat_at = now
            else:
                model.active_key = None
                model.released_at = now
                requested_until = (
                    now + timedelta(seconds=max(int(cooldown_seconds), 0))
                    if cooldown_seconds > 0
                    else None
                )
                existing_until = model.cooldown_until
                if existing_until and existing_until.tzinfo is None:
                    existing_until = existing_until.replace(tzinfo=timezone.utc)
                model.cooldown_until = max(
                    [item for item in (existing_until, requested_until) if item],
                    default=None,
                )
            if metadata is not None:
                model.set_metadata(metadata)
            session.add(model)
            session.commit()
            return True

    def heartbeat(self, lease_id: str, *, ttl_seconds: int) -> bool:
        now = _utcnow()
        with Session(engine) as session:
            model = session.get(ResourceLeaseModel, lease_id)
            if model is None or model.status not in LIVE_RESOURCE_STATES:
                return False
            model.heartbeat_at = now
            model.lease_until = now + timedelta(seconds=max(int(ttl_seconds), 1))
            session.add(model)
            session.commit()
            return True

    def heartbeat_owner(self, owner_attempt_id: str, *, ttl_seconds: int) -> int:
        now = _utcnow()
        with Session(engine) as session:
            rows = session.exec(
                select(ResourceLeaseModel)
                .where(ResourceLeaseModel.owner_attempt_id == owner_attempt_id)
                .where(ResourceLeaseModel.status.in_(LIVE_RESOURCE_STATES))
            ).all()
            for model in rows:
                model.heartbeat_at = now
                model.lease_until = now + timedelta(seconds=max(int(ttl_seconds), 1))
                session.add(model)
            session.commit()
            return len(rows)

    def release(self, lease_id: str, *, status: str = "released", cooldown_seconds: int = 0) -> bool:
        now = _utcnow()
        with Session(engine) as session:
            model = session.get(ResourceLeaseModel, lease_id)
            if model is None:
                return False
            model.status = status
            model.active_key = None
            model.released_at = now
            if cooldown_seconds > 0:
                model.cooldown_until = now + timedelta(seconds=int(cooldown_seconds))
            session.add(model)
            session.commit()
            return True

    def release_owner(self, owner_attempt_id: str, *, status: str = "released") -> int:
        now = _utcnow()
        with Session(engine) as session:
            rows = session.exec(
                select(ResourceLeaseModel)
                .where(ResourceLeaseModel.owner_attempt_id == owner_attempt_id)
                .where(ResourceLeaseModel.status.in_(LIVE_RESOURCE_STATES))
            ).all()
            for model in rows:
                model.status = status
                model.active_key = None
                model.released_at = now
                session.add(model)
            session.commit()
            return len(rows)

    def reap_expired(self) -> int:
        now = _utcnow()
        with Session(engine) as session:
            rows = session.exec(
                select(ResourceLeaseModel)
                .where(ResourceLeaseModel.status.in_(LIVE_RESOURCE_STATES))
                .where(ResourceLeaseModel.lease_until <= now)
            ).all()
            for model in rows:
                model.status = "expired"
                model.active_key = None
                model.released_at = now
                session.add(model)
            session.commit()
            return len(rows)


class RegistrationResourceHealthRepository:
    def get(self, *, mode: str, egress_ref: str) -> dict[str, Any] | None:
        key = f"{mode}:{egress_ref}"
        with Session(engine) as session:
            model = session.get(RegistrationResourceHealthModel, key)
            if model is None:
                return None
            return {
                "resource_key": model.resource_key,
                "mode": model.mode,
                "egress_ref": model.egress_ref,
                "state": model.state,
                "healthy_concurrency": model.healthy_concurrency,
                "success_streak": model.success_streak,
                "failure_streak": model.failure_streak,
                "last_error_code": model.last_error_code,
                "cooldown_until": _iso(model.cooldown_until),
                "window": model.get_window(),
            }

    def put(
        self,
        *,
        mode: str,
        egress_ref: str,
        state: str,
        healthy_concurrency: int,
        success_streak: int,
        failure_streak: int,
        last_error_code: str,
        cooldown_until: datetime | None,
        window: list,
    ) -> None:
        key = f"{mode}:{egress_ref}"
        with Session(engine) as session:
            model = session.get(RegistrationResourceHealthModel, key) or RegistrationResourceHealthModel(
                resource_key=key,
                mode=mode,
                egress_ref=egress_ref,
            )
            model.state = state
            model.healthy_concurrency = max(int(healthy_concurrency), 1)
            model.success_streak = max(int(success_streak), 0)
            model.failure_streak = max(int(failure_streak), 0)
            model.last_error_code = str(last_error_code or "")
            model.cooldown_until = cooldown_until
            model.set_window(window[-20:])
            model.updated_at = _utcnow()
            session.add(model)
            session.commit()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class RegistrationArtifactRepository:
    _CONTENT_TYPES = {
        "screenshot": "image/png",
        "dom": "text/html",
        "diagnostic": "application/json",
    }

    def add_bundle(
        self,
        *,
        task_id: str,
        attempt_id: str,
        bundle: dict[str, Any],
    ) -> list[dict[str, Any]]:
        rows: list[RegistrationArtifactModel] = []
        for artifact_type in ("screenshot", "dom", "diagnostic"):
            raw_path = str(bundle.get(artifact_type) or "").strip()
            if not raw_path:
                continue
            path = Path(raw_path).resolve()
            if not path.is_file():
                continue
            rows.append(
                RegistrationArtifactModel(
                    id=uuid4().hex,
                    task_id=task_id,
                    attempt_id=attempt_id,
                    artifact_type=artifact_type,
                    path=str(path),
                    content_type=self._CONTENT_TYPES[artifact_type],
                    size_bytes=path.stat().st_size,
                    sha256=_file_sha256(path),
                    redacted=True,
                )
            )
        if not rows:
            return []
        with Session(engine) as session:
            session.add_all(rows)
            session.commit()
            for row in rows:
                session.refresh(row)
        return [self._serialize(row) for row in rows]

    def list_for_task(
        self,
        task_id: str,
        *,
        attempt_id: str = "",
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        query = select(RegistrationArtifactModel).where(
            RegistrationArtifactModel.task_id == task_id
        )
        if attempt_id:
            query = query.where(RegistrationArtifactModel.attempt_id == attempt_id)
        query = query.order_by(RegistrationArtifactModel.created_at.desc()).limit(
            max(1, min(int(limit), 1000))
        )
        with Session(engine) as session:
            return [self._serialize(row) for row in session.exec(query).all()]

    @staticmethod
    def _serialize(row: RegistrationArtifactModel) -> dict[str, Any]:
        return {
            "id": row.id,
            "task_id": row.task_id,
            "attempt_id": row.attempt_id,
            "artifact_type": row.artifact_type,
            "path": row.path,
            "content_type": row.content_type,
            "size_bytes": row.size_bytes,
            "sha256": row.sha256,
            "redacted": row.redacted,
            "created_at": _iso(row.created_at),
        }


registration_attempts = RegistrationAttemptRepository()
resource_leases = ResourceLeaseRepository()
registration_resource_health = RegistrationResourceHealthRepository()
registration_artifacts = RegistrationArtifactRepository()

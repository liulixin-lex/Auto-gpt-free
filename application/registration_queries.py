"""Read models for registration attempts, summaries and local capabilities."""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
import importlib.util
import os
from typing import Any

from sqlmodel import Session, func, select

from core.db import (
    RegistrationAttemptModel,
    ResourceLeaseModel,
    TaskEventModel,
    TaskModel,
    engine,
)
from domain.registration_runtime import RegistrationAttemptStatus, RegistrationMode, RegistrationStage
from infrastructure.registration_repository import LIVE_RESOURCE_STATES, registration_attempts
from services.registration_capacity import MODE_CAPACITY


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _percentile(values: list[int], percentile: float) -> int:
    if not values:
        return 0
    ordered = sorted(max(int(value), 0) for value in values)
    index = int(round((len(ordered) - 1) * min(max(percentile, 0.0), 1.0)))
    return ordered[index]


class RegistrationQueries:
    def list_attempts(self, task_id: str, **filters) -> dict[str, Any]:
        items = registration_attempts.list_for_task(task_id, **filters)
        return {"items": items, "total": len(items)}

    def summary(self, task_id: str) -> dict[str, Any]:
        items = registration_attempts.list_for_task(task_id, limit=1000)
        statuses = Counter(item["status"] for item in items)
        error_codes = Counter(item["error_code"] for item in items if item["error_code"])
        current_stages = Counter(item["current_stage"] for item in items if item["current_stage"])
        modes = Counter(item["effective_mode"] for item in items if item["effective_mode"])
        durations = [int(item["duration_ms"] or 0) for item in items if item["duration_ms"]]
        retry_count = sum(int(item["retry_count"] or 0) for item in items)
        retried_attempts = sum(1 for item in items if int(item["retry_count"] or 0) > 0)
        started_values = [_parse_datetime(item.get("started_at")) for item in items]
        finished_values = [_parse_datetime(item.get("finished_at")) for item in items]
        started = [value for value in started_values if value]
        finished = [value for value in finished_values if value]
        elapsed_seconds = 0.0
        if started:
            end = max(finished) if finished else datetime.now(timezone.utc)
            elapsed_seconds = max((end - min(started)).total_seconds(), 0.0)
        completed = statuses[RegistrationAttemptStatus.SUCCEEDED.value] + statuses[
            RegistrationAttemptStatus.FAILED.value
        ] + statuses[RegistrationAttemptStatus.TIMED_OUT.value] + statuses[
            RegistrationAttemptStatus.CANCELLED.value
        ]
        success = statuses[RegistrationAttemptStatus.SUCCEEDED.value]
        with Session(engine) as session:
            task = session.get(TaskModel, task_id)
            active_leases = session.exec(
                select(func.count())
                .select_from(ResourceLeaseModel)
                .join(
                    RegistrationAttemptModel,
                    ResourceLeaseModel.owner_attempt_id == RegistrationAttemptModel.attempt_id,
                )
                .where(RegistrationAttemptModel.task_id == task_id)
                .where(ResourceLeaseModel.status.in_(LIVE_RESOURCE_STATES))
            ).one()
            lease_rows = session.exec(
                select(ResourceLeaseModel.resource_type, func.count())
                .join(
                    RegistrationAttemptModel,
                    ResourceLeaseModel.owner_attempt_id == RegistrationAttemptModel.attempt_id,
                )
                .where(RegistrationAttemptModel.task_id == task_id)
                .where(ResourceLeaseModel.status.in_(LIVE_RESOURCE_STATES))
                .group_by(ResourceLeaseModel.resource_type)
            ).all()
            stage_rows = session.exec(
                select(TaskEventModel.stage, TaskEventModel.attempt_id)
                .where(TaskEventModel.task_id == task_id)
                .where(TaskEventModel.attempt_id != "")
                .where(TaskEventModel.stage != "")
            ).all()
        if task and task.started_at:
            task_started = task.started_at
            task_end = task.finished_at or datetime.now(timezone.utc)
            if task_started.tzinfo is None:
                task_started = task_started.replace(tzinfo=timezone.utc)
            if task_end.tzinfo is None:
                task_end = task_end.replace(tzinfo=timezone.utc)
            elapsed_seconds = max((task_end - task_started).total_seconds(), 0.0)
        reached: dict[str, set[str]] = {stage.value: set() for stage in RegistrationStage}
        for stage_name, attempt_id in stage_rows:
            if stage_name in reached and attempt_id:
                reached[stage_name].add(attempt_id)
        stage_funnel = {
            stage.value: max(len(reached[stage.value]), current_stages.get(stage.value, 0))
            for stage in RegistrationStage
        }
        payload = task.get_payload() if task else {}
        task_result = task.get_result() if task else {}
        result_data = task_result.get("data") if isinstance(task_result.get("data"), dict) else {}
        effective_concurrency = int(
            result_data.get("effective_concurrency")
            or payload.get("effective_concurrency")
            or 1
        )
        return {
            "task_id": task_id,
            "total": len(items),
            "completed": completed,
            "success": success,
            "failed": statuses[RegistrationAttemptStatus.FAILED.value],
            "timed_out": statuses[RegistrationAttemptStatus.TIMED_OUT.value],
            "cancelled": statuses[RegistrationAttemptStatus.CANCELLED.value],
            "running": statuses[RegistrationAttemptStatus.RUNNING.value],
            "queued": statuses[RegistrationAttemptStatus.QUEUED.value],
            "success_rate": (success / completed) if completed else 0.0,
            "retry_count": retry_count,
            "retry_rate": (retried_attempts / completed) if completed else 0.0,
            "timeout_rate": (
                statuses[RegistrationAttemptStatus.TIMED_OUT.value] / completed
                if completed
                else 0.0
            ),
            "p50_duration_ms": _percentile(durations, 0.50),
            "p95_duration_ms": _percentile(durations, 0.95),
            "throughput_per_minute": (completed * 60.0 / elapsed_seconds) if elapsed_seconds > 0 else 0.0,
            "error_codes": dict(error_codes.most_common()),
            "stages": stage_funnel,
            "current_stages": dict(current_stages),
            "modes": dict(modes),
            "active_leases": int(active_leases or 0),
            "resource_status": {
                str(resource_type): int(count or 0)
                for resource_type, count in lease_rows
            },
            "requested_concurrency": int(
                result_data.get("requested_concurrency")
                or payload.get("requested_concurrency")
                or payload.get("concurrency")
                or 1
            ),
            "effective_concurrency": effective_concurrency,
            "current_concurrency": max(int(result_data.get("active_concurrency") or 0), 0),
            "configured_concurrency_limit": int(
                result_data.get("current_concurrency_limit") or effective_concurrency
            ),
            "peak_active_concurrency": int(result_data.get("peak_active_concurrency") or 0),
            "healthy_concurrency": int(result_data.get("healthy_concurrency") or 1),
            "limiting_resource": str(result_data.get("limiting_resource") or ""),
            "egress_state": str(result_data.get("egress_state") or "closed"),
            "cooldown_seconds": int(result_data.get("cooldown_seconds") or 0),
            "replacement_count": int(result_data.get("replacement_count") or 0),
            "elapsed_seconds": elapsed_seconds,
            "top_error_code": (
                error_codes.most_common(1)[0][0]
                if error_codes and (statuses[RegistrationAttemptStatus.FAILED.value] or statuses[RegistrationAttemptStatus.TIMED_OUT.value])
                else ""
            ),
        }


class RegistrationCapabilitiesService:
    def inspect(self, *, run_checks: bool = False) -> dict[str, Any]:
        camoufox_available = importlib.util.find_spec("camoufox") is not None
        protocol_available = importlib.util.find_spec("curl_cffi") is not None
        from platforms.chatgpt.protocol.sdk import SentinelSdkResolver

        sentinel_drift = SentinelSdkResolver.drift_status()
        windows = os.name == "nt"
        display_available = windows or bool(os.getenv("DISPLAY"))
        items = []
        for mode in RegistrationMode:
            if mode == RegistrationMode.PROTOCOL:
                healthy = protocol_available
                dependency = "curl_cffi"
            else:
                healthy = camoufox_available and (mode == RegistrationMode.HEADLESS or display_available)
                dependency = "camoufox"
            status = "healthy" if healthy else "unavailable"
            if mode == RegistrationMode.PROTOCOL and healthy and sentinel_drift["open"]:
                status = "degraded"
            if healthy and run_checks and mode != RegistrationMode.PROTOCOL and not display_available:
                status = "degraded"
            config = MODE_CAPACITY[mode]
            items.append(
                {
                    "mode": mode.value,
                    "status": status,
                    "dependency": dependency,
                    "default_concurrency": config.default,
                    "maximum_concurrency": config.maximum,
                    "direct_maximum": config.direct_maximum,
                    "strict_mode": True,
                    "sentinel_sdk_drift": (
                        sentinel_drift if mode == RegistrationMode.PROTOCOL else None
                    ),
                }
            )
        return {
            "items": items,
            "automatic_probe": False,
            "browser_engine": "camoufox",
            "run_checks": bool(run_checks),
        }


registration_queries = RegistrationQueries()
registration_capabilities = RegistrationCapabilitiesService()

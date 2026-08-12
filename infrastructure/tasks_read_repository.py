from __future__ import annotations

from application.tasks import get_task, list_task_events
from core.datetime_utils import ensure_utc_datetime
from domain.tasks import TaskEvent, TaskProgress, TaskSummary


def _to_task_summary(data: dict) -> TaskSummary:
    progress_raw = data.get("progress_detail") or {}
    return TaskSummary(
        id=data["id"],
        type=data.get("type", ""),
        platform=data.get("platform", ""),
        status=data.get("status", ""),
        progress=TaskProgress(
            current=int(progress_raw.get("current", 0) or 0),
            total=int(progress_raw.get("total", 0) or 0),
            label=str(progress_raw.get("label", data.get("progress", "0/0"))),
        ),
        success=int(data.get("success", 0) or 0),
        error_count=int(data.get("error_count", 0) or 0),
        errors=list(data.get("errors", [])),
        cashier_urls=list(data.get("cashier_urls", [])),
        error=str(data.get("error", "")),
        created_at=ensure_utc_datetime(data.get("created_at")),
        started_at=ensure_utc_datetime(data.get("started_at")),
        finished_at=ensure_utc_datetime(data.get("finished_at")),
        updated_at=ensure_utc_datetime(data.get("updated_at")),
        result=dict(data.get("result", {}) or {}),
    )


def _to_event(data: dict) -> TaskEvent:
    return TaskEvent(
        id=int(data["id"]),
        task_id=data["task_id"],
        type=data.get("type", ""),
        level=data.get("level", "info"),
        message=data.get("message", ""),
        line=data.get("line", ""),
        kind=data.get("kind", data.get("type", "log")),
        attempt_id=data.get("attempt_id", ""),
        seq=int(data.get("seq", 0) or 0),
        stage=data.get("stage", ""),
        action=data.get("action", ""),
        event_code=data.get("event_code", ""),
        error_code=data.get("error_code", ""),
        retryable=bool(data.get("retryable", False)),
        retry_index=int(data.get("retry_index", 0) or 0),
        duration_ms=int(data.get("duration_ms", 0) or 0),
        schema_version=int(data.get("schema_version", 1) or 1),
        detail=dict(data.get("detail", {}) or {}),
        created_at=ensure_utc_datetime(data.get("created_at")),
    )


class TasksReadRepository:
    def get(self, task_id: str) -> TaskSummary | None:
        data = get_task(task_id)
        return _to_task_summary(data) if data else None

    def list_events(
        self,
        task_id: str,
        *,
        since: int = 0,
        limit: int = 200,
        tail: bool = False,
        attempt_id: str = "",
        stage: str = "",
        level: str = "",
        error_code: str = "",
    ) -> list[TaskEvent]:
        return [
            _to_event(item)
            for item in list_task_events(
                task_id,
                since=since,
                limit=limit,
                tail=tail,
                attempt_id=attempt_id,
                stage=stage,
                level=level,
                error_code=error_code,
            )
        ]

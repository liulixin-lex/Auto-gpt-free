from __future__ import annotations

from datetime import datetime, timezone

from core.datetime_utils import serialize_datetime
from infrastructure.tasks_read_repository import TasksReadRepository


class TasksQueryService:
    def __init__(self, repository: TasksReadRepository | None = None):
        self.repository = repository or TasksReadRepository()

    def get_task(self, task_id: str) -> dict | None:
        item = self.repository.get(task_id)
        if not item:
            return None
        return self._serialize(item)

    def list_events(
        self,
        task_id: str,
        *,
        since: int = 0,
        limit: int = 200,
        attempt_id: str = "",
        stage: str = "",
        level: str = "",
        error_code: str = "",
    ) -> dict:
        # Initial open (since=0): newest window only — do not dump entire history.
        items = self.repository.list_events(
            task_id,
            since=since,
            limit=limit,
            tail=(since <= 0),
            attempt_id=attempt_id,
            stage=stage,
            level=level,
            error_code=error_code,
        )
        return {
            "items": [
                {
                    "id": item.id,
                    "task_id": item.task_id,
                    "type": item.type,
                    "kind": item.kind,
                    "level": item.level,
                    "message": item.message,
                    "line": item.line,
                    "attempt_id": item.attempt_id,
                    "seq": item.seq,
                    "stage": item.stage,
                    "action": item.action,
                    "event_code": item.event_code,
                    "error_code": item.error_code,
                    "retryable": item.retryable,
                    "retry_index": item.retry_index,
                    "duration_ms": item.duration_ms,
                    "schema_version": item.schema_version,
                    "detail": item.detail,
                    "created_at": serialize_datetime(item.created_at),
                }
                for item in items
            ]
        }

    @staticmethod
    def _serialize(item) -> dict:
        result_data = (
            item.result.get("data")
            if isinstance(item.result, dict) and isinstance(item.result.get("data"), dict)
            else {}
        )
        error_classes = dict(result_data.get("error_classes") or {})
        elapsed_seconds = 0.0
        if item.started_at:
            started_at = item.started_at
            finished_at = item.finished_at or datetime.now(timezone.utc)
            if started_at.tzinfo is None:
                started_at = started_at.replace(tzinfo=timezone.utc)
            if finished_at.tzinfo is None:
                finished_at = finished_at.replace(tzinfo=timezone.utc)
            elapsed_seconds = max((finished_at - started_at).total_seconds(), 0.0)
        return {
            "id": item.id,
            "task_id": item.id,
            "type": item.type,
            "platform": item.platform,
            "status": item.status,
            "terminal": item.status in {
                "succeeded",
                "partial",
                "failed",
                "interrupted",
                "cancelled",
                "timed_out",
            },
            "cancel_requested": item.status == "cancel_requested",
            "cancellable": item.status in {"pending", "claimed", "running"},
            "progress": item.progress.label,
            "progress_detail": {
                "current": item.progress.current,
                "total": item.progress.total,
                "label": item.progress.label,
            },
            "success": item.success,
            "error_count": item.error_count,
            "errors": item.errors,
            "cashier_urls": item.cashier_urls,
            "error": item.error,
            "created_at": serialize_datetime(item.created_at),
            "started_at": serialize_datetime(item.started_at),
            "finished_at": serialize_datetime(item.finished_at),
            "updated_at": serialize_datetime(item.updated_at),
            "result": item.result,
            "requested_mode": str(result_data.get("requested_mode") or ""),
            "effective_mode": str(result_data.get("effective_mode") or ""),
            "requested_concurrency": int(result_data.get("requested_concurrency") or 1),
            "effective_concurrency": int(result_data.get("effective_concurrency") or 1),
            "current_concurrency": max(int(result_data.get("active_concurrency") or 0), 0),
            "configured_concurrency_limit": int(
                result_data.get("current_concurrency_limit")
                or result_data.get("effective_concurrency")
                or 1
            ),
            "peak_active_concurrency": int(result_data.get("peak_active_concurrency") or 0),
            "healthy_concurrency": int(result_data.get("healthy_concurrency") or 1),
            "limiting_resource": str(result_data.get("limiting_resource") or ""),
            "egress_state": str(result_data.get("egress_state") or "closed"),
            "cooldown_seconds": int(result_data.get("cooldown_seconds") or 0),
            "replacement_count": int(result_data.get("replacement_count") or 0),
            "elapsed_seconds": elapsed_seconds,
            "throughput_per_minute": (
                item.progress.current * 60.0 / elapsed_seconds
                if elapsed_seconds > 0
                else 0.0
            ),
            "top_error_code": max(
                error_classes,
                key=lambda key: int(error_classes.get(key) or 0),
                default="",
            ) if item.error_count > 0 else "",
        }

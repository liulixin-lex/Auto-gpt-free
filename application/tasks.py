"""Task orchestration and persistence helpers."""
from __future__ import annotations

import json
import sys
import threading
import time
import uuid
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from sqlmodel import Session, select

from core.account_graph import (
    load_account_graphs,
    patch_account_graph,
    recover_lifecycle_status_for_valid_account,
)
from core.base_platform import AccountStatus, RegisterConfig
from core.datetime_utils import format_local_clock, serialize_datetime
from core.db import AccountModel, TaskEventModel, TaskModel, engine, save_account
from core.platform_accounts import build_platform_account
from core.registry import get
from domain.registration_runtime import (
    AttemptContext,
    RegistrationAttemptStatus,
    RegistrationErrorCode,
    RegistrationEventKind,
    RegistrationMode,
    RegistrationStage,
    classify_registration_error,
    redact_registration_data,
    redact_registration_text,
    stable_resource_ref,
)
from services.registration_capacity import CapacityTimeout, MODE_CAPACITY, registration_capacity
from services.registration_process import (
    BrowserProcessRequest,
    BrowserWorkerCancelled,
    BrowserWorkerError,
    BrowserWorkerTimeout,
    browser_process_supervisor,
)
from services.task_event_writer import task_event_writer
from infrastructure.registration_repository import (
    ResourceLeaseConflict,
    registration_attempts,
    resource_leases,
)

TASK_TYPE_REGISTER = "register"
TASK_TYPE_ACCOUNT_CHECK_ALL = "account_check_all"

TASK_STATUS_PENDING = "pending"
TASK_STATUS_CLAIMED = "claimed"
TASK_STATUS_RUNNING = "running"
TASK_STATUS_SUCCEEDED = "succeeded"
TASK_STATUS_PARTIAL = "partial"
TASK_STATUS_FAILED = "failed"
TASK_STATUS_INTERRUPTED = "interrupted"
TASK_STATUS_CANCEL_REQUESTED = "cancel_requested"
TASK_STATUS_CANCELLED = "cancelled"

# UI + backend contract for register worker pool size.
MAX_REGISTER_CONCURRENCY = 20
# Fail-fast: one mailbox/account must not occupy a worker forever.
DEFAULT_REGISTER_ACCOUNT_TIMEOUT_SECONDS = 180
MIN_REGISTER_ACCOUNT_TIMEOUT_SECONDS = 45
MAX_REGISTER_ACCOUNT_TIMEOUT_SECONDS = 600

TERMINAL_TASK_STATUSES = {
    TASK_STATUS_SUCCEEDED,
    TASK_STATUS_PARTIAL,
    TASK_STATUS_FAILED,
    TASK_STATUS_INTERRUPTED,
    TASK_STATUS_CANCELLED,
}
ACTIVE_TASK_STATUSES = {
    TASK_STATUS_CLAIMED,
    TASK_STATUS_RUNNING,
    TASK_STATUS_CANCEL_REQUESTED,
}

_task_locks: dict[str, threading.Lock] = {}
_task_locks_guard = threading.Lock()
_task_cancel_events: dict[str, threading.Event] = {}
_task_cancel_events_guard = threading.Lock()


def _task_cancel_event(task_id: str, *, create: bool = True) -> threading.Event | None:
    """Return the in-process cancellation signal for a task.

    The database remains the source of truth, but polling SQLite for every
    browser/protocol checkpoint amplified contention exactly when a user tried
    to stop a high-concurrency task.  This event lets the API wake all live
    workers immediately; ``TaskLogger.is_cancel_requested`` still falls back to
    the database for cross-process/service-restart compatibility.
    """
    normalized = str(task_id or "").strip()
    if not normalized:
        return None
    with _task_cancel_events_guard:
        event = _task_cancel_events.get(normalized)
        if event is None and create:
            event = threading.Event()
            _task_cancel_events[normalized] = event
        return event


def _forget_task_cancel_event(task_id: str) -> None:
    normalized = str(task_id or "").strip()
    if not normalized:
        return
    with _task_cancel_events_guard:
        _task_cancel_events.pop(normalized, None)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _utcnow_iso() -> str:
    return _utcnow().isoformat().replace("+00:00", "Z")


def _serialize_datetime(value: datetime | None) -> str | None:
    return serialize_datetime(value)


def _json_default(value: Any) -> Any:
    if isinstance(value, datetime):
        return _serialize_datetime(value)
    raise TypeError(f"Object of type {value.__class__.__name__} is not JSON serializable")


def _dump_json(data: Any) -> str:
    return json.dumps(data or {}, ensure_ascii=False, default=_json_default)


def _task_lock(task_id: str) -> threading.Lock:
    with _task_locks_guard:
        lock = _task_locks.get(task_id)
        if lock is None:
            lock = threading.Lock()
            _task_locks[task_id] = lock
        return lock


def _mutate_task(task_id: str, fn: Callable[[TaskModel], None]) -> Optional[TaskModel]:
    with _task_lock(task_id):
        with Session(engine) as session:
            task = session.get(TaskModel, task_id)
            if not task:
                return None
            fn(task)
            task.updated_at = _utcnow()
            session.add(task)
            session.commit()
            session.refresh(task)
            return task


def _task_result_seed(result: dict[str, Any] | None = None) -> dict[str, Any]:
    base = {"errors": [], "cashier_urls": [], "data": None}
    if result:
        base.update(result)
    return base


def _task_account_keys(task_type: str, payload: dict[str, Any]) -> list[str]:
    return []


def serialize_task(task: TaskModel) -> dict[str, Any]:
    result = task.get_result()
    payload = task.get_payload()
    result_data = result.get("data") if isinstance(result.get("data"), dict) else {}
    progress_total = int(task.progress_total or 0)
    progress_current = int(task.progress_current or 0)
    elapsed_seconds = 0.0
    if task.started_at:
        started_at = task.started_at
        end = task.finished_at or _utcnow()
        if started_at.tzinfo is None:
            started_at = started_at.replace(tzinfo=timezone.utc)
        if end.tzinfo is None:
            end = end.replace(tzinfo=timezone.utc)
        elapsed_seconds = max((end - started_at).total_seconds(), 0.0)
    return {
        "id": task.id,
        "task_id": task.id,
        "type": task.type,
        "platform": task.platform,
        "status": task.status,
        "terminal": task.status in TERMINAL_TASK_STATUSES,
        "cancellable": task.status in {TASK_STATUS_PENDING, TASK_STATUS_CLAIMED, TASK_STATUS_RUNNING},
        "progress": f"{progress_current}/{progress_total}" if progress_total else "0/0",
        "progress_detail": {
            "current": progress_current,
            "total": progress_total,
            "label": f"{progress_current}/{progress_total}" if progress_total else "0/0",
        },
        "success": int(task.success_count or 0),
        "error_count": int(task.error_count or 0),
        "errors": list(result.get("errors", [])),
        "cashier_urls": list(result.get("cashier_urls", [])),
        "data": result.get("data"),
        "result": result,
        "error": task.error,
        "created_at": _serialize_datetime(task.created_at),
        "started_at": _serialize_datetime(task.started_at),
        "finished_at": _serialize_datetime(task.finished_at),
        "updated_at": _serialize_datetime(task.updated_at),
        "requested_mode": str(
            result_data.get("requested_mode")
            or payload.get("executor_type")
            or ""
        ),
        "effective_mode": str(
            result_data.get("effective_mode")
            or payload.get("executor_type")
            or ""
        ),
        "requested_concurrency": int(
            result_data.get("requested_concurrency")
            or payload.get("requested_concurrency")
            or payload.get("concurrency")
            or 1
        ),
        "effective_concurrency": int(
            result_data.get("effective_concurrency")
            or payload.get("effective_concurrency")
            or 1
        ),
        "current_concurrency": max(int(result_data.get("active_concurrency") or 0), 0),
        "configured_concurrency_limit": int(
            result_data.get("current_concurrency_limit")
            or result_data.get("effective_concurrency")
            or payload.get("effective_concurrency")
            or 1
        ),
        "peak_active_concurrency": int(result_data.get("peak_active_concurrency") or 0),
        "healthy_concurrency": int(result_data.get("healthy_concurrency") or 1),
        "limiting_resource": str(result_data.get("limiting_resource") or ""),
        "egress_state": str(result_data.get("egress_state") or "closed"),
        "cooldown_seconds": int(result_data.get("cooldown_seconds") or 0),
        "replacement_count": int(result_data.get("replacement_count") or 0),
        "top_error_code": (
            ""
            if int(result_data.get("fail") or 0) <= 0
            else max(
                dict(result_data.get("error_classes") or {}),
                key=lambda key: int(dict(result_data.get("error_classes") or {}).get(key) or 0),
                default="",
            )
        ),
        "elapsed_seconds": elapsed_seconds,
        "throughput_per_minute": (
            progress_current * 60.0 / elapsed_seconds if elapsed_seconds > 0 else 0.0
        ),
    }


def serialize_event(event: TaskEventModel) -> dict[str, Any]:
    return {
        "id": event.id,
        "task_id": event.task_id,
        "type": event.type,
        "kind": event.kind or event.type,
        "level": event.level,
        "message": event.message,
        "line": f"[{format_local_clock(event.created_at)}] {event.message}",
        "attempt_id": event.attempt_id,
        "seq": event.seq,
        "stage": event.stage,
        "action": event.action,
        "event_code": event.event_code,
        "error_code": event.error_code,
        "retryable": event.retryable,
        "retry_index": event.retry_index,
        "duration_ms": event.duration_ms,
        "schema_version": event.schema_version,
        "detail": event.get_detail(),
        "created_at": _serialize_datetime(event.created_at),
    }


def create_task(
    *,
    task_type: str,
    platform: str,
    payload: dict[str, Any],
    progress_total: int = 1,
    result_seed: dict[str, Any] | None = None,
) -> dict[str, Any]:
    task_id = f"task_{int(time.time() * 1000)}_{uuid.uuid4().hex[:6]}"
    task = TaskModel(
        id=task_id,
        type=task_type,
        platform=platform,
        status=TASK_STATUS_PENDING,
        payload_json=_dump_json(payload),
        result_json=_dump_json(_task_result_seed(result_seed)),
        progress_current=0,
        progress_total=max(int(progress_total or 0), 0),
    )
    with Session(engine) as session:
        session.add(task)
        session.commit()
        session.refresh(task)
    append_task_event(task.id, f"任务已创建: {task_type}", event_type="state")
    return serialize_task(task)


def create_register_task(payload: dict[str, Any]) -> dict[str, Any]:
    count = max(int(payload.get("count", 1) or 1), 1)
    payload = {**payload, "platform": "chatgpt"}
    return create_task(
        task_type=TASK_TYPE_REGISTER,
        platform="chatgpt",
        payload=payload,
        progress_total=count,
    )


def create_account_check_all_task(
    platform: str = "",
    *,
    ids: list[int] | None = None,
    select_all: bool = False,
) -> dict[str, Any]:
    """Create a batch validity/credits check task.

    - ``ids``: check only these account ids (preferred when UI multi-selects)
    - ``select_all``: check every account on the platform (no fixed 50 cap)
    - both empty: check all accounts on the platform (same as select_all)
    """
    clean_ids = sorted({int(x) for x in (ids or []) if int(x) > 0})
    check_all = bool(select_all) or not clean_ids
    progress_total = max(len(clean_ids), 1) if not check_all else 1
    return create_task(
        task_type=TASK_TYPE_ACCOUNT_CHECK_ALL,
        platform=platform,
        payload={
            "platform": platform,
            "ids": clean_ids,
            "select_all": check_all,
        },
        progress_total=progress_total,
    )


def create_account_check_one_task(account_id: int) -> dict[str, Any]:
    return create_task(
        task_type=TASK_TYPE_ACCOUNT_CHECK_ALL,
        platform="",
        payload={"ids": [int(account_id)], "select_all": False},
        progress_total=1,
    )


def get_task(task_id: str) -> Optional[dict[str, Any]]:
    with Session(engine) as session:
        task = session.get(TaskModel, task_id)
        return serialize_task(task) if task else None


# Rolling log retention — prevent unbounded task_events growth.
TASK_EVENT_MAX_PER_TASK = 800
TASK_EVENT_MAX_REGISTER = 1200
TASK_EVENT_GLOBAL_MAX = 20000
TASK_EVENT_TERMINAL_KEEP = 120
TASK_EVENT_PRUNE_EVERY = 25
_prune_counters: dict[str, int] = {}
_prune_lock = threading.Lock()
_event_sequences: dict[tuple[str, str], int] = {}
_event_sequence_lock = threading.Lock()


def _task_event_cap(task_type: str | None = None) -> int:
    typ = str(task_type or "").strip().lower()
    if typ == TASK_TYPE_REGISTER:
        return TASK_EVENT_MAX_REGISTER
    return TASK_EVENT_MAX_PER_TASK


def prune_task_events(
    task_id: str,
    *,
    keep: int | None = None,
    task_type: str | None = None,
) -> int:
    """Delete oldest events for a task beyond ``keep``. Returns deleted count."""
    if not task_id:
        return 0
    if keep is None:
        if task_type is None:
            with Session(engine) as session:
                task = session.get(TaskModel, task_id)
                task_type = task.type if task else ""
        keep = _task_event_cap(task_type)
    keep = max(int(keep), 50)
    with Session(engine) as session:
        from sqlalchemy import delete as sa_delete

        # Newest keep ids: order desc, take keep, then delete id < min(kept).
        newest_ids = session.exec(
            select(TaskEventModel.id)
            .where(TaskEventModel.task_id == task_id)
            .order_by(TaskEventModel.id.desc())
            .limit(keep)
        ).all()
        if not newest_ids:
            return 0
        # If fewer than keep, nothing to prune — but still check if more exist.
        floor_id = min(int(x) for x in newest_ids if x is not None)
        # Count how many would be deleted.
        from sqlalchemy import func

        excess = session.exec(
            select(func.count())
            .select_from(TaskEventModel)
            .where(TaskEventModel.task_id == task_id)
            .where(TaskEventModel.id < floor_id)
        ).one()
        try:
            excess_n = int(excess or 0)
        except Exception:
            excess_n = 0
        if excess_n <= 0:
            return 0
        session.execute(
            sa_delete(TaskEventModel).where(
                TaskEventModel.task_id == task_id,
                TaskEventModel.id < floor_id,
            )
        )
        session.commit()
        return excess_n


def prune_all_task_events(*, global_max: int = TASK_EVENT_GLOBAL_MAX) -> dict[str, int]:
    """Housekeeping: per-task caps + terminal task shrink + global ceiling."""
    stats = {"tasks_pruned": 0, "events_deleted": 0, "global_deleted": 0}
    with Session(engine) as session:
        tasks = session.exec(select(TaskModel)).all()
        task_rows = [(t.id, t.type, t.status) for t in tasks]
    for tid, ttype, tstatus in task_rows:
        if not tid:
            continue
        keep = (
            TASK_EVENT_TERMINAL_KEEP
            if str(tstatus) in TERMINAL_TASK_STATUSES
            else _task_event_cap(str(ttype or ""))
        )
        deleted = prune_task_events(str(tid), keep=keep, task_type=str(ttype or ""))
        if deleted:
            stats["tasks_pruned"] += 1
            stats["events_deleted"] += deleted

    # Global ceiling: drop oldest events across all tasks if still over budget.
    global_max = max(int(global_max), 1000)
    with Session(engine) as session:
        from sqlalchemy import func, delete as sa_delete

        total = session.exec(select(func.count()).select_from(TaskEventModel)).one()
        try:
            total_n = int(total or 0)
        except Exception:
            total_n = 0
        if total_n > global_max:
            excess = total_n - global_max
            old_ids = session.exec(
                select(TaskEventModel.id).order_by(TaskEventModel.id.asc()).limit(excess)
            ).all()
            ids = [int(x) for x in old_ids if x is not None]
            if ids:
                session.execute(sa_delete(TaskEventModel).where(TaskEventModel.id.in_(ids)))
                session.commit()
                stats["global_deleted"] = len(ids)
    return stats


def list_task_events(
    task_id: str,
    *,
    since: int = 0,
    limit: int = 200,
    tail: bool = False,
    attempt_id: str = "",
    stage: str = "",
    level: str = "",
    error_code: str = "",
) -> list[dict[str, Any]]:
    """List events after ``since``. When ``since==0`` and ``tail``, return newest window only."""
    limit = min(max(limit, 1), 500)
    with Session(engine) as session:
        query = select(TaskEventModel).where(TaskEventModel.task_id == task_id)
        if attempt_id:
            query = query.where(TaskEventModel.attempt_id == attempt_id)
        if stage:
            query = query.where(TaskEventModel.stage == stage)
        if level:
            query = query.where(TaskEventModel.level == level)
        if error_code:
            query = query.where(TaskEventModel.error_code == error_code)
        if since <= 0 and tail:
            # Newest ``limit`` rows, then chronological for UI.
            newest = session.exec(
                query.order_by(TaskEventModel.id.desc()).limit(limit)
            ).all()
            items = list(reversed(newest))
        else:
            q = query.where(TaskEventModel.id > since).order_by(TaskEventModel.id).limit(limit)
            items = list(session.exec(q).all())
    return [serialize_event(item) for item in items]


def list_tasks(*, limit: int = 50, types: list[str] | None = None) -> list[dict[str, Any]]:
    """Newest tasks first (for Jobs UI / multi-stream logs)."""
    limit = min(max(int(limit or 50), 1), 200)
    with Session(engine) as session:
        q = select(TaskModel).order_by(TaskModel.created_at.desc()).limit(limit)
        if types:
            q = (
                select(TaskModel)
                .where(TaskModel.type.in_(list(types)))
                .order_by(TaskModel.created_at.desc())
                .limit(limit)
            )
        items = session.exec(q).all()
    return [serialize_task(item) for item in items]


def _write_task_event_batch(payloads: list[dict[str, Any]]) -> list[dict[str, Any]]:
    with Session(engine) as session:
        events: list[TaskEventModel] = []
        for payload in payloads:
            event = TaskEventModel(**payload)
            session.add(event)
            events.append(event)
        session.commit()
        for event in events:
            session.refresh(event)
        return [serialize_event(event) for event in events]


def append_task_event(task_id: str, message: str, *, event_type: str = "log", level: str = "info", detail: dict | None = None) -> dict[str, Any]:
    normalized_detail = dict(redact_registration_data(detail or {}))
    attempt_id = str(normalized_detail.get("attempt_id") or "")
    if attempt_id:
        sequence_key = (task_id, attempt_id)
        with _event_sequence_lock:
            seq = _event_sequences.get(sequence_key, 0) + 1
            _event_sequences[sequence_key] = seq
    else:
        seq = 0
    kind = str(normalized_detail.get("kind") or event_type or "log")
    payload = {
        "task_id": task_id,
        "type": event_type,
        "level": level,
        "message": redact_registration_text(message),
        "detail_json": _dump_json(normalized_detail),
        "attempt_id": attempt_id,
        "seq": seq,
        "kind": kind,
        "stage": str(normalized_detail.get("stage") or ""),
        "action": str(normalized_detail.get("action") or ""),
        "event_code": str(normalized_detail.get("event_code") or ""),
        "error_code": str(normalized_detail.get("error_code") or ""),
        "retryable": bool(normalized_detail.get("retryable", False)),
        "retry_index": int(normalized_detail.get("retry_index") or 0),
        "duration_ms": int(normalized_detail.get("duration_ms") or 0),
        "schema_version": int(normalized_detail.get("schema_version") or (2 if attempt_id else 1)),
    }
    serialized = task_event_writer.submit(payload, _write_task_event_batch)

    # Rolling prune: every N appends, drop oldest beyond cap (cheap amortised cost).
    should_prune = False
    with _prune_lock:
        n = _prune_counters.get(task_id, 0) + 1
        _prune_counters[task_id] = n
        if n >= TASK_EVENT_PRUNE_EVERY:
            _prune_counters[task_id] = 0
            should_prune = True
    if should_prune:
        try:
            prune_task_events(task_id)
        except Exception:
            pass
    return serialized


def mark_incomplete_tasks_interrupted() -> None:
    interrupted_ids: list[str] = []
    with Session(engine) as session:
        non_terminal = [TASK_STATUS_PENDING] + list(ACTIVE_TASK_STATUSES)
        tasks = session.exec(
            select(TaskModel).where(TaskModel.status.in_(non_terminal))
        ).all()
        for task in tasks:
            task.status = TASK_STATUS_INTERRUPTED
            task.error = task.error or "任务在服务重启后被中断"
            task.finished_at = _utcnow()
            task.updated_at = _utcnow()
            session.add(task)
            interrupted_ids.append(task.id)
        session.commit()
    for task_id in interrupted_ids:
        append_task_event(
            task_id,
            "任务在服务重启后被标记为中断",
            event_type="state",
            level="warning",
        )


def request_cancel(task_id: str) -> Optional[dict[str, Any]]:
    task = _mutate_task(
        task_id,
        lambda model: _request_cancel_mutation(model),
    )
    if not task:
        return None
    if task.status == TASK_STATUS_CANCEL_REQUESTED:
        event = _task_cancel_event(task_id)
        if event is not None:
            event.set()
        append_task_event(task_id, "已请求取消任务", event_type="state", level="warning")
    elif task.status == TASK_STATUS_CANCELLED:
        # A pending task is cancelled before a worker claims it.  There is no
        # live worker to wake, but keep a single explicit state event for SSE.
        append_task_event(task_id, "任务已在启动前取消", event_type="state", level="warning")
    return serialize_task(task)


def _request_cancel_mutation(task: TaskModel) -> None:
    if task.status in TERMINAL_TASK_STATUSES:
        return
    if task.status == TASK_STATUS_PENDING:
        task.status = TASK_STATUS_CANCELLED
        task.finished_at = _utcnow()
        task.error = task.error or "任务在开始前被取消"
    else:
        task.status = TASK_STATUS_CANCEL_REQUESTED


def claim_next_runnable_task(
    *,
    running_platform_counts: dict[str, int] | None = None,
    busy_account_keys: set[str] | None = None,
    max_parallel_per_platform: int = 1,
) -> Optional[dict[str, Any]]:
    running_platform_counts = dict(running_platform_counts or {})
    busy_account_keys = set(busy_account_keys or set())
    with Session(engine) as session:
        tasks = session.exec(
            select(TaskModel)
            .where(TaskModel.status == TASK_STATUS_PENDING)
            .order_by(TaskModel.created_at)
        ).all()
        for task in tasks:
            payload = task.get_payload()
            platform = task.platform or str(payload.get("platform", "") or "")
            account_keys = _task_account_keys(task.type, payload)
            if platform and running_platform_counts.get(platform, 0) >= max_parallel_per_platform:
                continue
            if account_keys and busy_account_keys.intersection(account_keys):
                continue
            task.status = TASK_STATUS_CLAIMED
            task.started_at = task.started_at or _utcnow()
            task.updated_at = _utcnow()
            session.add(task)
            session.commit()
            return {"id": task.id, "platform": platform, "account_keys": account_keys}
    return None


class TaskLogger:
    def __init__(self, task_id: str):
        self.task_id = task_id
        self._cancel_event = _task_cancel_event(task_id)
        # 并发任务里每个 worker 通过 ``set_subtask`` 把自己的 subtask_id
        # 绑到 thread-local，之后 ``log()`` 自动把 ``subtask_id`` 注入
        # 事件 detail，前端按这个分组折叠展示。
        self._tlocal = threading.local()

    def set_subtask(self, subtask_id: str, label: str = "") -> None:
        """绑定当前线程的子任务标签。子任务结束后调 ``clear_subtask`` 解绑。

        ``subtask_id`` 是稳定标识（如 ``worker_1``）；``label`` 是给前端
        展示的人类可读标题（如"账号 #1"）。
        """
        self._tlocal.subtask_id = str(subtask_id or "")
        self._tlocal.subtask_label = str(label or "")

    def set_attempt(self, context: AttemptContext) -> None:
        self._tlocal.attempt_id = context.attempt_id
        self._tlocal.attempt_context = context
        self._tlocal.registration_mode = context.effective_mode.value
        self._tlocal.registration_stage = context.stage.value

    def set_stage(self, stage: RegistrationStage) -> None:
        self._tlocal.registration_stage = stage.value

    def clear_subtask(self) -> None:
        for key in (
            "subtask_id",
            "subtask_label",
            "attempt_id",
            "attempt_context",
            "registration_mode",
            "registration_stage",
        ):
            try:
                delattr(self._tlocal, key)
            except AttributeError:
                pass

    def _current_subtask(self) -> tuple[str, str]:
        sid = getattr(self._tlocal, "subtask_id", "") or ""
        label = getattr(self._tlocal, "subtask_label", "") or ""
        return sid, label

    def log(self, message: str, *, level: str = "info", event_type: str = "log", detail: dict | None = None) -> None:
        # 自动给当前线程绑定的 subtask 加 detail，用于前端按 worker 分组折叠
        merged_detail = dict(detail or {})
        explicit_stage = str(merged_detail.get("stage") or "")
        sid, slabel = self._current_subtask()
        if sid and "subtask_id" not in merged_detail:
            merged_detail["subtask_id"] = sid
        if slabel and "subtask_label" not in merged_detail:
            merged_detail["subtask_label"] = slabel
        attempt_id = str(getattr(self._tlocal, "attempt_id", "") or "")
        mode = str(getattr(self._tlocal, "registration_mode", "") or "")
        stage = str(getattr(self._tlocal, "registration_stage", "") or "")
        if attempt_id:
            merged_detail.setdefault("attempt_id", attempt_id)
            merged_detail.setdefault("schema_version", 2)
        if mode:
            merged_detail.setdefault("mode", mode)
        if stage:
            merged_detail.setdefault("stage", stage)
        event_stage = explicit_stage
        if attempt_id and event_stage:
            try:
                normalized_stage = RegistrationStage(event_stage)
                context = getattr(self._tlocal, "attempt_context", None)
                if context is not None:
                    context.stage = normalized_stage
                    context.retry_count = max(
                        int(context.retry_count or 0),
                        int(merged_detail.get("retry_index") or 0),
                    )
                self._tlocal.registration_stage = normalized_stage.value
                registration_attempts.stage(
                    attempt_id,
                    normalized_stage,
                    retry_count=int(getattr(context, "retry_count", 0) or 0),
                )
            except (TypeError, ValueError):
                pass
        safe_message = redact_registration_text(message)
        append_task_event(
            self.task_id,
            safe_message,
            event_type=event_type,
            level=level,
            detail=merged_detail or None,
        )
        prefix = f"[task:{self.task_id}]"
        if sid:
            prefix += f"[{sid}]"
        print(f"{prefix} {safe_message}")

    def stage(
        self,
        stage: RegistrationStage,
        message: str,
        *,
        action: str = "enter",
        level: str = "info",
        detail: dict | None = None,
    ) -> None:
        self.set_stage(stage)
        payload = dict(detail or {})
        payload.update(
            {
                "kind": RegistrationEventKind.STAGE.value,
                "stage": stage.value,
                "action": action,
                "event_code": f"registration.{stage.value}.{action}",
                "schema_version": 2,
            }
        )
        self.log(message, level=level, event_type=RegistrationEventKind.STAGE.value, detail=payload)

    def mark_running(self) -> bool:
        def _update(task: TaskModel) -> None:
            if task.status in TERMINAL_TASK_STATUSES or task.status == TASK_STATUS_CANCEL_REQUESTED:
                return
            task.status = TASK_STATUS_RUNNING
            task.started_at = task.started_at or _utcnow()

        task = _mutate_task(self.task_id, _update)
        if not task or task.status != TASK_STATUS_RUNNING:
            return False
        self.log("任务已开始执行", event_type="state")
        return True

    def is_cancel_requested(self) -> bool:
        if self._cancel_event is not None and self._cancel_event.is_set():
            return True
        with Session(engine) as session:
            task = session.get(TaskModel, self.task_id)
            cancelled = bool(
                task
                and task.status in {TASK_STATUS_CANCEL_REQUESTED, TASK_STATUS_CANCELLED}
            )
        if cancelled and self._cancel_event is not None:
            self._cancel_event.set()
        return cancelled

    def set_progress(self, current: int, total: Optional[int] = None) -> None:
        current = max(int(current), 0)

        def _update(task: TaskModel) -> None:
            task.progress_current = current
            if total is not None:
                task.progress_total = max(int(total), 0)

        _mutate_task(self.task_id, _update)

    def record_success(self) -> None:
        def _update(task: TaskModel) -> None:
            task.success_count += 1

        _mutate_task(self.task_id, _update)

    def record_error(self, error: str) -> None:
        def _update(task: TaskModel) -> None:
            task.error_count += 1
            result = task.get_result()
            errors = list(result.get("errors", []))
            errors.append(error)
            result["errors"] = errors
            task.set_result(result)

        _mutate_task(self.task_id, _update)

    def add_cashier_url(self, url: str) -> None:
        def _update(task: TaskModel) -> None:
            result = task.get_result()
            urls = list(result.get("cashier_urls", []))
            urls.append(url)
            result["cashier_urls"] = urls
            task.set_result(result)

        _mutate_task(self.task_id, _update)

    def set_result_data(self, data: Any, *, merge: bool = True) -> None:
        def _update(task: TaskModel) -> None:
            result = task.get_result()
            current = result.get("data")
            if merge and isinstance(current, dict) and isinstance(data, dict):
                result["data"] = {**current, **data}
            else:
                result["data"] = data
            task.set_result(result)

        _mutate_task(self.task_id, _update)

    def finish(self, status: str, *, error: str = "") -> None:
        def _update(task: TaskModel) -> None:
            requested_cancel = (
                task.status == TASK_STATUS_CANCEL_REQUESTED
                or bool(self._cancel_event and self._cancel_event.is_set())
            )
            task.status = TASK_STATUS_CANCELLED if requested_cancel else status
            task.finished_at = _utcnow()
            final_error = "任务已取消" if requested_cancel else error
            if final_error:
                task.error = final_error

        task = _mutate_task(self.task_id, _update)
        final_status = task.status if task else status
        final_error = task.error if task else error
        event_level = "error" if final_status == TASK_STATUS_FAILED else ("warning" if final_status in {TASK_STATUS_INTERRUPTED, TASK_STATUS_CANCELLED} else "info")
        self.log(
            f"任务结束: {final_status}",
            level=event_level,
            event_type="state",
            detail={"status": final_status, "error": final_error},
        )


def _build_platform_instance(
    platform_name: str,
    payload: dict[str, Any],
    logger: TaskLogger,
    resolved_proxy: str | None = None,
):
    from core.base_identity import normalize_identity_provider
    from core.base_mailbox import create_mailbox

    executor_type = str(payload.get("executor_type", "headless") or "headless")
    captcha_solver = str(payload.get("captcha_solver", "auto") or "auto")
    extra = dict(payload.get("extra") or {})
    config = RegisterConfig(
        executor_type=executor_type,
        captcha_solver=captcha_solver,
        proxy=resolved_proxy,
        extra=extra,
    )
    identity_provider = normalize_identity_provider(extra.get("identity_provider", "mailbox"))
    mailbox = None
    if identity_provider == "mailbox":
        if not extra.get("mail_provider"):
            from infrastructure.provider_settings_repository import ProviderSettingsRepository

            extra["mail_provider"] = ProviderSettingsRepository().get_default_provider_key("mailbox")
        mailbox = create_mailbox(
            provider=extra.get("mail_provider", ""),
            extra=extra,
            proxy=resolved_proxy,
        )
    if identity_provider == "mailbox" and mailbox is not None:
        from services.registration_mailbox import lease_mailbox_for_attempt

        mailbox = lease_mailbox_for_attempt(
            mailbox,
            owner_attempt_id=str(extra.get("registration_attempt_id") or ""),
            provider=str(extra.get("mail_provider") or ""),
            ttl_seconds=int(extra.get("registration_lease_ttl_seconds") or 300),
        )

    platform_cls = get(platform_name)
    platform = platform_cls(config=config, mailbox=mailbox)
    if hasattr(platform, "set_logger"):
        platform.set_logger(logger.log)
    else:
        platform._log_fn = logger.log
    return platform


def _run_single_account_check(account_id: int, logger: TaskLogger | None = None) -> tuple[bool, dict[str, Any]]:
    with Session(engine) as session:
        model = session.get(AccountModel, account_id)
        if not model:
            raise ValueError("账号不存在")
        plugin = get(model.platform)(config=RegisterConfig())
        account = build_platform_account(session, model)

    valid = plugin.check_valid(account)
    with Session(engine) as session:
        model = session.get(AccountModel, account_id)
        if model:
            model.updated_at = _utcnow()
            current_graph = load_account_graphs(session, [account_id]).get(account_id, {})
            summary_updates = {"checked_at": _utcnow_iso(), "valid": bool(valid)}
            if hasattr(plugin, "get_last_check_overview"):
                summary_updates.update(plugin.get_last_check_overview() or {})
            lifecycle_status = None
            if valid:
                # **bug 修复**：原实现 ``recover_lifecycle_status_for_valid_account``
                # 直接读 ``current_graph`` 老快照——但 plugin 刚拉到的新
                # ``plan_state`` 在 ``summary_updates`` 里、还没写回 graph，
                # 导致 free → 重新刷新仍然被认成 subscribed。这里把
                # ``summary_updates`` merge 到 graph 里再算 lifecycle。
                merged_graph = dict(current_graph)
                merged_overview = dict(merged_graph.get("overview") or {})
                merged_overview.update(summary_updates)
                merged_graph["overview"] = merged_overview
                lifecycle_status = recover_lifecycle_status_for_valid_account(merged_graph)
            else:
                lifecycle_status = AccountStatus.INVALID.value
            patch_account_graph(
                session,
                model,
                lifecycle_status=lifecycle_status,
                summary_updates=summary_updates,
            )
            session.add(model)
            session.commit()

    result = {"account_id": account_id, "valid": bool(valid), "platform": account.platform, "email": account.email}
    if logger:
        logger.log(f"{account.email}: {'有效' if valid else '失效'}")
    return valid, result


def execute_task(task_id: str) -> None:
    with Session(engine) as session:
        task = session.get(TaskModel, task_id)
        if not task:
            return
        task_type = task.type
        payload = task.get_payload()

    logger = TaskLogger(task_id)
    try:
        # Do the atomic status transition before entering a handler.  A user
        # can cancel a claimed task in the small gap between dispatch and
        # worker startup; the old unconditional mark_running() overwrote that
        # request and allowed the whole batch to continue.
        if logger.is_cancel_requested() or not logger.mark_running():
            logger.finish(TASK_STATUS_CANCELLED, error="任务在启动后立即被取消")
            return

        handlers: dict[str, Callable[[dict[str, Any], TaskLogger], None]] = {
            TASK_TYPE_REGISTER: _execute_register_task,
            TASK_TYPE_ACCOUNT_CHECK_ALL: _execute_account_check_all_task,
        }
        handler = handlers.get(task_type)
        if not handler:
            logger.finish(TASK_STATUS_FAILED, error=f"未知任务类型: {task_type}")
            return
        handler(payload, logger)
    finally:
        _forget_task_cancel_event(task_id)


def _resolve_registration_proxy_for_platform(
    platform_name: str,
    *,
    explicit_proxy: str | None,
    proxy_getter: Callable[[], str | None],
    allow_pool: bool = True,
) -> str | None:
    """Resolve proxy for registration. Direct connection is always allowed.

    Priority: explicit task proxy → optional proxy_runtime → pool (if allow_pool) → None.
    """
    normalized_explicit = str(explicit_proxy or "").strip() or None
    if normalized_explicit:
        try:
            from core.proxy_runtime import resolve_registration_proxy

            return resolve_registration_proxy(explicit_proxy=normalized_explicit)
        except Exception:
            return normalized_explicit

    pool_proxy = None
    if allow_pool:
        try:
            pool_proxy = proxy_getter()
        except Exception:
            pool_proxy = None
    try:
        from core.proxy_runtime import resolve_registration_proxy

        return resolve_registration_proxy(
            explicit_proxy=None,
            pool_proxy=pool_proxy if allow_pool else None,
        )
    except Exception:
        return pool_proxy if allow_pool else None


def _registration_concurrency(
    requested: Any,
    count: int,
    *,
    executor_type: str = "protocol",
    proxy_count: int | None = None,
    egress_ref: str = "direct",
) -> int:
    try:
        mode = RegistrationMode(str(executor_type or "protocol"))
    except ValueError:
        mode = RegistrationMode.PROTOCOL
    requested_value = min(
        max(int(requested or 1), 1),
        max(int(count or 1), 1),
        MAX_REGISTER_CONCURRENCY,
    )
    direct = not proxy_count or int(proxy_count) <= 0
    if not direct and proxy_count is not None and int(proxy_count) > 0:
        # The task-level key represents a pool, not a physical exit.  Each
        # attempt is subsequently constrained by its resolved egress gate and
        # proxy lease, so the pool scheduler may fill one slot per available
        # independent exit without waiting for a fictitious "proxy-pool"
        # AIMD history to grow.
        return max(1, min(requested_value, int(proxy_count), max(int(count), 1)))
    return registration_capacity.effective_concurrency(
        mode,
        requested_value,
        direct=direct,
        count=count,
        proxy_count=proxy_count,
        egress_ref=egress_ref,
    )


def _proxy_host(url: str | None) -> str:
    raw = str(url or "").strip()
    if not raw:
        return "direct"
    try:
        from urllib.parse import urlparse

        return urlparse(raw).hostname or raw[:48]
    except Exception:
        return raw[:48]


def _inter_account_jitter_seconds(
    extra: dict[str, Any],
    *,
    has_proxy: bool,
    mode: RegistrationMode | None = None,
    healthy_concurrency: int = 1,
) -> float:
    """Random delay between accounts on the same worker (anti-risk pacing)."""
    if extra.get("disable_register_jitter"):
        return 0.0
    if has_proxy:
        default_lo, default_hi = 8.0, 20.0
    elif int(healthy_concurrency or 1) >= 2:
        # Once the shared direct egress has proved two slots, keep attempts
        # out of phase but let long protocol/browser flows overlap.
        if mode in {RegistrationMode.HEADLESS, RegistrationMode.HEADED}:
            default_lo, default_hi = 15.0, 22.0
        else:
            default_lo, default_hi = 8.0, 12.0
    else:
        default_lo, default_hi = 30.0, 45.0
    try:
        lo = float(extra.get("register_jitter_min_seconds") or default_lo)
        hi = float(extra.get("register_jitter_max_seconds") or default_hi)
    except Exception:
        lo, hi = default_lo, default_hi
    if hi < lo:
        lo, hi = hi, lo
    import random

    return max(0.0, random.uniform(lo, hi))


def _registration_account_timeout(payload: dict[str, Any], extra: dict[str, Any]) -> int:
    """Per-account wall-clock budget (industry fail-fast slot timeout)."""
    raw = (
        payload.get("account_timeout_seconds")
        or extra.get("account_timeout_seconds")
        or extra.get("register_account_timeout_seconds")
        or DEFAULT_REGISTER_ACCOUNT_TIMEOUT_SECONDS
    )
    try:
        value = int(raw)
    except Exception:
        value = DEFAULT_REGISTER_ACCOUNT_TIMEOUT_SECONDS
    return max(MIN_REGISTER_ACCOUNT_TIMEOUT_SECONDS, min(value, MAX_REGISTER_ACCOUNT_TIMEOUT_SECONDS))


def _execute_register_task(payload: dict[str, Any], logger: TaskLogger) -> None:
    from core.proxy_pool import proxy_pool
    from application.register_metrics import classify_register_error, record_register_attempt
    from platforms.chatgpt.browser_profiles import pick_camoufox_profile, pick_chrome_profile

    count = max(int(payload.get("count", 1) or 1), 1)
    platform_name = "chatgpt"
    email = payload.get("email") or None
    password = payload.get("password") or None
    explicit_proxy = str(payload.get("proxy") or "").strip() or None
    extra = dict(payload.get("extra") or {})
    account_timeout = _registration_account_timeout(payload, extra)
    try:
        queue_timeout = int(extra.get("queue_timeout_seconds") or account_timeout)
    except (TypeError, ValueError):
        queue_timeout = account_timeout
    queue_timeout = max(30, min(queue_timeout, MAX_REGISTER_ACCOUNT_TIMEOUT_SECONDS))
    # Cap OTP wait inside each account so it never exceeds the slot budget.
    otp_cap = max(30, min(int(extra.get("otp_timeout") or 90), account_timeout - 15))
    extra.setdefault("otp_timeout", otp_cap)
    executor_type = str(payload.get("executor_type") or extra.get("executor_type") or "protocol")
    try:
        requested_mode = RegistrationMode(executor_type)
    except ValueError:
        logger.log(f"不支持的注册方式: {executor_type}", level="error")
        logger.finish(TASK_STATUS_FAILED, error=f"不支持的注册方式: {executor_type}")
        return

    if getattr(sys, "frozen", False):
        try:
            from services.browser_runtime import ensure_camoufox, ensure_playwright_chromium

            if requested_mode == RegistrationMode.PROTOCOL:
                ensure_playwright_chromium()
            else:
                ensure_camoufox()
        except Exception as exc:
            message = f"注册运行时准备失败: {type(exc).__name__}: {exc}"
            logger.log(message, level="error")
            logger.finish(TASK_STATUS_FAILED, error=message)
            return

    proxy_count = 0
    try:
        proxy_count = int(proxy_pool.count_available())
    except Exception:
        proxy_count = 0
    if explicit_proxy:
        proxy_count = max(proxy_count, 1)
    task_direct = proxy_count <= 0
    task_egress_ref = "proxy-pool"
    if task_direct:
        try:
            from core.proxy_runtime import resolve_egress_ref

            task_egress_ref = resolve_egress_ref(None, timeout=4.0)
        except Exception:
            task_egress_ref = "direct"
    concurrency = _registration_concurrency(
        payload.get("concurrency", 1),
        count,
        executor_type=executor_type,
        proxy_count=proxy_count,
        egress_ref=task_egress_ref,
    )
    requested_concurrency = min(
        max(int(payload.get("concurrency", 1) or 1), 1),
        count,
        MAX_REGISTER_CONCURRENCY,
        MODE_CAPACITY[requested_mode].maximum,
    )
    worker_capacity = max(requested_concurrency, concurrency)

    logger.set_progress(0, count)
    logger.log(
        f"注册并发 requested={int(payload.get('concurrency', 1) or 1)} effective={concurrency} "
        f"count={count} proxies≈{proxy_count} "
        f"executor={executor_type} queue_timeout={queue_timeout}s "
        f"account_timeout={account_timeout}s otp_cap={otp_cap}s "
        f"(模式严格独立；每号独立 session/指纹/代理)"
    )
    logger.set_result_data(
        {
            "requested_concurrency": int(payload.get("concurrency", 1) or 1),
            "effective_concurrency": concurrency,
            "requested_mode": requested_mode.value,
            "effective_mode": requested_mode.value,
        }
    )
    try:
        get(platform_name)
    except Exception as exc:
        logger.log(f"致命错误: {exc}", level="error")
        logger.finish(TASK_STATUS_FAILED, error=str(exc))
        return

    # Resolve the provider key once, but instantiate the mailbox inside each
    # attempt. Several providers keep current-address state and must not be
    # shared by concurrent protocol workers.
    try:
        from core.base_identity import normalize_identity_provider

        identity_provider = normalize_identity_provider(extra.get("identity_provider", "mailbox"))
        if identity_provider == "mailbox":
            if not extra.get("mail_provider"):
                from infrastructure.provider_settings_repository import ProviderSettingsRepository

                extra["mail_provider"] = ProviderSettingsRepository().get_default_provider_key("mailbox")
    except Exception as exc:
        logger.log(f"邮箱初始化失败: {exc}", level="error")
        logger.finish(TASK_STATUS_FAILED, error=f"邮箱初始化失败: {exc}")
        return

    timed_out_count = 0
    timed_out_lock = threading.Lock()
    class_counts: dict[str, int] = {}
    class_lock = threading.Lock()
    active_attempts = 0
    peak_active_attempts = 0
    active_attempts_lock = threading.Lock()
    protocol_profiles_by_egress: dict[str, dict[str, Any]] = {}
    protocol_profile_lock = threading.Lock()

    def _profile_for_egress(egress_ref: str) -> dict[str, Any]:
        if requested_mode != RegistrationMode.PROTOCOL:
            profile = pick_camoufox_profile()
            profile.update(
                {
                    "egress_id": egress_ref,
                    "proxy_lease_id": egress_ref,
                    "sticky_session_id": egress_ref,
                }
            )
            return profile
        # Keep one coherent protocol UA/TLS/Client-Hints bundle per physical
        # egress for the lifetime of this task.  A fresh device/session is still
        # created for every account, while FlareSolverr can retain the matching
        # browser session instead of starting from a different Chrome identity.
        with protocol_profile_lock:
            cached = protocol_profiles_by_egress.get(egress_ref)
            if cached is None:
                cached = pick_chrome_profile()
                cached.update(
                    {
                        # Scheduler leases, clearance cache and FlareSolverr
                        # sessions must use the same observed physical egress
                        # identity.  Falling back to ``direct`` or a proxy URL
                        # hash splits one exit into unrelated identities.
                        "egress_id": egress_ref,
                        "proxy_lease_id": egress_ref,
                        "sticky_session_id": egress_ref,
                    }
                )
                protocol_profiles_by_egress[egress_ref] = dict(cached)
            return dict(cached)

    def _do_one(index: int) -> dict[str, Any] | str:
        nonlocal timed_out_count, active_attempts, peak_active_attempts
        if logger.is_cancel_requested():
            return "__cancel_requested__"

        queued_monotonic = time.monotonic()
        queue_deadline = queued_monotonic + queue_timeout
        execution_started_monotonic: float | None = None
        slot_deadline = queue_deadline
        slot_cancel = threading.Event()
        slot_proxy = _resolve_registration_proxy_for_platform(
            platform_name,
            explicit_proxy=explicit_proxy,
            proxy_getter=proxy_pool.get_next,
            allow_pool=True,
        )
        is_direct = not bool(slot_proxy)
        try:
            from core.proxy_runtime import resolve_egress_ref

            egress_ref = task_egress_ref if is_direct else resolve_egress_ref(slot_proxy, timeout=4.0)
        except Exception:
            egress_ref = stable_resource_ref(slot_proxy)
        profile = _profile_for_egress(egress_ref)
        mail_provider = str(extra.get("mail_provider") or "")
        context = AttemptContext(
            task_id=str(getattr(logger, "task_id", "task") or "task"),
            ordinal=index + 1,
            requested_mode=requested_mode,
            effective_mode=requested_mode,
            deadline_monotonic=queue_deadline,
            mail_provider=mail_provider,
            proxy_ref=egress_ref,
            fingerprint_id=str(profile.get("fingerprint_id") or profile.get("key") or ""),
        )
        lease_owner_ids = [context.attempt_id]
        lease_heartbeat_stop = threading.Event()
        lease_heartbeat_thread: threading.Thread | None = None
        egress_lease_id = ""
        pending_egress_cooldown = 0
        egress_slot = (
            f"{context.proxy_ref}:slot:{index % max(requested_concurrency, 1)}"
            if is_direct
            else context.proxy_ref
        )
        # Direct capacity leases may use independent slots, but start pacing
        # and post-attempt cooldowns are shared by the physical egress.
        egress_pace_ref = context.proxy_ref
        capacity_already_recorded = False
        logger.set_subtask(f"account_{index + 1}", f"账号 {index + 1}")
        if hasattr(logger, "set_attempt"):
            logger.set_attempt(context)
        registration_attempts.start(context)

        def _lease_heartbeat_loop() -> None:
            while not lease_heartbeat_stop.wait(5.0):
                for owner_id in tuple(lease_owner_ids):
                    try:
                        resource_leases.heartbeat_owner(
                            owner_id,
                            ttl_seconds=account_timeout + 30,
                        )
                    except Exception:
                        continue

        def _stage(
            stage: RegistrationStage,
            message: str,
            *,
            action: str = "enter",
            detail: dict | None = None,
        ) -> None:
            context.stage = stage
            registration_attempts.stage(
                context.attempt_id,
                stage,
                retry_count=context.retry_count,
                replacement_count=context.replacement_count,
            )
            if hasattr(logger, "stage"):
                logger.stage(stage, message, action=action, detail=detail)
                return
            payload = dict(detail or {})
            payload.update(
                {
                    "kind": RegistrationEventKind.STAGE.value,
                    "stage": stage.value,
                    "action": action,
                    "attempt_id": context.attempt_id,
                    "schema_version": 2,
                }
            )
            logger.log(message, event_type=RegistrationEventKind.STAGE.value, detail=payload)

        def _slot_cancel_check() -> bool:
            if logger.is_cancel_requested():
                slot_cancel.set()
                return True
            if time.monotonic() >= slot_deadline:
                slot_cancel.set()
                return True
            return slot_cancel.is_set()

        def _metrics(
            ok: bool,
            err: str = "",
            *,
            record_capacity: bool = True,
            record_class: bool = True,
        ) -> RegistrationErrorCode | None:
            nonlocal pending_egress_cooldown
            code = None if ok else classify_registration_error(err)
            cls = "" if ok else classify_register_error(err)
            if cls and record_class:
                with class_lock:
                    class_counts[code.value if code else cls] = class_counts.get(code.value if code else cls, 0) + 1
            duration_started = execution_started_monotonic or queued_monotonic
            duration = max(time.monotonic() - duration_started, 0.0)
            outcome_cooldown = 0
            if record_capacity:
                outcome_cooldown = registration_capacity.record_outcome(
                    mode=requested_mode,
                    egress_ref=context.proxy_ref,
                    direct=is_direct,
                    success=ok,
                    error_code=code,
                    duration_seconds=duration,
                    memory_percent=registration_capacity.memory_percent(),
                    pace_ref=egress_pace_ref,
                    apply_cooldown=not bool(extra.get("disable_register_cooldown")),
                )
            egress_cooldown = outcome_cooldown
            if not record_capacity:
                egress_cooldown = registration_capacity.outcome_cooldown_seconds(
                    direct=is_direct,
                    success=ok,
                    error_code=code,
                )
            if egress_cooldown and not bool(extra.get("disable_register_cooldown")):
                pending_egress_cooldown = max(pending_egress_cooldown, egress_cooldown)
            try:
                record_register_attempt(
                    ok=ok,
                    executor=requested_mode.value,
                    proxy_host=_proxy_host(slot_proxy),
                    mail_provider=mail_provider,
                    profile_key=str(profile.get("fingerprint_id") or profile.get("key") or ""),
                    error=err,
                    error_class=code.value if code else cls,
                )
            except Exception:
                pass
            return code

        def _timing_metadata() -> dict[str, int]:
            now = time.monotonic()
            execution_started = execution_started_monotonic or now
            return {
                "queue_ms": max(int((execution_started - queued_monotonic) * 1000), 0),
                "run_ms": (
                    max(int((now - execution_started) * 1000), 0)
                    if execution_started_monotonic is not None
                    else 0
                ),
            }

        try:
            _stage(
                RegistrationStage.PREPARE,
                f"准备第 {index + 1}/{count} 个账号",
                detail={
                    "requested_mode": requested_mode.value,
                    "effective_mode": requested_mode.value,
                    "proxy_ref": context.proxy_ref,
                    "mail_provider": mail_provider,
                },
            )

            pacing_health = registration_capacity.health(
                requested_mode,
                egress_pace_ref,
                direct=is_direct,
            )
            gap = _inter_account_jitter_seconds(
                extra,
                has_proxy=bool(slot_proxy),
                mode=requested_mode,
                healthy_concurrency=int(pacing_health.get("healthy_concurrency") or 1),
            )
            wait_seconds = registration_capacity.pace(
                egress_pace_ref,
                gap,
                include_finish_cooldown=not bool(extra.get("disable_register_cooldown")),
            )
            if wait_seconds > 0:
                logger.log(
                    f"启动节奏等待 {wait_seconds:.1f}s",
                    event_type=RegistrationEventKind.RETRY.value,
                    detail={"kind": "retry", "action": "pace", "duration_ms": int(wait_seconds * 1000)},
                )
                wait_until = time.monotonic() + wait_seconds
                while time.monotonic() < wait_until:
                    if _slot_cancel_check():
                        if logger.is_cancel_requested():
                            raise BrowserWorkerCancelled("registration cancelled while pacing")
                        raise CapacityTimeout("registration queue deadline exceeded")
                    remaining = max(wait_until - time.monotonic(), 0.0)
                    if remaining <= 0:
                        break
                    time.sleep(min(0.25, remaining))

            if _slot_cancel_check():
                raise CapacityTimeout("registration queue deadline exceeded")

            _stage(RegistrationStage.PREFLIGHT, "检查网络与资源")
            if slot_proxy and bool(extra.get("proxy_preflight", True)):
                probe = proxy_pool.probe_chatgpt(slot_proxy, timeout=min(10.0, context.remaining_seconds()))
                if not probe.get("ok"):
                    proxy_pool.report_fail(slot_proxy)
                    raise RuntimeError(
                        f"代理不可用(chatgpt.com): {probe.get('error') or probe.get('status_code')}"
                    )

            with registration_capacity.slot(
                mode=requested_mode,
                proxy_ref=context.proxy_ref,
                mail_provider=mail_provider,
                timeout_seconds=max(context.remaining_seconds(), 1.0),
                cancel_check=_slot_cancel_check,
                direct=is_direct,
            ):
                lease_wait_logged_at = 0.0
                while True:
                    try:
                        egress_lease = resource_leases.acquire(
                            resource_type="egress",
                            resource_id=egress_slot,
                            owner_attempt_id=context.attempt_id,
                            ttl_seconds=account_timeout + 30,
                            metadata={"host": _proxy_host(slot_proxy), "egress_ref": context.proxy_ref},
                        )
                        egress_lease_id = str(egress_lease.id or "")
                        break
                    except ResourceLeaseConflict as lease_exc:
                        detail = str(lease_exc)
                        if not any(
                            marker in detail
                            for marker in ("resource already leased: egress", "resource cooling down: egress")
                        ):
                            raise
                        if _slot_cancel_check():
                            raise CapacityTimeout("registration queue deadline exceeded") from lease_exc
                        now = time.monotonic()
                        if now - lease_wait_logged_at >= 5:
                            lease_wait_logged_at = now
                            logger.log(
                                "出口正在被其他注册任务使用，继续排队",
                                event_type=RegistrationEventKind.RETRY.value,
                                detail={
                                    "kind": RegistrationEventKind.RETRY.value,
                                    "action": "wait_egress_lease",
                                    "error_code": RegistrationErrorCode.EGRESS_COOLDOWN.value,
                                    "schema_version": 2,
                                },
                            )
                        time.sleep(min(0.5, max(queue_deadline - now, 0.05)))

                execution_started_monotonic = time.monotonic()
                slot_deadline = execution_started_monotonic + account_timeout
                context.deadline_monotonic = slot_deadline
                with active_attempts_lock:
                    active_attempts += 1
                    peak_active_attempts = max(peak_active_attempts, active_attempts)
                    logger.set_result_data(
                        {
                            "current_concurrency_limit": current_limit if "current_limit" in locals() else concurrency,
                            "active_concurrency": active_attempts,
                            "peak_active_concurrency": peak_active_attempts,
                            "effective_concurrency": peak_active_attempts,
                            "healthy_concurrency": int(
                                registration_capacity.health(
                                    requested_mode,
                                    task_egress_ref,
                                    direct=task_direct,
                                ).get("healthy_concurrency")
                                or 1
                            ),
                        }
                    )
                if slot_proxy:
                    resource_leases.acquire(
                        resource_type="proxy",
                        resource_id=context.proxy_ref,
                        owner_attempt_id=context.attempt_id,
                        ttl_seconds=account_timeout + 30,
                        metadata={"host": _proxy_host(slot_proxy)},
                    )
                if email:
                    resource_leases.acquire(
                        resource_type="mailbox",
                        resource_id=stable_resource_ref(email),
                        owner_attempt_id=context.attempt_id,
                        ttl_seconds=account_timeout + 30,
                        metadata={"provider": mail_provider},
                    )
                lease_heartbeat_thread = threading.Thread(
                    target=_lease_heartbeat_loop,
                    name=f"registration-lease-heartbeat-{context.attempt_id[:8]}",
                    daemon=False,
                )
                lease_heartbeat_thread.start()
                _stage(
                    RegistrationStage.AUTH_BEGIN,
                    f"启动 {requested_mode.value} 注册",
                    detail={"proxy_ref": context.proxy_ref, "fingerprint_id": context.fingerprint_id},
                )

                max_replacements = 0 if email else max(
                    0,
                    min(int(extra.get("identity_replacement_limit") or 2), 2),
                )
                account = None
                platform = None
                for replacement_index in range(max_replacements + 1):
                    capacity_already_recorded = False
                    if replacement_index:
                        context.replacement_count = replacement_index
                        context.retry_count += 1
                        profile = _profile_for_egress(context.proxy_ref)
                        context.fingerprint_id = str(
                            profile.get("fingerprint_id") or profile.get("key") or ""
                        )
                        registration_attempts.stage(
                            context.attempt_id,
                            context.stage,
                            retry_count=context.retry_count,
                            replacement_count=context.replacement_count,
                        )
                    physical_owner = (
                        context.attempt_id
                        if replacement_index == 0
                        else f"{context.attempt_id}:replacement:{replacement_index}"
                    )
                    if physical_owner not in lease_owner_ids:
                        lease_owner_ids.append(physical_owner)
                    slot_extra = {
                        **extra,
                        "browser_profile": profile,
                        "sentinel_browser_runtime": True,
                        "registration_attempt_id": physical_owner,
                        "registration_task_id": context.task_id,
                        "registration_deadline_monotonic": slot_deadline,
                        "registration_lease_ttl_seconds": account_timeout + 30,
                        "replacement_count": replacement_index,
                    }
                    slot_payload = {
                        **payload,
                        "executor_type": requested_mode.value,
                        "extra": slot_extra,
                    }
                    try:
                        if requested_mode == RegistrationMode.PROTOCOL:
                            platform = _build_platform_instance(
                                platform_name,
                                slot_payload,
                                logger,
                                resolved_proxy=slot_proxy,
                            )
                            if hasattr(platform, "set_cancel_checker"):
                                platform.set_cancel_checker(_slot_cancel_check)
                            account = platform.register(email=email, password=password)
                        else:
                            account = browser_process_supervisor.run(
                                BrowserProcessRequest(
                                    platform_name=platform_name,
                                    payload=slot_payload,
                                    resolved_proxy=slot_proxy,
                                    email=email,
                                    password=password,
                                ),
                                timeout_seconds=max(context.remaining_seconds(), 1.0),
                                cancel_check=_slot_cancel_check,
                                log_callback=logger.log,
                            )
                        break
                    except Exception as run_exc:
                        run_code = classify_registration_error(run_exc)
                        mailbox_resource_id = str(
                            getattr(run_exc, "mailbox_resource_id", "") or ""
                        )
                        if not mailbox_resource_id and platform is not None:
                            identity = getattr(platform, "_last_identity", None)
                            resolved_email = str(getattr(identity, "email", "") or "")
                            if resolved_email:
                                mailbox_resource_id = stable_resource_ref(resolved_email.lower())
                        if run_code == RegistrationErrorCode.AUTH_IDENTITY_PROVIDER_MISMATCH:
                            mailbox_domain_ref = str(
                                getattr(run_exc, "metadata", {}).get("mailbox_domain_ref")
                                if isinstance(getattr(run_exc, "metadata", None), dict)
                                else ""
                            )
                            if not mailbox_domain_ref and platform is not None:
                                identity = getattr(platform, "_last_identity", None)
                                resolved_email = str(getattr(identity, "email", "") or "").lower()
                                _local, _separator, domain = resolved_email.rpartition("@")
                                if domain:
                                    mailbox_domain_ref = stable_resource_ref(domain)
                            cooldown_seconds = registration_capacity.record_identity_mismatch(
                                mode=requested_mode,
                                egress_ref=context.proxy_ref,
                                mailbox_domain_ref=mailbox_domain_ref,
                                direct=is_direct,
                            )
                            capacity_already_recorded = True
                            with class_lock:
                                class_counts[run_code.value] = class_counts.get(run_code.value, 0) + 1
                            pending_egress_cooldown = max(
                                pending_egress_cooldown,
                                cooldown_seconds,
                            )
                            if mailbox_resource_id:
                                resource_leases.mark_resource(
                                    resource_type="mailbox",
                                    resource_id=mailbox_resource_id,
                                    status="quarantined",
                                    cooldown_seconds=7 * 24 * 3600,
                                    metadata={"provider": mail_provider, "reason": run_code.value},
                                )
                            if replacement_index < max_replacements and context.remaining_seconds() > 30:
                                if execution_started_monotonic is not None:
                                    logical_budget = min(
                                        max(
                                            int(extra.get("logical_account_timeout_seconds") or 1200),
                                            account_timeout,
                                        ),
                                        1200,
                                    )
                                    slot_deadline = max(
                                        slot_deadline,
                                        execution_started_monotonic + logical_budget,
                                    )
                                    context.deadline_monotonic = slot_deadline
                                logger.log(
                                    f"身份事务不一致，隔离当前邮箱并更换邮箱 ({replacement_index + 1}/{max_replacements})",
                                    level="warning",
                                    event_type=RegistrationEventKind.RETRY.value,
                                    detail={
                                        "kind": RegistrationEventKind.RETRY.value,
                                        "action": "replace_mailbox",
                                        "error_code": run_code.value,
                                        "retry_index": replacement_index + 1,
                                        "schema_version": 2,
                                    },
                                )
                                raw_retry_delay = extra.get("identity_replacement_delay_seconds")
                                configured_retry_delay = (
                                    float(cooldown_seconds)
                                    if raw_retry_delay in (None, "")
                                    else max(float(raw_retry_delay), 0.0)
                                )
                                retry_delay = min(
                                    configured_retry_delay,
                                    max(context.remaining_seconds() - 15, 0),
                                )
                                retry_until = time.monotonic() + retry_delay
                                while time.monotonic() < retry_until:
                                    if _slot_cancel_check():
                                        raise BrowserWorkerCancelled("registration cancelled during identity cooldown")
                                    time.sleep(min(0.25, retry_until - time.monotonic()))
                                continue
                        if run_code in {
                            RegistrationErrorCode.OTP_PROVIDER,
                            RegistrationErrorCode.OTP_TIMEOUT,
                            RegistrationErrorCode.OTP_STALE,
                            RegistrationErrorCode.OTP_INVALID,
                        }:
                            if mailbox_resource_id:
                                resource_leases.mark_resource(
                                    resource_type="mailbox",
                                    resource_id=mailbox_resource_id,
                                    status="quarantined",
                                    cooldown_seconds=24 * 3600,
                                    metadata={"provider": mail_provider, "reason": run_code.value},
                                )
                            if replacement_index < max_replacements and context.remaining_seconds() > 30:
                                with class_lock:
                                    class_counts[run_code.value] = class_counts.get(run_code.value, 0) + 1
                                registration_capacity.record_outcome(
                                    mode=requested_mode,
                                    egress_ref=context.proxy_ref,
                                    direct=is_direct,
                                    success=False,
                                    error_code=run_code,
                                    duration_seconds=max(
                                        time.monotonic()
                                        - (execution_started_monotonic or queued_monotonic),
                                        0.0,
                                    ),
                                    memory_percent=registration_capacity.memory_percent(),
                                    pace_ref=egress_pace_ref,
                                    apply_cooldown=False,
                                )
                                if execution_started_monotonic is not None:
                                    logical_budget = min(
                                        max(
                                            int(extra.get("logical_account_timeout_seconds") or 1200),
                                            account_timeout,
                                        ),
                                        1200,
                                    )
                                    slot_deadline = max(
                                        slot_deadline,
                                        execution_started_monotonic + logical_budget,
                                    )
                                    context.deadline_monotonic = slot_deadline
                                logger.log(
                                    f"邮箱验证码链路失败，隔离当前邮箱并更换邮箱 ({replacement_index + 1}/{max_replacements})",
                                    level="warning",
                                    event_type=RegistrationEventKind.RETRY.value,
                                    detail={
                                        "kind": RegistrationEventKind.RETRY.value,
                                        "action": "replace_mailbox",
                                        "error_code": run_code.value,
                                        "retry_index": replacement_index + 1,
                                        "schema_version": 2,
                                    },
                                )
                                continue
                        raise

                if account is None:
                    raise RuntimeError("AUTH_SESSION_DESYNC: registration ended without account result")

                _stage(RegistrationStage.SESSION_VALIDATE, "验证 ChatGPT Session")
                token = str((account.extra or {}).get("access_token") or account.token or "")
                if not token:
                    raise RuntimeError("注册完成但缺少 access_token，不计成功")

                _stage(RegistrationStage.PERSIST, "保存账号与凭证")
                saved_account = save_account(account)
                saved_account_id = int(saved_account.id)
                if account.email:
                    resource_leases.mark_resource(
                        resource_type="mailbox",
                        resource_id=stable_resource_ref(account.email.lower()),
                        status="consumed",
                        metadata={"provider": mail_provider, "account_id": saved_account_id},
                    )

            if slot_proxy:
                proxy_pool.report_success(slot_proxy)
            logger.record_success()
            _stage(
                RegistrationStage.DONE,
                "ChatGPT 注册成功",
                action="complete",
                detail={"status": RegistrationAttemptStatus.SUCCEEDED.value, "account_id": saved_account_id},
            )
            _metrics(True)
            registration_attempts.finish(
                context.attempt_id,
                status=RegistrationAttemptStatus.SUCCEEDED,
                account_id=saved_account_id,
                metadata={
                    "proxy_ref": context.proxy_ref,
                    "fingerprint_id": context.fingerprint_id,
                    **_timing_metadata(),
                },
            )
            return {
                "account_id": saved_account_id,
                "email": account.email,
            }
        except (BrowserWorkerTimeout, TimeoutError) as exc:
            slot_cancel.set()
            with timed_out_lock:
                timed_out_count += 1
            error = f"单号超时 ({account_timeout}s): {exc}"
            code = _metrics(False, error) or RegistrationErrorCode.DEADLINE_EXCEEDED
            logger.record_error(error)
            logger.log(
                error,
                level="error",
                event_type=RegistrationEventKind.RESULT.value,
                detail={
                    "kind": "result",
                    "status": RegistrationAttemptStatus.TIMED_OUT.value,
                    "error_code": code.value,
                },
            )
            if slot_proxy:
                proxy_pool.report_fail(slot_proxy)
            registration_attempts.finish(
                context.attempt_id,
                status=RegistrationAttemptStatus.TIMED_OUT,
                error_code=code,
                error_stage=context.stage,
                error_message=error,
                metadata=_timing_metadata(),
            )
            return error
        except (BrowserWorkerCancelled, CapacityTimeout, ResourceLeaseConflict) as exc:
            error = str(exc)
            code = _metrics(
                False,
                error,
                record_capacity=not capacity_already_recorded,
                record_class=not capacity_already_recorded,
            )
            logger.record_error(error)
            logger.log(
                error,
                level="warning",
                event_type=RegistrationEventKind.RESULT.value,
                detail={
                    "kind": "result",
                    "status": RegistrationAttemptStatus.CANCELLED.value if logger.is_cancel_requested() else RegistrationAttemptStatus.FAILED.value,
                    "error_code": (code or RegistrationErrorCode.RESOURCE_EXHAUSTED).value,
                },
            )
            registration_attempts.finish(
                context.attempt_id,
                status=(
                    RegistrationAttemptStatus.CANCELLED
                    if logger.is_cancel_requested()
                    else RegistrationAttemptStatus.FAILED
                ),
                error_code=code or RegistrationErrorCode.RESOURCE_EXHAUSTED,
                error_stage=context.stage,
                error_message=error,
                metadata=_timing_metadata(),
            )
            return "__cancel_requested__" if logger.is_cancel_requested() else error
        except Exception as exc:
            if slot_proxy:
                proxy_pool.report_fail(slot_proxy)
            error = str(exc)
            if slot_cancel.is_set() or time.monotonic() >= slot_deadline:
                with timed_out_lock:
                    timed_out_count += 1
                if execution_started_monotonic is None:
                    error = f"排队超时 ({queue_timeout}s): {error}"
                else:
                    error = f"单号超时 ({account_timeout}s): {error}"
            code = _metrics(
                False,
                error,
                record_capacity=not capacity_already_recorded,
                record_class=not capacity_already_recorded,
            )
            mailbox_resource_id = str(getattr(exc, "mailbox_resource_id", "") or "")
            if not mailbox_resource_id and "platform" in locals() and platform is not None:
                identity = getattr(platform, "_last_identity", None)
                failed_email = str(getattr(identity, "email", "") or "")
                if failed_email:
                    mailbox_resource_id = stable_resource_ref(failed_email.lower())
            if mailbox_resource_id and context.stage in {
                RegistrationStage.OTP_TRIGGER,
                RegistrationStage.OTP_WAIT,
                RegistrationStage.OTP_SUBMIT,
                RegistrationStage.PROFILE_CREATE,
                RegistrationStage.CALLBACK,
                RegistrationStage.SESSION_VALIDATE,
                RegistrationStage.PERSIST,
            }:
                quarantine_seconds = (
                    7 * 24 * 3600
                    if code == RegistrationErrorCode.AUTH_IDENTITY_PROVIDER_MISMATCH
                    else 24 * 3600
                )
                resource_leases.mark_resource(
                    resource_type="mailbox",
                    resource_id=mailbox_resource_id,
                    status="quarantined",
                    cooldown_seconds=quarantine_seconds,
                    metadata={"provider": mail_provider, "reason": code.value if code else ""},
                )
            logger.record_error(error)
            logger.log(
                f"注册失败: {error}",
                level="error",
                event_type=RegistrationEventKind.RESULT.value,
                detail={
                    "kind": "result",
                    "status": RegistrationAttemptStatus.FAILED.value,
                    "error_code": (code or RegistrationErrorCode.INTERNAL_ERROR).value,
                    "error_stage": context.stage.value,
                },
            )
            registration_attempts.finish(
                context.attempt_id,
                status=(
                    RegistrationAttemptStatus.TIMED_OUT
                    if slot_cancel.is_set() or time.monotonic() >= slot_deadline
                    else RegistrationAttemptStatus.FAILED
                ),
                error_code=code or RegistrationErrorCode.INTERNAL_ERROR,
                error_stage=context.stage,
                error_message=error,
                metadata=_timing_metadata(),
            )
            return error
        finally:
            if execution_started_monotonic is not None:
                with active_attempts_lock:
                    active_attempts = max(active_attempts - 1, 0)
                    logger.set_result_data(
                        {
                            "active_concurrency": active_attempts,
                            "peak_active_concurrency": peak_active_attempts,
                            "effective_concurrency": peak_active_attempts,
                        }
                    )
            lease_heartbeat_stop.set()
            if lease_heartbeat_thread is not None:
                try:
                    lease_heartbeat_thread.join(timeout=10)
                except Exception as cleanup_exc:
                    logger.log(
                        "租约心跳清理失败: "
                        f"{redact_registration_text(cleanup_exc)[:160]}",
                        level="warning",
                    )
            if egress_lease_id:
                try:
                    resource_leases.release(
                        egress_lease_id,
                        status="cooldown" if pending_egress_cooldown else "released",
                        cooldown_seconds=pending_egress_cooldown,
                    )
                except Exception as cleanup_exc:
                    logger.log(
                        "出口租约释放失败: "
                        f"{redact_registration_text(cleanup_exc)[:160]}",
                        level="warning",
                    )
            for owner_id in lease_owner_ids:
                try:
                    resource_leases.release_owner(owner_id)
                except Exception as cleanup_exc:
                    logger.log(
                        "尝试资源释放失败: "
                        f"{redact_registration_text(cleanup_exc)[:160]}",
                        level="warning",
                    )
            try:
                logger.clear_subtask()
            except Exception:
                pass

    success = 0
    errors: list[str] = []
    registered_accounts: list[dict[str, Any]] = []
    completed = 0
    try:
        # The pool is fixed, but the number of live futures follows the current
        # AIMD limit before every refill instead of staying frozen at task start.
        with ThreadPoolExecutor(max_workers=worker_capacity) as pool:
            next_index = 0
            pending: set = set()
            current_limit = concurrency
            peak_concurrency = concurrency

            def _submit_one(i: int):
                return pool.submit(_do_one, i)

            def _refresh_limit() -> int:
                nonlocal current_limit, peak_concurrency
                health = registration_capacity.health(
                    requested_mode,
                    task_egress_ref,
                    direct=task_direct,
                )
                if task_direct and int(health.get("cooldown_seconds") or 0) > 0:
                    refreshed = 0
                else:
                    refreshed = min(
                        worker_capacity,
                        _registration_concurrency(
                            payload.get("concurrency", 1),
                            count,
                            executor_type=executor_type,
                            proxy_count=proxy_count,
                            egress_ref=task_egress_ref,
                        ),
                    )
                if refreshed != current_limit:
                    logger.log(
                        f"并发上限动态调整 {current_limit} -> {refreshed}",
                        event_type=RegistrationEventKind.STATE.value,
                        detail={
                            "kind": RegistrationEventKind.STATE.value,
                            "action": "concurrency_adjust",
                            "previous_limit": current_limit,
                            "effective_limit": refreshed,
                        },
                    )
                    current_limit = refreshed
                    peak_concurrency = max(peak_concurrency, refreshed)
                    logger.set_result_data(
                        {
                            "current_concurrency_limit": current_limit,
                            "effective_concurrency": peak_active_attempts,
                            "active_concurrency": active_attempts,
                            "peak_active_concurrency": peak_active_attempts,
                            "healthy_concurrency": int(health.get("healthy_concurrency") or 1),
                            "limiting_resource": "egress_cooldown" if refreshed == 0 else "",
                            "egress_state": str(health.get("egress_state") or "closed"),
                            "cooldown_seconds": int(health.get("cooldown_seconds") or 0),
                        }
                    )
                return current_limit

            def _refill() -> None:
                nonlocal next_index
                # Never replenish a batch after the user pressed Stop.  This
                # guard is deliberately inside refill as well as at the loop
                # call sites because a running future may finish between two
                # cancellation checks.
                if logger.is_cancel_requested():
                    return
                limit = _refresh_limit()
                while next_index < count and len(pending) < limit:
                    if logger.is_cancel_requested():
                        return
                    pending.add(_submit_one(next_index))
                    next_index += 1

            _refill()

            while pending or next_index < count:
                if not pending:
                    _refill()
                    if not pending:
                        if logger.is_cancel_requested():
                            break
                        health = registration_capacity.health(
                            requested_mode,
                            task_egress_ref,
                            direct=task_direct,
                        )
                        wait_seconds = max(min(int(health.get("cooldown_seconds") or 1), 1), 1)
                        time.sleep(wait_seconds)
                        continue
                done, pending = wait(
                    pending,
                    timeout=0.25,
                    return_when=FIRST_COMPLETED,
                )
                if logger.is_cancel_requested():
                    # Futures that have not started yet must be withdrawn so
                    # cancelling a high-count task never creates a tail of
                    # late registrations.  Running futures receive the same
                    # signal through TaskLogger/BrowserProcessSupervisor and
                    # are allowed to release their leases normally.
                    for future in tuple(pending):
                        future.cancel()
                for future in done:
                    if future.cancelled():
                        continue
                    result = future.result()
                    completed += 1
                    if isinstance(result, dict):
                        success += 1
                        registered_accounts.append(result)
                    elif result != "__cancel_requested__":
                        errors.append(str(result))
                    logger.set_progress(completed, count)
                if next_index < count and not logger.is_cancel_requested():
                    _refill()
    except Exception as exc:
        logger.log(f"致命错误: {exc}", level="error")
        logger.finish(TASK_STATUS_FAILED, error=str(exc))
        return

    logger.set_result_data(
        {
            "success": success,
            "fail": len(errors),
            "timed_out": timed_out_count,
            "account_timeout_seconds": account_timeout,
            "queue_timeout_seconds": queue_timeout,
            "error_classes": dict(class_counts),
            "proxy_count": proxy_count,
            "account_ids": [item["account_id"] for item in registered_accounts],
            "accounts": registered_accounts,
            "auto_download_agent_identity": bool(
                extra.get("auto_download_agent_identity")
            ),
            "requested_concurrency": int(payload.get("concurrency", 1) or 1),
            "effective_concurrency": peak_active_attempts,
            "active_concurrency": active_attempts,
            "peak_active_concurrency": peak_active_attempts,
            "current_concurrency_limit": current_limit if 'current_limit' in locals() else concurrency,
            "healthy_concurrency": int(
                registration_capacity.health(
                    requested_mode,
                    task_egress_ref,
                    direct=task_direct,
                ).get("healthy_concurrency")
                or 1
            ),
            "limiting_resource": (
                "egress_cooldown"
                if int(
                    registration_capacity.health(
                        requested_mode,
                        task_egress_ref,
                        direct=task_direct,
                    ).get("cooldown_seconds")
                    or 0
                )
                > 0
                else ""
            ),
            "egress_state": str(
                registration_capacity.health(
                    requested_mode,
                    task_egress_ref,
                    direct=task_direct,
                ).get("egress_state")
                or "closed"
            ),
            "cooldown_seconds": int(
                registration_capacity.health(
                    requested_mode,
                    task_egress_ref,
                    direct=task_direct,
                ).get("cooldown_seconds")
                or 0
            ),
            "replacement_count": sum(
                int(item.get("replacement_count") or 0)
                for item in registration_attempts.list_for_task(str(getattr(logger, "task_id", "") or ""), limit=1000)
            ),
            "concurrency": concurrency,
            "requested_mode": requested_mode.value,
            "effective_mode": requested_mode.value,
        }
    )
    class_summary = ", ".join(f"{k}={v}" for k, v in sorted(class_counts.items())) or "—"
    logger.log(
        f"完成: 成功 {success} 个, 失败 {len(errors)} 个, "
        f"超时丢弃 {timed_out_count} 个; "
        f"错误分类: {class_summary}",
        event_type="summary",
    )
    if logger.is_cancel_requested():
        logger.finish(TASK_STATUS_CANCELLED, error="任务已取消")
        return
    final_status = (
        TASK_STATUS_FAILED
        if errors and success == 0
        else (TASK_STATUS_PARTIAL if errors else TASK_STATUS_SUCCEEDED)
    )
    logger.finish(final_status, error=errors[0] if final_status == TASK_STATUS_FAILED else "")
def _execute_account_check_all_task(payload: dict[str, Any], logger: TaskLogger) -> None:
    platform = str(payload.get("platform", "") or "")
    raw_ids = payload.get("ids") or []
    clean_ids = sorted({int(x) for x in raw_ids if str(x).strip().lstrip("-").isdigit() and int(x) > 0})
    # Backward compat: old clients sent limit=50 with no ids
    legacy_limit = payload.get("limit")
    select_all = bool(payload.get("select_all")) or (not clean_ids and legacy_limit is None)
    if not clean_ids and legacy_limit is not None and not select_all:
        # Old "limit only" path: still honor explicit limit if provided without ids
        select_all = False

    with Session(engine) as session:
        if clean_ids:
            q = select(AccountModel).where(AccountModel.id.in_(clean_ids))
            if platform:
                q = q.where(AccountModel.platform == platform)
            q = q.order_by(AccountModel.created_at.desc(), AccountModel.id.desc())
            accounts = session.exec(q).all()
        else:
            q = select(AccountModel)
            if platform:
                q = q.where(AccountModel.platform == platform)
            q = q.order_by(AccountModel.created_at.desc(), AccountModel.id.desc())
            if legacy_limit is not None and not select_all:
                q = q.limit(max(int(legacy_limit or 1), 1))
            accounts = session.exec(q).all()

    total = len(accounts)
    logger.set_progress(0, total)
    if total == 0:
        logger.set_result_data({"valid": 0, "invalid": 0, "error": 0, "total": 0})
        logger.finish(TASK_STATUS_SUCCEEDED)
        return

    logger.log(f"开始巡检 {total} 个账号")
    results = {"valid": 0, "invalid": 0, "error": 0, "total": total}
    completed = 0
    for model in accounts:
        if logger.is_cancel_requested():
            logger.finish(TASK_STATUS_CANCELLED, error="任务已取消")
            return
        try:
            valid, _ = _run_single_account_check(int(model.id or 0), logger)
            if valid:
                results["valid"] += 1
            else:
                results["invalid"] += 1
        except Exception as exc:
            results["error"] += 1
            logger.record_error(str(exc))
            logger.log(f"{model.email}: 检测异常 {exc}", level="error")
        completed += 1
        logger.set_progress(completed, total)
    logger.set_result_data(results)
    logger.finish(TASK_STATUS_SUCCEEDED)

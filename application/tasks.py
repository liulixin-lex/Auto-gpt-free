"""Task orchestration and persistence helpers."""
from __future__ import annotations

import json
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
from infrastructure.platform_runtime import PlatformRuntime

TASK_TYPE_REGISTER = "register"
TASK_TYPE_ACCOUNT_CHECK_ALL = "account_check_all"
TASK_TYPE_PLATFORM_ACTION = "platform_action"
TASK_TYPE_AUTO_OPS = "auto_ops"

TASK_STATUS_PENDING = "pending"
TASK_STATUS_CLAIMED = "claimed"
TASK_STATUS_RUNNING = "running"
TASK_STATUS_SUCCEEDED = "succeeded"
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
    if task_type == TASK_TYPE_PLATFORM_ACTION:
        account_id = int(payload.get("account_id", 0) or 0)
        if account_id > 0:
            return [f"account:{account_id}"]
    return []


def serialize_task(task: TaskModel) -> dict[str, Any]:
    result = task.get_result()
    progress_total = int(task.progress_total or 0)
    progress_current = int(task.progress_current or 0)
    return {
        "id": task.id,
        "task_id": task.id,
        "type": task.type,
        "platform": task.platform,
        "status": task.status,
        "terminal": task.status in TERMINAL_TASK_STATUSES,
        "cancellable": task.status in {TASK_STATUS_PENDING, TASK_STATUS_CLAIMED, TASK_STATUS_RUNNING, TASK_STATUS_CANCEL_REQUESTED},
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
    }


def serialize_event(event: TaskEventModel) -> dict[str, Any]:
    return {
        "id": event.id,
        "task_id": event.task_id,
        "type": event.type,
        "level": event.level,
        "message": event.message,
        "line": f"[{format_local_clock(event.created_at)}] {event.message}",
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


def create_platform_action_task(payload: dict[str, Any]) -> dict[str, Any]:
    return create_task(
        task_type=TASK_TYPE_PLATFORM_ACTION,
        platform=str(payload.get("platform", "")),
        payload=payload,
        progress_total=1,
    )


def get_task(task_id: str) -> Optional[dict[str, Any]]:
    with Session(engine) as session:
        task = session.get(TaskModel, task_id)
        return serialize_task(task) if task else None


# Rolling log retention — prevent unbounded task_events growth.
TASK_EVENT_MAX_PER_TASK = 800
TASK_EVENT_MAX_AUTO_OPS = 400
TASK_EVENT_MAX_REGISTER = 1200
TASK_EVENT_GLOBAL_MAX = 20000
TASK_EVENT_TERMINAL_KEEP = 120
TASK_EVENT_PRUNE_EVERY = 25
_prune_counters: dict[str, int] = {}
_prune_lock = threading.Lock()


def _task_event_cap(task_type: str | None = None) -> int:
    typ = str(task_type or "").strip().lower()
    if typ == TASK_TYPE_AUTO_OPS:
        return TASK_EVENT_MAX_AUTO_OPS
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
) -> list[dict[str, Any]]:
    """List events after ``since``. When ``since==0`` and ``tail``, return newest window only."""
    limit = min(max(limit, 1), 500)
    with Session(engine) as session:
        if since <= 0 and tail:
            # Newest ``limit`` rows, then chronological for UI.
            newest = session.exec(
                select(TaskEventModel)
                .where(TaskEventModel.task_id == task_id)
                .order_by(TaskEventModel.id.desc())
                .limit(limit)
            ).all()
            items = list(reversed(newest))
        else:
            q = (
                select(TaskEventModel)
                .where(TaskEventModel.task_id == task_id)
                .where(TaskEventModel.id > since)
                .order_by(TaskEventModel.id)
                .limit(limit)
            )
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


# Register tasks older than this no longer block auto replenish.
# With per-account fail-fast (~3min), a batch of N accounts should finish well under this.
REGISTER_ACTIVE_MAX_AGE_SECONDS = 12 * 60


def list_active_register_task_ids() -> list[str]:
    """Return ids of register tasks still pending/claimed/running/cancel_requested."""
    active = {TASK_STATUS_PENDING, *ACTIVE_TASK_STATUSES}
    with Session(engine) as session:
        rows = session.exec(
            select(TaskModel)
            .where(TaskModel.type == TASK_TYPE_REGISTER)
            .where(TaskModel.status.in_(list(active)))
        ).all()
        return [str(row.id) for row in rows if row and row.id]


def cancel_active_register_tasks(*, reason: str = "用户手动停止注册任务") -> dict[str, Any]:
    """Request cancel on every non-terminal register task."""
    ids = list_active_register_task_ids()
    cancelled: list[str] = []
    for task_id in ids:
        task = request_cancel(task_id)
        if task:
            cancelled.append(task_id)
            try:
                append_task_event(
                    task_id,
                    reason,
                    event_type="state",
                    level="warning",
                )
            except Exception:
                pass
    return {
        "requested": len(ids),
        "cancelled": len(cancelled),
        "task_ids": cancelled,
        "reason": reason,
    }


def has_active_register_task() -> bool:
    """True when a register task is still pending/running (blocks auto replenish spam).

    Stale running/claimed register tasks (no progress for REGISTER_ACTIVE_MAX_AGE_SECONDS)
    are force-failed so a hung OTP wait cannot permanently stall auto replenish.
    """
    active = {TASK_STATUS_PENDING, *ACTIVE_TASK_STATUSES}
    cutoff = _utcnow().timestamp() - REGISTER_ACTIVE_MAX_AGE_SECONDS
    stale_ids: list[str] = []
    with Session(engine) as session:
        rows = session.exec(
            select(TaskModel)
            .where(TaskModel.type == TASK_TYPE_REGISTER)
            .where(TaskModel.status.in_(list(active)))
        ).all()
        live = False
        for row in rows:
            anchor = row.started_at or row.created_at
            try:
                ts = anchor.timestamp() if anchor.tzinfo else anchor.replace(tzinfo=timezone.utc).timestamp()
            except Exception:
                ts = 0.0
            if ts and ts < cutoff and row.status != TASK_STATUS_PENDING:
                row.status = TASK_STATUS_FAILED
                row.error = row.error or "注册任务超时未完成，已自动结束以解除补号阻塞"
                row.finished_at = _utcnow()
                row.updated_at = _utcnow()
                session.add(row)
                stale_ids.append(row.id)
            else:
                live = True
        if stale_ids:
            session.commit()
    for task_id in stale_ids:
        append_task_event(
            task_id,
            "注册任务超时未完成，已自动结束以解除补号阻塞",
            event_type="state",
            level="warning",
        )
    return live


def create_running_task(
    *,
    task_type: str,
    platform: str = "chatgpt",
    payload: dict[str, Any] | None = None,
    progress_total: int = 1,
) -> dict[str, Any]:
    """Create a task already in running state (used by auto-ops logging)."""
    task_id = f"task_{int(time.time() * 1000)}_{uuid.uuid4().hex[:6]}"
    now = _utcnow()
    task = TaskModel(
        id=task_id,
        type=task_type,
        platform=platform,
        status=TASK_STATUS_RUNNING,
        payload_json=_dump_json(payload or {}),
        result_json=_dump_json(_task_result_seed()),
        progress_current=0,
        progress_total=max(int(progress_total or 0), 0),
        started_at=now,
    )
    with Session(engine) as session:
        session.add(task)
        session.commit()
        session.refresh(task)
    append_task_event(task.id, f"任务已创建: {task_type}", event_type="state")
    return serialize_task(task)


def get_or_create_persistent_task(
    *,
    task_type: str,
    platform: str = "chatgpt",
    session_key: str = "",
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Reuse one long-lived running task (e.g. auto_ops session log window)."""
    from core.config_store import config_store

    key = session_key or f"persistent_task_{task_type}"
    existing_id = str(config_store.get(key, "") or "").strip()
    if existing_id:
        with Session(engine) as session:
            task = session.get(TaskModel, existing_id)
            if task and task.status in {
                TASK_STATUS_RUNNING,
                TASK_STATUS_CLAIMED,
                TASK_STATUS_PENDING,
            }:
                return serialize_task(task)
            # revive finished session task so UI keeps one window
            if task and task.type == task_type:
                task.status = TASK_STATUS_RUNNING
                task.finished_at = None
                task.error = ""
                task.updated_at = _utcnow()
                if not task.started_at:
                    task.started_at = _utcnow()
                session.add(task)
                session.commit()
                session.refresh(task)
                append_task_event(
                    task.id,
                    "会话任务已恢复，继续记录运维日志",
                    event_type="state",
                )
                return serialize_task(task)
    created = create_running_task(
        task_type=task_type,
        platform=platform,
        payload=payload or {"session": True},
        progress_total=0,
    )
    config_store.set(key, str(created.get("id") or ""))
    return created


def append_task_event(task_id: str, message: str, *, event_type: str = "log", level: str = "info", detail: dict | None = None) -> dict[str, Any]:
    with Session(engine) as session:
        event = TaskEventModel(
            task_id=task_id,
            type=event_type,
            level=level,
            message=message,
            detail_json=_dump_json(detail or {}),
        )
        session.add(event)
        session.commit()
        session.refresh(event)
        serialized = serialize_event(event)

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
    append_task_event(task_id, "已请求取消任务", event_type="state", level="warning")
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

    def clear_subtask(self) -> None:
        try:
            del self._tlocal.subtask_id
        except AttributeError:
            pass
        try:
            del self._tlocal.subtask_label
        except AttributeError:
            pass

    def _current_subtask(self) -> tuple[str, str]:
        sid = getattr(self._tlocal, "subtask_id", "") or ""
        label = getattr(self._tlocal, "subtask_label", "") or ""
        return sid, label

    def log(self, message: str, *, level: str = "info", event_type: str = "log", detail: dict | None = None) -> None:
        # 自动给当前线程绑定的 subtask 加 detail，用于前端按 worker 分组折叠
        merged_detail = dict(detail or {})
        sid, slabel = self._current_subtask()
        if sid and "subtask_id" not in merged_detail:
            merged_detail["subtask_id"] = sid
        if slabel and "subtask_label" not in merged_detail:
            merged_detail["subtask_label"] = slabel
        append_task_event(
            self.task_id,
            message,
            event_type=event_type,
            level=level,
            detail=merged_detail or None,
        )
        prefix = f"[task:{self.task_id}]"
        if sid:
            prefix += f"[{sid}]"
        print(f"{prefix} {message}")

    def mark_running(self) -> None:
        def _update(task: TaskModel) -> None:
            task.status = TASK_STATUS_RUNNING
            task.started_at = task.started_at or _utcnow()

        _mutate_task(self.task_id, _update)
        self.log("任务已开始执行", event_type="state")

    def is_cancel_requested(self) -> bool:
        with Session(engine) as session:
            task = session.get(TaskModel, self.task_id)
            return bool(task and task.status == TASK_STATUS_CANCEL_REQUESTED)

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

    def set_result_data(self, data: Any) -> None:
        def _update(task: TaskModel) -> None:
            result = task.get_result()
            result["data"] = data
            task.set_result(result)

        _mutate_task(self.task_id, _update)

    def finish(self, status: str, *, error: str = "") -> None:
        def _update(task: TaskModel) -> None:
            task.status = status
            task.finished_at = _utcnow()
            if error:
                task.error = error

        _mutate_task(self.task_id, _update)
        event_level = "error" if status == TASK_STATUS_FAILED else ("warning" if status in {TASK_STATUS_INTERRUPTED, TASK_STATUS_CANCELLED} else "info")
        self.log(
            f"任务结束: {status}",
            level=event_level,
            event_type="state",
            detail={"status": status, "error": error},
        )


def _build_platform_instance(platform_name: str, payload: dict[str, Any], logger: TaskLogger, resolved_proxy: str | None = None, shared_mailbox=None):
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
    mailbox = shared_mailbox
    if mailbox is None and identity_provider == "mailbox":
        if not extra.get("mail_provider"):
            from infrastructure.provider_settings_repository import ProviderSettingsRepository

            extra["mail_provider"] = ProviderSettingsRepository().get_default_provider_key("mailbox")
        mailbox = create_mailbox(
            provider=extra.get("mail_provider", ""),
            extra=extra,
            proxy=resolved_proxy,
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
    logger.mark_running()

    if logger.is_cancel_requested():
        logger.finish(TASK_STATUS_CANCELLED, error="任务在启动后立即被取消")
        return

    handlers: dict[str, Callable[[dict[str, Any], TaskLogger], None]] = {
        TASK_TYPE_REGISTER: _execute_register_task,
        TASK_TYPE_ACCOUNT_CHECK_ALL: _execute_account_check_all_task,
        TASK_TYPE_PLATFORM_ACTION: _execute_platform_action_task,
    }
    handler = handlers.get(task_type)
    if not handler:
        logger.finish(TASK_STATUS_FAILED, error=f"未知任务类型: {task_type}")
        return
    handler(payload, logger)


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
    pool_proxy = None
    if allow_pool:
        try:
            pool_proxy = proxy_getter()
        except Exception:
            pool_proxy = None
    try:
        from core.proxy_runtime import resolve_registration_proxy

        return resolve_registration_proxy(
            explicit_proxy=explicit_proxy,
            pool_proxy=pool_proxy if allow_pool else None,
        )
    except Exception:
        normalized = str(explicit_proxy or "").strip() or None
        return normalized or (pool_proxy if allow_pool else None)


def _registration_concurrency(requested: Any, count: int, *, proxy_count: int | None = None) -> int:
    base = min(
        max(int(requested or 1), 1),
        max(int(count or 1), 1),
        MAX_REGISTER_CONCURRENCY,
    )
    if proxy_count is None:
        return base
    # Direct mode keeps the user-requested concurrency (proxy optional).
    # With proxies, do not exceed available exits.
    if proxy_count <= 0:
        return base
    return min(base, max(int(proxy_count), 1), MAX_REGISTER_CONCURRENCY)


def _proxy_host(url: str | None) -> str:
    raw = str(url or "").strip()
    if not raw:
        return "direct"
    try:
        from urllib.parse import urlparse

        return urlparse(raw).hostname or raw[:48]
    except Exception:
        return raw[:48]


def _inter_account_jitter_seconds(extra: dict[str, Any], *, has_proxy: bool) -> float:
    """Random delay between accounts on the same worker (anti-risk pacing)."""
    if extra.get("disable_register_jitter"):
        return 0.0
    try:
        lo = float(extra.get("register_jitter_min_seconds") or (3 if has_proxy else 8))
        hi = float(extra.get("register_jitter_max_seconds") or (12 if has_proxy else 28))
    except Exception:
        lo, hi = (3.0, 12.0) if has_proxy else (8.0, 28.0)
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
    from platforms.chatgpt.browser_profiles import pick_chrome_profile

    count = max(int(payload.get("count", 1) or 1), 1)
    platform_name = "chatgpt"
    email = payload.get("email") or None
    password = payload.get("password") or None
    explicit_proxy = str(payload.get("proxy") or "").strip() or None
    extra = dict(payload.get("extra") or {})
    account_timeout = _registration_account_timeout(payload, extra)
    # Cap OTP wait inside each account so it never exceeds the slot budget.
    otp_cap = max(30, min(int(extra.get("otp_timeout") or 90), account_timeout - 15))
    extra.setdefault("otp_timeout", otp_cap)
    executor_type = str(payload.get("executor_type") or extra.get("executor_type") or "protocol")
    allow_browser_fallback = bool(extra.get("browser_fallback_on_cf", True))

    proxy_count = 0
    try:
        proxy_count = int(proxy_pool.count_available())
    except Exception:
        proxy_count = 0
    if explicit_proxy:
        proxy_count = max(proxy_count, 1)
    concurrency = _registration_concurrency(
        payload.get("concurrency", 1),
        count,
        proxy_count=proxy_count if executor_type == "protocol" else None,
    )

    logger.set_progress(0, count)
    logger.log(
        f"注册并发 workers={concurrency} count={count} proxies≈{proxy_count} "
        f"executor={executor_type} account_timeout={account_timeout}s otp_cap={otp_cap}s "
        f"(每号独立 session/指纹/代理；超时丢弃并接下一个)"
    )
    try:
        get(platform_name)
    except Exception as exc:
        logger.log(f"致命错误: {exc}", level="error")
        logger.finish(TASK_STATUS_FAILED, error=str(exc))
        return

    # Shared mailbox pool object is OK (thread-safe providers); each register still
    # calls get_email() for a fresh account. Proxy is applied per-slot below.
    shared_mailbox = None
    try:
        from core.base_identity import normalize_identity_provider
        from core.base_mailbox import create_mailbox

        identity_provider = normalize_identity_provider(extra.get("identity_provider", "mailbox"))
        if identity_provider == "mailbox":
            if not extra.get("mail_provider"):
                from infrastructure.provider_settings_repository import ProviderSettingsRepository

                extra["mail_provider"] = ProviderSettingsRepository().get_default_provider_key("mailbox")
            shared_mailbox = create_mailbox(
                provider=extra.get("mail_provider", ""),
                extra=extra,
                proxy=explicit_proxy or None,
            )
    except Exception as exc:
        logger.log(f"邮箱初始化失败: {exc}", level="error")
        logger.finish(TASK_STATUS_FAILED, error=f"邮箱初始化失败: {exc}")
        return

    upload_lock = threading.Lock()
    uploaded_count = 0
    upload_failures: list[str] = []
    timed_out_count = 0
    timed_out_lock = threading.Lock()
    class_counts: dict[str, int] = {}
    class_lock = threading.Lock()
    # Serialize slot starts slightly so workers do not burst the same IP/gateway.
    start_gate = threading.Lock()
    last_start_ts = {"t": 0.0}

    def _do_one(index: int) -> dict[str, Any] | str:
        nonlocal uploaded_count, timed_out_count
        if logger.is_cancel_requested():
            return "__cancel_requested__"
        logger.set_subtask(f"worker_{index + 1}", f"Worker {index + 1}")
        slot_deadline = time.monotonic() + account_timeout
        slot_cancel = {"flag": False}
        slot_proxy = _resolve_registration_proxy_for_platform(
            platform_name,
            explicit_proxy=explicit_proxy,
            proxy_getter=proxy_pool.get_next,
            allow_pool=True,
        )
        profile = pick_chrome_profile()
        slot_executor = executor_type
        mail_provider = str(extra.get("mail_provider") or "")

        def _slot_cancel_check() -> bool:
            if logger.is_cancel_requested():
                return True
            if time.monotonic() >= slot_deadline:
                slot_cancel["flag"] = True
                return True
            return bool(slot_cancel["flag"])

        def _metrics(ok: bool, err: str = "", profile_key: str = "") -> None:
            cls = "" if ok else classify_register_error(err)
            if cls:
                with class_lock:
                    class_counts[cls] = class_counts.get(cls, 0) + 1
            try:
                record_register_attempt(
                    ok=ok,
                    executor=slot_executor,
                    proxy_host=_proxy_host(slot_proxy),
                    mail_provider=mail_provider,
                    profile_key=profile_key or str(profile.get("key") or ""),
                    error=err,
                    error_class=cls,
                )
            except Exception:
                pass

        try:
            # Inter-account jitter (P1-8): avoid same-worker stampede.
            with start_gate:
                gap = _inter_account_jitter_seconds(extra, has_proxy=bool(slot_proxy))
                now = time.monotonic()
                wait = max(0.0, (last_start_ts["t"] + gap) - now)
                if wait > 0 and not logger.is_cancel_requested():
                    logger.log(f"节奏抖动 {wait:.1f}s 后启动 worker_{index + 1}")
                    time.sleep(min(wait, 60.0))
                last_start_ts["t"] = time.monotonic()

            # Optional light proxy health probe (P2-11) — only when proxy present.
            if slot_proxy and bool(extra.get("proxy_preflight", True)):
                try:
                    probe = proxy_pool.probe_chatgpt(slot_proxy, timeout=10.0)
                    if not probe.get("ok"):
                        logger.log(
                            f"代理预检失败 {_proxy_host(slot_proxy)}: {probe.get('error') or probe.get('status_code')}，换下一个代理",
                            level="warning",
                        )
                        proxy_pool.report_fail(slot_proxy)
                        alt = proxy_pool.get_next()
                        if alt and alt != slot_proxy:
                            slot_proxy = alt
                            probe2 = proxy_pool.probe_chatgpt(slot_proxy, timeout=10.0)
                            if not probe2.get("ok"):
                                raise RuntimeError(
                                    f"代理不可用(chatgpt.com): {probe2.get('error') or probe2.get('status_code')}"
                                )
                except RuntimeError:
                    raise
                except Exception as probe_exc:
                    logger.log(f"代理预检异常: {probe_exc}", level="warning")

            slot_extra = {
                **extra,
                "browser_profile": profile,
                "sentinel_browser_runtime": True,
            }
            slot_payload = {
                **payload,
                "executor_type": slot_executor,
                "extra": slot_extra,
            }
            platform = _build_platform_instance(
                platform_name,
                slot_payload,
                logger,
                resolved_proxy=slot_proxy,
                shared_mailbox=shared_mailbox,
            )
            if hasattr(platform, "set_cancel_checker"):
                platform.set_cancel_checker(_slot_cancel_check)
            logger.log(
                f"开始注册第 {index + 1}/{count} 个账号 "
                f"(slot≤{account_timeout}s profile={profile.get('key')} proxy={_proxy_host(slot_proxy)})"
            )
            if slot_proxy:
                logger.log(f"使用代理: {slot_proxy}")

            result_box: dict[str, Any] = {}
            error_box: dict[str, Any] = {}

            def _register_body(plat) -> None:
                try:
                    result_box["account"] = plat.register(email=email, password=password)
                except Exception as exc:  # noqa: BLE001 — per-account isolation
                    error_box["exc"] = exc

            worker = threading.Thread(
                target=_register_body,
                args=(platform,),
                name=f"register-slot-{index + 1}",
                daemon=True,
            )
            worker.start()
            worker.join(timeout=account_timeout + 2)
            if worker.is_alive() or slot_cancel["flag"] or time.monotonic() >= slot_deadline:
                slot_cancel["flag"] = True
                with timed_out_lock:
                    timed_out_count += 1
                msg = f"单号超时丢弃 ({account_timeout}s)，换下一个"
                logger.record_error(msg)
                logger.log(msg, level="warning")
                _metrics(False, msg, str(profile.get("key") or ""))
                if slot_proxy:
                    proxy_pool.report_fail(slot_proxy)
                return msg
            if "exc" in error_box:
                first_err = error_box["exc"]
                err_text = str(first_err)
                err_class = classify_register_error(err_text)
                # P2-13: protocol CF/network → one browser fallback attempt.
                if (
                    slot_executor == "protocol"
                    and allow_browser_fallback
                    and err_class in {"network_cf", "network"}
                    and not slot_cancel["flag"]
                    and time.monotonic() < slot_deadline - 20
                ):
                    logger.log(
                        f"协议失败({err_class})，尝试浏览器后备: {err_text[:120]}",
                        level="warning",
                    )
                    slot_executor = "headless"
                    fb_extra = {**slot_extra, "browser_profile": pick_chrome_profile()}
                    fb_payload = {**slot_payload, "executor_type": "headless", "extra": fb_extra}
                    fb_platform = _build_platform_instance(
                        platform_name,
                        fb_payload,
                        logger,
                        resolved_proxy=slot_proxy,
                        shared_mailbox=shared_mailbox,
                    )
                    if hasattr(fb_platform, "set_cancel_checker"):
                        fb_platform.set_cancel_checker(_slot_cancel_check)
                    error_box.clear()
                    result_box.clear()
                    fb_worker = threading.Thread(
                        target=_register_body,
                        args=(fb_platform,),
                        name=f"register-slot-{index + 1}-browser",
                        daemon=True,
                    )
                    remain = max(15.0, slot_deadline - time.monotonic())
                    fb_worker.start()
                    fb_worker.join(timeout=remain)
                    if fb_worker.is_alive() or slot_cancel["flag"]:
                        slot_cancel["flag"] = True
                        msg = f"浏览器后备超时丢弃，换下一个"
                        logger.record_error(msg)
                        logger.log(msg, level="warning")
                        _metrics(False, msg, str(fb_extra.get("browser_profile", {}).get("key") or ""))
                        if slot_proxy:
                            proxy_pool.report_fail(slot_proxy)
                        return msg
                    if "exc" in error_box:
                        raise error_box["exc"]
                else:
                    raise first_err
            account = result_box.get("account")
            if account is None:
                raise RuntimeError("注册未返回账号")

            # P2-10: require usable token before counting success.
            token = ""
            try:
                token = str((account.extra or {}).get("access_token") or account.token or "")
            except Exception:
                token = ""
            if not token:
                raise RuntimeError("注册完成但缺少 access_token，不计成功")

            saved_account = save_account(account)
            saved_account_id = int(saved_account.id)
            if slot_proxy:
                proxy_pool.report_success(slot_proxy)
            logger.record_success()
            logger.log(f"注册成功: {account.email}")
            _metrics(True, profile_key=str(profile.get("key") or ""))
            try:
                from application.auto_ops import upload_after_register

                logger.log(f"即时上传 {account.email} → 远程号池")
                outs = upload_after_register([saved_account_id], log_fn=logger.log)
                ok_upload = bool(outs and outs[0].get("ok"))
                with upload_lock:
                    if ok_upload:
                        uploaded_count += 1
                    else:
                        err = (outs[0].get("error") if outs else "upload failed") or "upload failed"
                        upload_failures.append(str(err))
            except Exception as upload_exc:
                with upload_lock:
                    upload_failures.append(str(upload_exc))
                logger.log(
                    f"即时上传异常 account_id={saved_account_id}: {upload_exc}",
                    level="warning",
                )
            return {
                "account_id": saved_account_id,
                "email": account.email,
            }
        except Exception as exc:
            if slot_proxy:
                proxy_pool.report_fail(slot_proxy)
            error = str(exc)
            if slot_cancel["flag"] or "任务已取消" in error:
                with timed_out_lock:
                    timed_out_count += 1
                error = f"单号超时丢弃 ({account_timeout}s): {error}"
            logger.record_error(error)
            logger.log(f"注册失败: {error}", level="error")
            _metrics(False, error, str(profile.get("key") or ""))
            return error
        finally:
            logger.clear_subtask()

    success = 0
    errors: list[str] = []
    registered_accounts: list[dict[str, Any]] = []
    completed = 0
    try:
        # Fixed-size worker pool + work queue: free slot immediately takes next account.
        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            next_index = 0
            pending: set = set()

            def _submit_one(i: int):
                return pool.submit(_do_one, i)

            while next_index < count and len(pending) < concurrency:
                pending.add(_submit_one(next_index))
                next_index += 1

            while pending:
                done, pending = wait(pending, return_when=FIRST_COMPLETED)
                for future in done:
                    result = future.result()
                    completed += 1
                    if isinstance(result, dict):
                        success += 1
                        registered_accounts.append(result)
                    elif result != "__cancel_requested__":
                        errors.append(str(result))
                    logger.set_progress(completed, count)
                    # Keep pool saturated until batch quota is exhausted.
                    if next_index < count and not logger.is_cancel_requested():
                        pending.add(_submit_one(next_index))
                        next_index += 1
    except Exception as exc:
        logger.log(f"致命错误: {exc}", level="error")
        logger.finish(TASK_STATUS_FAILED, error=str(exc))
        return

    # Safety-net re-upload for any account that may have missed the per-account push.
    upload_results: list[dict[str, Any]] = []
    if registered_accounts:
        try:
            from application.auto_ops import upload_after_register

            logger.log("注册批次结束，复核自动上传…")
            upload_results = upload_after_register(
                [int(item["account_id"]) for item in registered_accounts],
                log_fn=logger.log,
            )
        except Exception as exc:
            logger.log(f"自动上传阶段异常: {exc}", level="warning")

    logger.set_result_data(
        {
            "success": success,
            "fail": len(errors),
            "timed_out": timed_out_count,
            "account_timeout_seconds": account_timeout,
            "error_classes": dict(class_counts),
            "proxy_count": proxy_count,
            "account_ids": [item["account_id"] for item in registered_accounts],
            "accounts": registered_accounts,
            "auto_download_agent_identity": bool(
                extra.get("auto_download_agent_identity")
            ),
            "auto_upload_results": upload_results,
            "sub2api_agent_identity_upload": {
                "submitted": uploaded_count,
                "failed": len(upload_failures),
                "errors": upload_failures[:20],
            },
            "concurrency": concurrency,
        }
    )
    class_summary = ", ".join(f"{k}={v}" for k, v in sorted(class_counts.items())) or "—"
    logger.log(
        f"完成: 成功 {success} 个, 失败 {len(errors)} 个, "
        f"超时丢弃 {timed_out_count} 个, "
        f"即时上传成功 {uploaded_count}, 上传失败 {len(upload_failures)}; "
        f"错误分类: {class_summary}",
        event_type="summary",
    )
    if logger.is_cancel_requested():
        logger.finish(TASK_STATUS_CANCELLED, error="任务已取消")
        return
    final_status = TASK_STATUS_FAILED if errors and success == 0 else TASK_STATUS_SUCCEEDED
    logger.finish(final_status, error=errors[0] if final_status == TASK_STATUS_FAILED else "")
    # Feed auto-ops fail-streak: network/CF failures back off harder than OTP.
    try:
        from core.config_store import config_store

        if str(extra.get("triggered_by") or "") == "auto_ops":
            if success > 0:
                config_store.set("auto_ops_register_fail_streak", "0")
            else:
                prev = 0
                try:
                    prev = int(str(config_store.get("auto_ops_register_fail_streak", "0") or "0"))
                except Exception:
                    prev = 0
                bump = 1
                if class_counts.get("network_cf") or class_counts.get("network"):
                    bump = 2
                elif class_counts.get("sentinel"):
                    bump = 2
                config_store.set("auto_ops_register_fail_streak", str(min(prev + bump, 8)))
                # Persist last dominant error class for ops UI.
                dominant = ""
                if class_counts:
                    dominant = max(class_counts.items(), key=lambda kv: kv[1])[0]
                if dominant:
                    config_store.set("auto_ops_last_register_error_class", dominant)
    except Exception:
        pass


def _execute_platform_action_task(payload: dict[str, Any], logger: TaskLogger) -> None:
    if logger.is_cancel_requested():
        logger.finish(TASK_STATUS_CANCELLED, error="任务已取消")
        return
    command_platform = str(payload.get("platform", ""))
    account_id = int(payload.get("account_id", 0) or 0)
    action_id = str(payload.get("action_id", ""))
    params = dict(payload.get("params") or {})
    runtime = PlatformRuntime()
    result = runtime.execute_action(
        type("Command", (), {
            "platform": command_platform,
            "account_id": account_id,
            "action_id": action_id,
            "params": params,
        })(),
        log_fn=logger.log,
        cancel_check=logger.is_cancel_requested,
    )
    if logger.is_cancel_requested() or str(result.error or "") == "任务已取消":
        logger.finish(TASK_STATUS_CANCELLED, error="任务已取消")
        return
    if not result.ok:
        logger.record_error(result.error)
        logger.finish(TASK_STATUS_FAILED, error=result.error)
        return
    logger.set_result_data(result.data)
    message = ""
    if isinstance(result.data, dict):
        message = str(result.data.get("message", "") or "")
    if message:
        logger.log(message, event_type="summary")
    logger.set_progress(1, 1)
    logger.finish(TASK_STATUS_SUCCEEDED)


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

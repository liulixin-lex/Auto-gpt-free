from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from application.tasks import list_tasks
from application.registration_queries import registration_queries
from application.tasks_query import TasksQueryService

router = APIRouter(prefix="/tasks", tags=["tasks"])
service = TasksQueryService()


@router.get("")
def list_all_tasks(
    limit: int = Query(default=40, ge=1, le=200),
    type: str = Query(default="", description="comma-separated task types"),
):
    types = [t.strip() for t in (type or "").split(",") if t.strip()]
    items = list_tasks(limit=limit, types=types or None)
    return {"items": items, "total": len(items)}


@router.get("/{task_id}")
def get_task(task_id: str):
    task = service.get_task(task_id)
    if not task:
        raise HTTPException(404, "任务不存在")
    return task


@router.get("/{task_id}/events")
def list_task_events(
    task_id: str,
    since: int = 0,
    limit: int = 200,
    attempt_id: str = "",
    stage: str = "",
    level: str = "",
    error_code: str = "",
):
    task = service.get_task(task_id)
    if not task:
        raise HTTPException(404, "任务不存在")
    return service.list_events(
        task_id,
        since=since,
        limit=limit,
        attempt_id=attempt_id,
        stage=stage,
        level=level,
        error_code=error_code,
    )


@router.get("/{task_id}/summary")
def get_registration_summary(task_id: str):
    task = service.get_task(task_id)
    if not task:
        raise HTTPException(404, "任务不存在")
    return registration_queries.summary(task_id)


@router.get("/{task_id}/attempts")
def list_registration_attempts(
    task_id: str,
    status: str = "",
    mode: str = "",
    stage: str = "",
    error_code: str = "",
    limit: int = Query(default=200, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
):
    task = service.get_task(task_id)
    if not task:
        raise HTTPException(404, "任务不存在")
    return registration_queries.list_attempts(
        task_id,
        status=status,
        mode=mode,
        stage=stage,
        error_code=error_code,
        limit=limit,
        offset=offset,
    )


@router.get("/{task_id}/artifacts")
def list_registration_artifacts(
    task_id: str,
    attempt_id: str = "",
    limit: int = Query(default=200, ge=1, le=1000),
):
    task = service.get_task(task_id)
    if not task:
        raise HTTPException(404, "任务不存在")
    from infrastructure.registration_repository import registration_artifacts

    items = registration_artifacts.list_for_task(
        task_id,
        attempt_id=attempt_id,
        limit=limit,
    )
    return {"items": items, "total": len(items)}

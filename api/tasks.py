from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from application.tasks import list_tasks
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
def list_task_events(task_id: str, since: int = 0, limit: int = 200):
    task = service.get_task(task_id)
    if not task:
        raise HTTPException(404, "任务不存在")
    return service.list_events(task_id, since=since, limit=limit)

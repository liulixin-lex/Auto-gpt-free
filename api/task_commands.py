from __future__ import annotations

from typing import Literal, Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from application.task_commands import TaskCommandsService
from application.tasks import _registration_concurrency
from application.tasks_query import TasksQueryService

router = APIRouter(prefix="/tasks", tags=["task-commands"])
command_service = TaskCommandsService()
query_service = TasksQueryService()


class RegisterTaskRequest(BaseModel):
    email: Optional[str] = None
    password: Optional[str] = None
    count: int = 1
    concurrency: int = 1
    proxy: Optional[str] = None
    executor_type: Literal["protocol", "headless", "headed"] = "headless"
    captcha_solver: str = "auto"
    extra: dict = Field(default_factory=dict)


@router.post("/register")
def create_register_task(body: RegisterTaskRequest):
    payload = body.model_dump()
    extra = dict(body.extra or {})
    extra.pop("browser_fallback_on_cf", None)
    extra["strict_executor_mode"] = True
    extra["identity_provider"] = "mailbox"
    mail_provider = str(extra.get("mail_provider") or "").strip()
    if body.executor_type == "protocol":
        # Protocol accepts any mailbox provider that implements BaseMailbox.
        pool_text = str(extra.get("local_ms_pool_text") or "").strip()
        pool_file = str(extra.get("local_ms_pool_file") or "").strip()
        # Infer Outlook pool when only pool text/file is provided (legacy clients).
        if not mail_provider and (pool_text or pool_file):
            mail_provider = "local_ms_pool"
        if not mail_provider:
            from infrastructure.provider_settings_repository import ProviderSettingsRepository

            mail_provider = (
                ProviderSettingsRepository().get_default_provider_key("mailbox") or ""
            )
        if not mail_provider:
            raise HTTPException(
                400,
                "协议注册需要选择邮箱服务（如 Cloud Mail），或提供 Outlook 账号池",
            )

        if mail_provider == "local_ms_pool":
            if not pool_text and not pool_file:
                raise HTTPException(400, "使用 Outlook 池时需填写账号池文本或文件")
            from core.local_ms_mailbox import MAX_OUTLOOK_SUBADDRESS_COUNT

            extra["local_ms_pool_alias_count"] = MAX_OUTLOOK_SUBADDRESS_COUNT
            if pool_text:
                from core.local_ms_mailbox import parse_local_ms_pool_rows

                rows = parse_local_ms_pool_rows(pool_text)
                if not rows:
                    raise HTTPException(400, "Outlook 账号池未解析到有效账号，请检查输入格式")
                allow_reuse = str(extra.get("local_ms_pool_allow_reuse") or "").strip().lower() in {
                    "1", "true", "yes", "on"
                }
                capacity = len(rows) * MAX_OUTLOOK_SUBADDRESS_COUNT
                if not allow_reuse and capacity < body.count:
                    raise HTTPException(
                        400,
                        f"Outlook 子邮箱容量 {capacity} 少于注册数量 {body.count}"
                        f"（每个母邮箱最多 {MAX_OUTLOOK_SUBADDRESS_COUNT} 个）",
                    )
        extra["mail_provider"] = mail_provider
    # Cap concurrency at backend contract (UI may send higher).
    payload["concurrency"] = min(max(int(body.concurrency or 1), 1), 20)
    payload["extra"] = extra
    if mail_provider:
        extra["mail_provider"] = mail_provider
    proxy_count = 1 if body.proxy else 0
    if not body.proxy:
        try:
            from core.proxy_pool import proxy_pool

            proxy_count = int(proxy_pool.count_available())
        except Exception:
            proxy_count = 0
    effective_concurrency = _registration_concurrency(
        payload["concurrency"],
        body.count,
        executor_type=body.executor_type,
        proxy_count=proxy_count,
    )
    payload["requested_concurrency"] = int(body.concurrency or 1)
    payload["effective_concurrency"] = effective_concurrency
    task = command_service.create_register_task(payload)
    task["requested_concurrency"] = int(body.concurrency or 1)
    task["effective_concurrency"] = effective_concurrency
    return task


@router.post("/{task_id}/cancel")
def cancel_task(task_id: str):
    task = command_service.cancel_task(task_id)
    if not task:
        raise HTTPException(404, "任务不存在")
    return task


@router.get("/{task_id}/logs/stream")
async def stream_logs(task_id: str, since: int = 0):
    if not query_service.get_task(task_id):
        raise HTTPException(404, "任务不存在")
    return StreamingResponse(
        command_service.stream_task_events(task_id, since=since),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )

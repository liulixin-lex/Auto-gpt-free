from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from application.account_checks import AccountChecksService

router = APIRouter(prefix="/accounts", tags=["account-checks"])
service = AccountChecksService()


class BatchCheckRequest(BaseModel):
    platform: Literal["chatgpt"] = "chatgpt"
    ids: list[int] = Field(default_factory=list)
    # When true (or ids empty), check every account on the platform — no fixed 50 cap.
    select_all: bool = False


@router.post("/check-all")
def check_all_accounts(
    body: BatchCheckRequest | None = None,
    platform: Literal["chatgpt"] = "chatgpt",
):
    """Batch check. Prefer JSON body; query `platform` kept for backward compat."""
    if body is None:
        return service.check_all_async(platform or "chatgpt", select_all=True)
    return service.check_all_async(
        body.platform or platform or "chatgpt",
        ids=body.ids,
        select_all=body.select_all or not body.ids,
    )


@router.post("/{account_id}/check")
def check_one_account(account_id: int):
    if account_id <= 0:
        raise HTTPException(400, "无效账号 ID")
    return service.check_one_async(account_id)

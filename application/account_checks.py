from __future__ import annotations

from application.tasks import (
    create_account_check_all_task,
    create_account_check_one_task,
)
from services.task_runtime import task_runtime


class AccountChecksService:
    def check_all_async(
        self,
        platform: str = "chatgpt",
        *,
        ids: list[int] | None = None,
        select_all: bool = False,
    ) -> dict:
        task = create_account_check_all_task(
            platform or "chatgpt",
            ids=ids,
            select_all=select_all,
        )
        task_runtime.wake_up()
        return task

    def check_one_async(self, account_id: int) -> dict:
        task = create_account_check_one_task(int(account_id))
        task_runtime.wake_up()
        return task

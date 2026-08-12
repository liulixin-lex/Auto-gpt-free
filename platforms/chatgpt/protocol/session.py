from __future__ import annotations

import time
from typing import Callable


class SessionResolver:
    def __init__(
        self,
        resolve: Callable[[], dict],
        *,
        cancel_check: Callable[[], bool] | None = None,
        attempts: int = 3,
        delay_seconds: float = 1.0,
    ) -> None:
        self.resolve = resolve
        self.cancel_check = cancel_check or (lambda: False)
        self.attempts = max(int(attempts), 1)
        self.delay_seconds = max(float(delay_seconds), 0.0)

    def run(self) -> dict:
        last_error: Exception | None = None
        for attempt in range(self.attempts):
            if self.cancel_check():
                raise RuntimeError("任务已取消")
            try:
                payload = self.resolve()
                if payload.get("access_token"):
                    return payload
                last_error = RuntimeError("session missing access token")
            except Exception as exc:
                last_error = exc
            if attempt < self.attempts - 1:
                end = time.monotonic() + self.delay_seconds
                while time.monotonic() < end:
                    if self.cancel_check():
                        raise RuntimeError("任务已取消")
                    time.sleep(min(0.2, max(end - time.monotonic(), 0.0)))
        raise RuntimeError(f"注册完成但获取 ChatGPT session 失败: {last_error}")

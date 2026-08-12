from __future__ import annotations

from email.utils import parsedate_to_datetime
import random
import time
from typing import Any, Callable

from domain.registration_runtime import IDEMPOTENT_RETRY, RetryPolicy, SIDE_EFFECT_RETRY


_RETRYABLE_STATUS = {408, 425, 429, 500, 502, 503, 504}


def _retry_after_seconds(response) -> float | None:
    raw = str(getattr(response, "headers", {}).get("retry-after") or "").strip()
    if not raw:
        return None
    try:
        return max(float(raw), 0.0)
    except ValueError:
        try:
            parsed = parsedate_to_datetime(raw)
            return max(parsed.timestamp() - time.time(), 0.0)
        except (TypeError, ValueError, OverflowError):
            return None


class ProtocolTransport:
    def __init__(
        self,
        session,
        *,
        cancel_check: Callable[[], bool] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        random_source: random.Random | None = None,
    ) -> None:
        self.session = session
        self.cancel_check = cancel_check or (lambda: False)
        self.sleep = sleep
        self.random = random_source or random.SystemRandom()

    def request(
        self,
        method: str,
        url: str,
        *,
        policy: RetryPolicy | None = None,
        side_effect: bool = False,
        operation: str = "request",
        **kwargs: Any,
    ):
        selected = policy or (SIDE_EFFECT_RETRY if side_effect else IDEMPOTENT_RETRY)
        attempts = max(int(selected.max_attempts), 1)
        last_error: BaseException | None = None
        for attempt in range(attempts):
            if self.cancel_check():
                raise RuntimeError("任务已取消")
            try:
                call = getattr(self.session, method.lower(), None)
                response = call(url, **kwargs) if callable(call) else self.session.request(method, url, **kwargs)
                status = int(getattr(response, "status_code", 0) or 0)
                if status not in _RETRYABLE_STATUS or attempt >= attempts - 1 or not selected.retryable:
                    return response
                delay = _retry_after_seconds(response) if selected.respect_retry_after else None
            except Exception as exc:
                last_error = exc
                if attempt >= attempts - 1 or not selected.retryable:
                    raise
                delay = None
            if delay is None:
                delay = selected.delay(attempt, random_value=self.random.random())
            end = time.monotonic() + min(float(delay), selected.max_delay_seconds)
            while time.monotonic() < end:
                if self.cancel_check():
                    raise RuntimeError("任务已取消")
                self.sleep(min(0.25, max(end - time.monotonic(), 0.0)))
        if last_error:
            raise last_error
        raise RuntimeError(f"{operation} exhausted retry budget")

    def get(self, url: str, **kwargs: Any):
        return self.request("GET", url, operation="GET", **kwargs)

    def post(self, url: str, *, side_effect: bool = True, **kwargs: Any):
        return self.request("POST", url, side_effect=side_effect, operation="POST", **kwargs)

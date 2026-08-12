"""Cross-process captcha provider slots backed by registration leases."""
from __future__ import annotations

from contextlib import contextmanager
import os
import time
from typing import Iterator
from uuid import uuid4

from infrastructure.registration_repository import ResourceLeaseConflict, resource_leases


def _limit(provider: str) -> int:
    key = str(provider or "default").strip().upper().replace("-", "_")
    defaults = {
        "LOCAL_SOLVER": 1,
        "YESCAPTCHA_API": 5,
        "TWOCAPTCHA_API": 5,
    }
    default = defaults.get(key, 2)
    try:
        value = int(os.getenv(f"CAPTCHA_{key}_MAX", str(default)))
    except (TypeError, ValueError):
        value = default
    return max(1, min(value, 20))


class CaptchaCapacityTimeout(RuntimeError):
    pass


class CaptchaProviderCapacity:
    @contextmanager
    def slot(
        self,
        provider: str,
        *,
        owner_attempt_id: str = "",
        timeout_seconds: float = 30.0,
        ttl_seconds: int = 240,
        cooldown_seconds: int = 60,
    ) -> Iterator[None]:
        provider_key = str(provider or "default").strip().lower() or "default"
        owner = str(owner_attempt_id or f"captcha-{uuid4().hex}")
        deadline = time.monotonic() + max(float(timeout_seconds), 0.1)
        lease = None
        while lease is None and time.monotonic() < deadline:
            for index in range(_limit(provider_key)):
                try:
                    lease = resource_leases.acquire(
                        resource_type="captcha_provider",
                        resource_id=f"{provider_key}:{index}",
                        owner_attempt_id=owner,
                        ttl_seconds=max(int(ttl_seconds), 1),
                        metadata={"provider": provider_key, "slot": index},
                    )
                    break
                except ResourceLeaseConflict:
                    continue
            if lease is None:
                time.sleep(min(0.25, max(deadline - time.monotonic(), 0.01)))
        if lease is None:
            raise CaptchaCapacityTimeout(f"captcha provider capacity exhausted: {provider_key}")
        try:
            yield
        except BaseException:
            resource_leases.release(
                lease.id,
                status="cooldown",
                cooldown_seconds=max(int(cooldown_seconds), 0),
            )
            raise
        else:
            resource_leases.release(lease.id)


captcha_provider_capacity = CaptchaProviderCapacity()


__all__ = [
    "CaptchaCapacityTimeout",
    "CaptchaProviderCapacity",
    "captcha_provider_capacity",
]

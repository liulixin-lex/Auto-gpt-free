from __future__ import annotations

from dataclasses import dataclass
import hashlib
import re
import threading
import time
from urllib.parse import urljoin


_SDK_PATTERN = re.compile(r"(?:https?:)?//[^\"']+/sentinel/[^/\"']+/sdk\.js|/sentinel/[^/\"']+/sdk\.js")


class SentinelSdkDriftError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class SentinelSdkBundle:
    url: str
    content: str
    sha256: str


class SentinelSdkResolver:
    _lock = threading.Lock()
    _cache: dict[str, SentinelSdkBundle] = {}
    _drift_until = 0.0
    _drift_hash = ""

    def __init__(self, session, *, fallback_url: str, bootstrap_url: str) -> None:
        self.session = session
        self.fallback_url = fallback_url
        self.bootstrap_url = bootstrap_url

    def discover_url(self) -> str:
        try:
            response = self.session.get(self.bootstrap_url, timeout=20)
            body = str(getattr(response, "text", "") or "")
            match = _SDK_PATTERN.search(body)
            if match:
                value = match.group(0)
                return urljoin(self.bootstrap_url, value if not value.startswith("//") else "https:" + value)
        except Exception:
            pass
        return self.fallback_url

    def load(self, *, compatibility_hook: str) -> SentinelSdkBundle:
        runtime = type(self)
        with runtime._lock:
            if time.monotonic() < runtime._drift_until:
                suffix = f" sha256={runtime._drift_hash}" if runtime._drift_hash else ""
                raise SentinelSdkDriftError(f"Sentinel SDK drift circuit is open{suffix}")
        url = self.discover_url()
        with runtime._lock:
            cached = runtime._cache.get(url)
            if cached is not None:
                return cached
        response = self.session.get(url, timeout=30)
        if int(getattr(response, "status_code", 0) or 0) >= 400:
            raise RuntimeError(f"Sentinel SDK 获取失败: HTTP {response.status_code}")
        content = str(getattr(response, "text", "") or "")
        if not content:
            raise RuntimeError("Sentinel SDK 返回为空")
        if compatibility_hook not in content:
            digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
            with runtime._lock:
                runtime._drift_hash = digest[:16]
                runtime._drift_until = time.monotonic() + 300.0
            raise SentinelSdkDriftError(f"Sentinel SDK 接口漂移 sha256={digest[:16]}")
        bundle = SentinelSdkBundle(
            url=url,
            content=content,
            sha256=hashlib.sha256(content.encode("utf-8")).hexdigest(),
        )
        with runtime._lock:
            runtime._cache[url] = bundle
            runtime._drift_until = 0.0
            runtime._drift_hash = ""
        return bundle

    @classmethod
    def report_runtime_drift(
        cls,
        sdk_hash: str,
        *,
        missing: tuple[str, ...] | list[str],
        cooldown_seconds: float = 300.0,
    ) -> None:
        """Open the process-wide circuit when the loaded SDK loses its contract."""

        names = tuple(sorted({str(item or "").strip() for item in missing if item}))
        digest = str(sdk_hash or "").strip()[:16]
        with cls._lock:
            cls._drift_hash = digest
            cls._drift_until = max(
                cls._drift_until,
                time.monotonic() + max(float(cooldown_seconds), 1.0),
            )
        detail = ",".join(names) or "unknown"
        raise SentinelSdkDriftError(
            f"Sentinel SDK runtime contract drift missing={detail} sha256={digest or '-'}"
        )

    @classmethod
    def drift_status(cls) -> dict[str, object]:
        with cls._lock:
            remaining = max(cls._drift_until - time.monotonic(), 0.0)
            return {
                "open": remaining > 0,
                "remaining_seconds": int(remaining),
                "sdk_hash": cls._drift_hash,
            }

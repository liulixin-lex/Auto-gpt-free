"""Rolling registration success metrics by executor / proxy / mailbox / error class."""
from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from typing import Any

from core.config_store import config_store

_LOCK = threading.Lock()
_KEY = "register_metrics_v1"
_MAX_BUCKETS = 80


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _load() -> dict[str, Any]:
    raw = config_store.get(_KEY, "") or ""
    if not raw:
        return {"buckets": {}, "updated_at": ""}
    try:
        data = json.loads(raw)
        if isinstance(data, dict):
            data.setdefault("buckets", {})
            return data
    except Exception:
        pass
    return {"buckets": {}, "updated_at": ""}


def _save(data: dict[str, Any]) -> None:
    data["updated_at"] = _utcnow()
    buckets = data.get("buckets") or {}
    if len(buckets) > _MAX_BUCKETS:
        # Drop coldest buckets (fewest attempts).
        ranked = sorted(
            buckets.items(),
            key=lambda kv: int((kv[1] or {}).get("attempts") or 0),
        )
        for key, _ in ranked[: max(0, len(buckets) - _MAX_BUCKETS)]:
            buckets.pop(key, None)
        data["buckets"] = buckets
    config_store.set(_KEY, json.dumps(data, ensure_ascii=False))


def classify_register_error(message: str) -> str:
    text = str(message or "").lower()
    if any(k in text for k in ("首页访问失败", "cloudflare", "cf-ray", "just a moment", "access denied", "403", "blocked")):
        return "network_cf"
    if any(k in text for k in ("timeout", "超时", "timed out", "connection", "proxy", "tls", "ssl", "reset")):
        return "network"
    if any(k in text for k in ("验证码", "otp", "cloud mail", "mailbox", "邮件")):
        return "otp"
    if "sentinel" in text or "pow" in text or "turnstile" in text:
        return "sentinel"
    if "csrf" in text:
        return "csrf"
    if "任务已取消" in text or "取消" in text:
        return "cancelled"
    if "单号超时" in text:
        return "slot_timeout"
    return "other"


def record_register_attempt(
    *,
    ok: bool,
    executor: str = "protocol",
    proxy_host: str = "",
    mail_provider: str = "",
    profile_key: str = "",
    error: str = "",
    error_class: str = "",
) -> None:
    cls = error_class or ("" if ok else classify_register_error(error))
    bucket_key = "|".join(
        [
            str(executor or "protocol"),
            str(mail_provider or "-"),
            str(profile_key or "-"),
            (proxy_host or "direct")[:48],
        ]
    )
    with _LOCK:
        data = _load()
        buckets = data.setdefault("buckets", {})
        row = buckets.get(bucket_key) or {
            "attempts": 0,
            "success": 0,
            "fail": 0,
            "by_class": {},
            "executor": executor,
            "mail_provider": mail_provider,
            "profile_key": profile_key,
            "proxy_host": proxy_host or "direct",
        }
        row["attempts"] = int(row.get("attempts") or 0) + 1
        if ok:
            row["success"] = int(row.get("success") or 0) + 1
        else:
            row["fail"] = int(row.get("fail") or 0) + 1
            by_class = row.setdefault("by_class", {})
            by_class[cls or "other"] = int(by_class.get(cls or "other") or 0) + 1
            row["last_error"] = str(error or "")[:200]
            row["last_error_class"] = cls or "other"
        row["last_at"] = _utcnow()
        buckets[bucket_key] = row
        _save(data)


def get_register_metrics(limit: int = 20) -> dict[str, Any]:
    with _LOCK:
        data = _load()
    buckets = list((data.get("buckets") or {}).values())
    buckets.sort(key=lambda r: int(r.get("attempts") or 0), reverse=True)
    total_a = sum(int(b.get("attempts") or 0) for b in buckets)
    total_s = sum(int(b.get("success") or 0) for b in buckets)
    class_totals: dict[str, int] = {}
    for b in buckets:
        for k, v in (b.get("by_class") or {}).items():
            class_totals[k] = class_totals.get(k, 0) + int(v or 0)
    return {
        "updated_at": data.get("updated_at") or "",
        "total_attempts": total_a,
        "total_success": total_s,
        "success_rate": (total_s / total_a) if total_a else 0.0,
        "error_classes": class_totals,
        "top_buckets": buckets[: max(1, min(int(limit or 20), 50))],
    }

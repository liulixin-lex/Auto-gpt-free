from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from application.auto_ops import (
    auto_ops_manager,
    get_auto_ops_status,
    get_recent_auto_ops_logs,
    run_probe_cycle,
    start_auto_probe,
    start_auto_replenish,
    stop_all_auto_ops,
    stop_auto_probe,
    stop_auto_replenish,
    sync_remote_orphans,
)
from application.remote_upload import upload_account_agent_identity
from application.tasks import cancel_active_register_tasks
from platforms.chatgpt.remote_sync import test_cpa, test_sub2api

router = APIRouter(prefix="/auto-ops", tags=["auto-ops"])


class RemoteTestRequest(BaseModel):
    target: str = Field(description="cpa | sub2api")
    cpa_api_url: str = ""
    cpa_api_key: str = ""
    sub2api_base_url: str = ""
    sub2api_token: str = ""
    sub2api_email: str = ""
    sub2api_password: str = ""


class UploadAccountRequest(BaseModel):
    account_id: int
    targets: list[str] = Field(default_factory=list)


class StopProbeRequest(BaseModel):
    cancel_registers: bool = False


class StopReplenishRequest(BaseModel):
    cancel_running: bool = True


class StopAllRequest(BaseModel):
    cancel_registers: bool = True


@router.get("/status")
def auto_ops_status():
    return get_auto_ops_status()


@router.get("/logs")
def auto_ops_logs(limit: int = 40):
    return {"items": get_recent_auto_ops_logs(limit)}


@router.get("/register-metrics")
def register_metrics(limit: int = 20):
    from application.register_metrics import get_register_metrics

    return get_register_metrics(limit=limit)


@router.post("/probe-proxy")
def probe_proxy(body: dict | None = None):
    """Light chatgpt.com probe for one proxy URL or a pool sample."""
    from core.proxy_pool import proxy_pool

    body = body or {}
    url = str((body or {}).get("url") or "").strip() or None
    if url:
        return proxy_pool.probe_chatgpt(url)
    # Sample up to 5 active proxies.
    urls = proxy_pool.list_active_urls()[:5]
    if not urls:
        return {"ok": False, "error": "no proxies", "items": []}
    items = [proxy_pool.probe_chatgpt(u) for u in urls]
    return {"ok": any(i.get("ok") for i in items), "items": items}


@router.get("/proxy-runtime")
def get_proxy_runtime():
    from core.proxy_runtime import ProxyRuntimeSettings, probe_flaresolverr

    data = ProxyRuntimeSettings.load().to_public_dict()
    if data.get("clearance_mode") == "flaresolverr":
        data["flaresolverr_probe"] = probe_flaresolverr(data.get("flaresolverr_url") or "")
    return data


@router.post("/proxy-runtime/test")
def test_proxy_runtime():
    from core.proxy_runtime import test_runtime_proxy

    return test_runtime_proxy()


@router.post("/proxy-runtime/ensure-fs")
def ensure_proxy_runtime_fs():
    """One-click: enable runtime + FlareSolverr defaults (proxy optional)."""
    from core.proxy_runtime import ensure_recommended_no_proxy_fs, probe_flaresolverr

    settings = ensure_recommended_no_proxy_fs()
    out = settings.to_public_dict()
    out["flaresolverr_probe"] = probe_flaresolverr(settings.flaresolverr_url)
    return out


@router.post("/token-keepalive")
def run_token_keepalive(body: dict | None = None):
    from application.token_survival import keepalive_chatgpt_accounts

    body = body or {}
    limit = int(body.get("limit") or 40)
    try_password = bool(body.get("try_password", True))
    return keepalive_chatgpt_accounts(limit=limit, try_password=try_password)


@router.post("/run-cycle")
def run_cycle():
    return auto_ops_manager.kick()


@router.post("/probe-now")
def probe_now():
    return run_probe_cycle(platform="chatgpt")


@router.post("/stop-probe")
def stop_probe(body: StopProbeRequest | None = None):
    """Disable auto probe; optionally cancel in-flight register tasks too."""
    body = body or StopProbeRequest()
    return stop_auto_probe(cancel_registers=bool(body.cancel_registers))


@router.post("/start-probe")
def start_probe():
    return start_auto_probe()


@router.post("/stop-replenish")
def stop_replenish(body: StopReplenishRequest | None = None):
    """Disable auto register/replenish and cancel running register tasks by default."""
    body = body or StopReplenishRequest()
    return stop_auto_replenish(cancel_running=bool(body.cancel_running))


@router.post("/start-replenish")
def start_replenish():
    return start_auto_replenish()


@router.post("/stop-register-tasks")
def stop_register_tasks_only():
    """Cancel active register tasks without changing auto-ops toggles."""
    result = cancel_active_register_tasks(reason="用户手动取消注册任务")
    return {"ok": True, "register": result}


@router.post("/stop-all")
def stop_all(body: StopAllRequest | None = None):
    """Emergency stop: probe + delete + replenish off, cancel registers."""
    body = body or StopAllRequest()
    return stop_all_auto_ops(cancel_registers=bool(body.cancel_registers))


@router.post("/sync-remote")
def sync_remote_now():
    return sync_remote_orphans()


@router.post("/test-remote")
def test_remote(body: RemoteTestRequest):
    target = (body.target or "").strip().lower()
    if target in {"cpa", "cliproxyapi", "cliproxy"}:
        ok, msg = test_cpa(api_url=body.cpa_api_url or None, api_key=body.cpa_api_key or None)
        return {"ok": ok, "message": msg, "target": "cpa"}
    if target in {"sub2api", "s2a"}:
        ok, msg = test_sub2api(
            base_url=body.sub2api_base_url or None,
            token=body.sub2api_token or None,
            email=body.sub2api_email or None,
            password=body.sub2api_password or None,
        )
        return {"ok": ok, "message": msg, "target": "sub2api"}
    raise HTTPException(400, "target 必须是 cpa 或 sub2api")


@router.post("/upload-account")
def upload_account(body: UploadAccountRequest):
    """Upload one account using target-official formats.

    CLIProxyAPI ← codex OAuth token; Sub2API ← agent_identity.
    Invalid local accounts are rejected before remote write.
    """
    targets = [t.strip().lower() for t in (body.targets or []) if t.strip()]
    result = upload_account_agent_identity(
        int(body.account_id),
        targets=targets or None,
    )
    if not result.get("ok"):
        raise HTTPException(400, result.get("error") or "上传失败")
    return result

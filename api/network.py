from __future__ import annotations

from fastapi import APIRouter

from core.proxy_runtime import (
    ProxyRuntimeSettings,
    ensure_recommended_no_proxy_fs,
    probe_flaresolverr,
    test_runtime_proxy,
)

router = APIRouter(prefix="/network", tags=["network"])


@router.get("/runtime")
def get_network_runtime():
    settings = ProxyRuntimeSettings.load()
    data = settings.to_public_dict()
    if data.get("clearance_mode") == "flaresolverr":
        data["flaresolverr_probe"] = probe_flaresolverr(
            data.get("flaresolverr_url") or ""
        )
    return data


@router.post("/runtime/test")
def test_network_runtime():
    return test_runtime_proxy()


@router.post("/runtime/ensure-fs")
def ensure_flaresolverr_runtime():
    settings = ensure_recommended_no_proxy_fs()
    data = settings.to_public_dict()
    data["flaresolverr_probe"] = probe_flaresolverr(settings.flaresolverr_url)
    return data

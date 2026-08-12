"""代理池 - 从数据库读取代理，支持轮询和按区域选取"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlparse

from sqlmodel import Session, select

from .db import ProxyModel, engine


class ProxyPool:
    def __init__(self):
        self._index = 0
        self._lock = threading.Lock()
        self._health: dict[str, dict] = {}  # url -> {ok, at, error}

    def count_available(self, region: str = "") -> int:
        """How many independent exits can currently be leased.

        A rotating gateway URL is one lease unless the provider explicitly
        returns distinct sticky endpoints. It must never inflate concurrency.
        """
        try:
            from core.proxy_providers import has_dynamic_proxy_config

            if has_dynamic_proxy_config():
                return 1
        except Exception:
            pass
        with Session(engine) as s:
            all_active = s.exec(
                select(ProxyModel).where(ProxyModel.is_active == True)  # noqa: E712
            ).all()
        if not all_active:
            return 0
        if region:
            preferred = [p for p in all_active if (p.region or "") == region]
            if preferred:
                return len(preferred)
        return len(all_active)

    def list_active_urls(self, region: str = "") -> list[str]:
        with Session(engine) as s:
            all_active = s.exec(
                select(ProxyModel).where(ProxyModel.is_active == True)  # noqa: E712
            ).all()
        if not all_active:
            return []
        if region:
            preferred = [p for p in all_active if (p.region or "") == region]
            if preferred:
                return [p.url for p in preferred]
        return [p.url for p in all_active]

    def get_next(self, region: str = "") -> Optional[str]:
        """获取下一个可用代理。

        优先级:
          1. 动态代理 provider（如果已配置且启用）
          2. 静态代理池里 region 匹配的代理
          3. 静态代理池里**任意**可用代理（软回退——region 不匹配总比无代理强）
        """
        # 1. 尝试动态代理
        try:
            from core.proxy_providers import get_dynamic_proxy

            dynamic = get_dynamic_proxy()
            if dynamic:
                return dynamic
        except Exception:
            pass

        # 2/3. 静态代理池：先按 region 严格匹配，没有再回退到任意代理
        with Session(engine) as s:
            all_active = s.exec(
                select(ProxyModel).where(ProxyModel.is_active == True)  # noqa: E712
            ).all()
            if not all_active:
                return None
            preferred = (
                [p for p in all_active if (p.region or "") == region]
                if region
                else list(all_active)
            )
            pool = preferred if preferred else list(all_active)
            pool.sort(
                key=lambda p: p.success_count / max(p.success_count + p.fail_count, 1),
                reverse=True,
            )
            with self._lock:
                idx = self._index % len(pool)
                self._index += 1
            return pool[idx].url

    def report_success(self, url: str) -> None:
        with Session(engine) as s:
            p = s.exec(select(ProxyModel).where(ProxyModel.url == url)).first()
            if p:
                p.success_count += 1
                p.last_checked = datetime.now(timezone.utc)
                s.add(p)
                s.commit()
        with self._lock:
            self._health[url] = {"ok": True, "at": time.time(), "error": ""}

    def report_fail(self, url: str) -> None:
        with Session(engine) as s:
            p = s.exec(select(ProxyModel).where(ProxyModel.url == url)).first()
            if p:
                p.fail_count += 1
                p.last_checked = datetime.now(timezone.utc)
                # 连续失败超过10次自动禁用
                if p.fail_count > 0 and p.success_count == 0 and p.fail_count >= 5:
                    p.is_active = False
                s.add(p)
                s.commit()
        with self._lock:
            self._health[url] = {"ok": False, "at": time.time(), "error": "fail"}

    def probe_chatgpt(self, url: str | None = None, *, timeout: float = 12.0) -> dict:
        """Light preflight against chatgpt.com for a proxy (or direct)."""
        proxy = str(url or "").strip() or None
        target = "https://chatgpt.com/"
        try:
            from curl_cffi import requests as cffi_requests

            kwargs = {"timeout": timeout, "impersonate": "chrome136", "allow_redirects": True}
            if proxy:
                kwargs["proxies"] = {"http": proxy, "https": proxy}
            resp = cffi_requests.get(target, **kwargs)
            code = int(getattr(resp, "status_code", 0) or 0)
            text = str(getattr(resp, "text", "") or "")[:400].lower()
            blocked = code >= 400 or "just a moment" in text or "cf-ray" in text and code == 403
            ok = 200 <= code < 400 and not blocked
            host = ""
            if proxy:
                try:
                    host = urlparse(proxy).hostname or proxy[:40]
                except Exception:
                    host = proxy[:40]
            from core.proxy_runtime import public_endpoint_url
            from domain.registration_runtime import stable_resource_ref

            result = {
                "ok": ok,
                "status_code": code,
                "proxy_ref": stable_resource_ref(proxy),
                "proxy_url": public_endpoint_url(proxy, empty="direct"),
                "proxy_host": host or "direct",
                "error": "" if ok else (f"HTTP {code}" if code else "probe failed"),
            }
            if proxy:
                if ok:
                    self.report_success(proxy)
                else:
                    self.report_fail(proxy)
            return result
        except Exception as exc:
            if proxy:
                self.report_fail(proxy)
            from core.proxy_runtime import public_endpoint_url
            from domain.registration_runtime import redact_registration_text, stable_resource_ref

            return {
                "ok": False,
                "status_code": 0,
                "proxy_ref": stable_resource_ref(proxy),
                "proxy_url": public_endpoint_url(proxy, empty="direct"),
                "proxy_host": "direct",
                "error": redact_registration_text(exc)[:200],
            }

    def check_all(self) -> dict:
        """检测所有代理可用性（httpbin + optional chatgpt probe sample）。"""
        import requests

        with Session(engine) as s:
            proxies = s.exec(select(ProxyModel)).all()
        results = {"ok": 0, "fail": 0, "items": []}
        for p in proxies:
            try:
                r = requests.get(
                    "https://httpbin.org/ip",
                    proxies={"http": p.url, "https": p.url},
                    timeout=8,
                )
                if r.status_code == 200:
                    self.report_success(p.url)
                    results["ok"] += 1
                    from core.proxy_runtime import public_endpoint_url

                    results["items"].append({"url": public_endpoint_url(p.url), "ok": True})
                    continue
            except Exception as exc:
                from core.proxy_runtime import public_endpoint_url
                from domain.registration_runtime import redact_registration_text

                results["items"].append(
                    {
                        "url": public_endpoint_url(p.url),
                        "ok": False,
                        "error": redact_registration_text(exc)[:120],
                    }
                )
            self.report_fail(p.url)
            results["fail"] += 1
        return results


proxy_pool = ProxyPool()

"""2Captcha — cloud Turnstile solver."""
from urllib.parse import unquote, urlsplit

from core.base_captcha import BaseCaptcha
from providers.registry import register_provider


@register_provider("captcha", "twocaptcha_api")
class TwoCaptcha(BaseCaptcha):
    def __init__(self, api_key: str):
        self.api_key = api_key
        self.api = "https://2captcha.com"

    @classmethod
    def from_config(cls, config: dict) -> 'TwoCaptcha':
        api_key = str(config.get("twocaptcha_key", "") or "")
        if not api_key:
            raise RuntimeError("2Captcha Key 未配置")
        return cls(api_key)

    @staticmethod
    def _turnstile_payload(
        page_url: str,
        site_key: str,
        *,
        proxy_url: str | None,
        user_agent: str,
        action: str = "",
        cdata: str = "",
        pagedata: str = "",
    ) -> dict:
        payload = {
            "method": "turnstile",
            "sitekey": site_key,
            "pageurl": page_url,
            "json": 1,
        }
        raw_proxy = str(proxy_url or "").strip()
        if raw_proxy:
            parsed = urlsplit(raw_proxy if "://" in raw_proxy else f"http://{raw_proxy}")
            if not parsed.hostname or not parsed.port:
                raise ValueError("Turnstile proxy must include host and port")
            credentials = ""
            if parsed.username:
                credentials = unquote(parsed.username)
                if parsed.password:
                    credentials += f":{unquote(parsed.password)}"
                credentials += "@"
            payload["proxy"] = f"{credentials}{parsed.hostname}:{parsed.port}"
            payload["proxytype"] = parsed.scheme.upper().replace("SOCKS5H", "SOCKS5")
        if user_agent:
            payload["userAgent"] = user_agent
        if action:
            payload["action"] = action
        if cdata:
            payload["data"] = cdata
        if pagedata:
            payload["pagedata"] = pagedata
        return payload

    def solve_turnstile(
        self,
        page_url: str,
        site_key: str,
        *,
        proxy_url: str | None = None,
        user_agent: str = "",
        timeout_seconds: float = 180.0,
        action: str = "",
        cdata: str = "",
        pagedata: str = "",
        attempt_id: str = "",
    ) -> str:
        import time
        import requests

        request_payload = self._turnstile_payload(
            page_url,
            site_key,
            proxy_url=proxy_url,
            user_agent=user_agent,
            action=action,
            cdata=cdata,
            pagedata=pagedata,
        )
        request_payload["key"] = self.api_key
        create = requests.post(
            f"{self.api}/in.php",
            data=request_payload,
            timeout=30,
        )
        create.raise_for_status()
        payload = create.json()
        if payload.get("status") != 1:
            raise RuntimeError(f"2Captcha 创建任务失败: {payload}")
        task_id = payload.get("request")
        if not task_id:
            raise RuntimeError(f"2Captcha 未返回任务 ID: {payload}")

        deadline = time.monotonic() + max(float(timeout_seconds), 1.0)
        while time.monotonic() < deadline:
            time.sleep(min(3.0, max(deadline - time.monotonic(), 0.01)))
            result = requests.get(
                f"{self.api}/res.php",
                params={
                    "key": self.api_key,
                    "action": "get",
                    "id": task_id,
                    "json": 1,
                },
                timeout=30,
            )
            result.raise_for_status()
            data = result.json()
            if data.get("status") == 1:
                return str(data.get("request") or "")
            if data.get("request") not in {"CAPCHA_NOT_READY", "CAPTCHA_NOT_READY"}:
                raise RuntimeError(f"2Captcha 错误: {data}")
        raise TimeoutError("2Captcha Turnstile 超时")

    def solve_recaptcha_v2(self, page_url: str, site_key: str) -> str:
        import time
        import requests

        create = requests.post(
            f"{self.api}/in.php",
            data={
                "key": self.api_key,
                "method": "userrecaptcha",
                "googlekey": site_key,
                "pageurl": page_url,
                "json": 1,
            },
            timeout=30,
        )
        create.raise_for_status()
        payload = create.json()
        if payload.get("status") != 1:
            raise RuntimeError(f"2Captcha 创建 reCAPTCHA 任务失败: {payload}")
        task_id = payload.get("request")
        if not task_id:
            raise RuntimeError(f"2Captcha 未返回 reCAPTCHA 任务 ID: {payload}")

        for _ in range(60):
            time.sleep(3)
            result = requests.get(
                f"{self.api}/res.php",
                params={
                    "key": self.api_key,
                    "action": "get",
                    "id": task_id,
                    "json": 1,
                },
                timeout=30,
            )
            result.raise_for_status()
            data = result.json()
            if data.get("status") == 1:
                return str(data.get("request") or "")
            if data.get("request") not in {"CAPCHA_NOT_READY", "CAPTCHA_NOT_READY"}:
                raise RuntimeError(f"2Captcha reCAPTCHA 错误: {data}")
        raise TimeoutError("2Captcha reCAPTCHA 超时")

    def solve_hcaptcha(self, page_url: str, site_key: str) -> str:
        """求解 hCaptcha (proxyless)。

        2Captcha 接口：``method=hcaptcha`` + ``sitekey`` + ``pageurl``，token
        从 ``request`` 字段返回（与 Turnstile / reCAPTCHA 路径同结构）。
        """
        import time
        import requests

        create = requests.post(
            f"{self.api}/in.php",
            data={
                "key": self.api_key,
                "method": "hcaptcha",
                "sitekey": site_key,
                "pageurl": page_url,
                "json": 1,
            },
            timeout=30,
        )
        create.raise_for_status()
        payload = create.json()
        if payload.get("status") != 1:
            raise RuntimeError(f"2Captcha 创建 hCaptcha 任务失败: {payload}")
        task_id = payload.get("request")
        if not task_id:
            raise RuntimeError(f"2Captcha 未返回 hCaptcha 任务 ID: {payload}")

        for _ in range(60):
            time.sleep(3)
            result = requests.get(
                f"{self.api}/res.php",
                params={
                    "key": self.api_key,
                    "action": "get",
                    "id": task_id,
                    "json": 1,
                },
                timeout=30,
            )
            result.raise_for_status()
            data = result.json()
            if data.get("status") == 1:
                return str(data.get("request") or "")
            if data.get("request") not in {"CAPCHA_NOT_READY", "CAPTCHA_NOT_READY"}:
                raise RuntimeError(f"2Captcha hCaptcha 错误: {data}")
        raise TimeoutError("2Captcha hCaptcha 超时")

    def solve_image(self, image_b64: str) -> str:
        raise NotImplementedError

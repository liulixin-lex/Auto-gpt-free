"""Cloud Mail Worker mailbox provider.

The provider uses Cloud Mail's public API to provision one mailbox per
registration task and poll that mailbox for verification messages.
"""

from __future__ import annotations

import hashlib
import html
import json
import re
import secrets
import string
import threading
import time
from urllib.parse import urlparse

import requests

from core.base_mailbox import BaseMailbox, MailboxAccount, _extract_verification_link


DEFAULT_CODE_PATTERN = r"(?<!#)(?<!\d)(\d{6})(?!\d)"


class CloudMailMailboxPool(BaseMailbox):
    """Create and read mailboxes through a self-hosted Cloud Mail Worker."""

    def __init__(
        self,
        *,
        api_base: str,
        api_key: str = "",
        admin_email: str = "",
        admin_password: str = "",
        domain: str = "",
        poll_interval: float | str = 3,
        request_timeout: float | str = 15,
        proxy: str | None = None,
        session: requests.Session | None = None,
    ):
        self.api_base = self._normalize_api_base(api_base)
        self.api_key = self._normalize_token(api_key)
        self.admin_email = str(admin_email or "").strip()
        self.admin_password = str(admin_password or "")
        self.domain = self._normalize_domain(domain)
        self.poll_interval = max(0.0, float(3 if poll_interval in (None, "") else poll_interval))
        self.request_timeout = max(1.0, float(15 if request_timeout in (None, "") else request_timeout))
        self.proxy = {"http": proxy, "https": proxy} if proxy else None
        self.session = session or requests.Session()
        self._cached_token = ""
        self._token_lock = threading.Lock()

    @classmethod
    def from_config(cls, config: dict) -> "CloudMailMailboxPool":
        return cls(
            api_base=config.get("cloud_mail_api_base", ""),
            api_key=config.get("cloud_mail_api_key", ""),
            admin_email=config.get("cloud_mail_admin_email", ""),
            admin_password=config.get("cloud_mail_admin_password", ""),
            domain=config.get("cloud_mail_domain", ""),
            poll_interval=config.get("cloud_mail_poll_interval", 3),
            request_timeout=config.get("cloud_mail_request_timeout", 15),
            proxy=config.get("proxy") or config.get("mailbox_proxy") or None,
        )

    @staticmethod
    def _normalize_api_base(value: object) -> str:
        base = str(value or "").strip().rstrip("/")
        if base.lower().endswith("/api"):
            base = base[:-4].rstrip("/")
        parsed = urlparse(base)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("Cloud Mail 地址无效，请填写完整的 http/https 地址")
        return base

    @staticmethod
    def _normalize_token(value: object) -> str:
        token = str(value or "").strip()
        if token.lower().startswith("bearer "):
            token = token[7:].strip()
        return token

    @staticmethod
    def _normalize_domain(value: object) -> str:
        domain = str(value or "").strip().lower().lstrip("@")
        return domain.rstrip(".")

    @staticmethod
    def _response_message(payload: object, fallback: str) -> str:
        if isinstance(payload, dict):
            message = payload.get("message") or payload.get("msg") or payload.get("error")
            if message:
                return str(message)
        return fallback

    def _request(
        self,
        method: str,
        path: str,
        *,
        body: dict | None = None,
        token: str = "",
    ) -> object:
        url = f"{self.api_base}/api/{path.lstrip('/')}"
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "freeAgentIdentity/cloud-mail",
        }
        if token:
            headers["Authorization"] = token
        response = self.session.request(
            method,
            url,
            headers=headers,
            json=body,
            proxies=self.proxy,
            timeout=self.request_timeout,
        )
        try:
            payload = response.json()
        except Exception as exc:
            try:
                response.raise_for_status()
            except Exception as http_exc:
                raise RuntimeError(f"Cloud Mail 请求失败: HTTP {response.status_code}") from http_exc
            raise RuntimeError("Cloud Mail 返回了非 JSON 响应，请检查 Worker 地址") from exc

        if int(getattr(response, "status_code", 200) or 200) >= 400:
            message = self._response_message(payload, f"HTTP {response.status_code}")
            raise RuntimeError(f"Cloud Mail 请求失败: {message}")
        response.raise_for_status()

        if isinstance(payload, dict) and "code" in payload:
            code = str(payload.get("code"))
            if code not in {"0", "200"}:
                message = self._response_message(payload, f"code={code}")
                raise RuntimeError(f"Cloud Mail 请求失败: {message}")
            return payload.get("data")
        return payload

    def _token(self) -> str:
        if self.api_key:
            return self.api_key
        if self._cached_token:
            return self._cached_token
        if not self.admin_email or not self.admin_password:
            raise RuntimeError("请填写 Cloud Mail Public API Token，或同时填写管理员邮箱和密码")

        with self._token_lock:
            if self._cached_token:
                return self._cached_token
            data = self._request(
                "POST",
                "/public/genToken",
                body={"email": self.admin_email, "password": self.admin_password},
            )
            token = self._normalize_token(data.get("token") if isinstance(data, dict) else "")
            if not token:
                raise RuntimeError("Cloud Mail 未返回 Public API Token")
            self._cached_token = token
            return token

    def _available_domains(self) -> list[str]:
        data = self._request("GET", "/setting/websiteConfig")
        raw_domains = data.get("domainList") if isinstance(data, dict) else []
        domains: list[str] = []
        for item in raw_domains or []:
            value = item
            if isinstance(item, dict):
                value = item.get("domain") or item.get("name") or item.get("value") or ""
            normalized = self._normalize_domain(value)
            if normalized and normalized not in domains:
                domains.append(normalized)
        if not domains:
            raise RuntimeError("Cloud Mail 未返回可用邮箱域名，请检查 Worker 的 domain 配置")
        return domains

    def _selected_domain(self) -> str:
        domains = self._available_domains()
        if not self.domain:
            return domains[0]
        if self.domain not in domains:
            raise RuntimeError(
                f"Cloud Mail 中未配置域名 {self.domain}，可用域名: {', '.join(domains)}"
            )
        return self.domain

    def _email_list(self, email_address: str, *, size: int = 20) -> list[dict]:
        requested_email = str(email_address or "").strip()
        query_email = requested_email
        # Cloud Mail's D1 query uses `LIKE` and some D1 deployments reject
        # patterns around 50 bytes. Generated local parts are safe literals,
        # so use a short local-part prefix and filter the exact address here.
        local_part, separator, _domain = requested_email.partition("@")
        if len(requested_email) >= 45 and separator and re.fullmatch(r"[A-Za-z0-9.+-]+", local_part):
            query_email = f"{local_part}@%"
        data = self._request(
            "POST",
            "/public/emailList",
            body={"toEmail": query_email, "size": size},
            token=self._token(),
        )
        if data is None:
            return []
        if not isinstance(data, list):
            raise RuntimeError("Cloud Mail 邮件列表响应格式无效")
        messages = [item for item in data if isinstance(item, dict)]
        if query_email != requested_email:
            expected = requested_email.lower()
            messages = [
                item for item in messages
                if str(item.get("toEmail") or "").strip().lower() == expected
            ]
        return messages

    def test_connection(self) -> dict:
        domains = self._available_domains()
        # Cloudflare D1 keeps LIKE patterns near 50 bytes; Cloud Mail queries
        # toEmail with LIKE, so the auth probe must stay comfortably below it.
        probe = f"fai{secrets.token_hex(4)}@invalid"
        self._email_list(probe, size=1)
        selected = self.domain or domains[0]
        if self.domain and self.domain not in domains:
            raise RuntimeError(
                f"Cloud Mail 中未配置域名 {self.domain}，可用域名: {', '.join(domains)}"
            )
        return {
            "ok": True,
            "message": f"连接成功，API 鉴权有效，可用域名: {', '.join(domains)}",
            "email": f"随机地址@{selected}",
        }

    @staticmethod
    def _random_local_part(length: int = 12) -> str:
        alphabet = string.ascii_lowercase + string.digits
        return "fai" + "".join(secrets.choice(alphabet) for _ in range(length))

    @staticmethod
    def _random_password(length: int = 20) -> str:
        alphabet = string.ascii_letters + string.digits
        return "".join(secrets.choice(alphabet) for _ in range(length))

    def peek_email(self) -> str:
        return f"{self._random_local_part()}@{self._selected_domain()}"

    def _create_user(self, email_address: str, password: str, token: str) -> None:
        self._request(
            "POST",
            "/public/addUser",
            body={"list": [{"email": email_address, "password": password}]},
            token=token,
        )

    def get_email(self) -> MailboxAccount:
        domain = self._selected_domain()
        token = self._token()
        last_error = ""
        for _attempt in range(5):
            email_address = f"{self._random_local_part()}@{domain}"
            password = self._random_password()
            try:
                self._create_user(email_address, password, token)
                return MailboxAccount(
                    email=email_address,
                    account_id=email_address.lower(),
                    extra={
                        "provider_account": {
                            "provider_type": "mailbox",
                            "provider_name": "cloud_mail",
                            "login_identifier": email_address,
                            "display_name": email_address,
                            "credentials": {"email": email_address, "password": password},
                            "metadata": {"source": "cloud_mail", "domain": domain},
                        },
                        "provider_resource": {
                            "provider_type": "mailbox",
                            "provider_name": "cloud_mail",
                            "resource_type": "mailbox",
                            "resource_identifier": email_address.lower(),
                            "handle": email_address,
                            "display_name": email_address,
                            "metadata": {"email": email_address, "domain": domain},
                        },
                    },
                )
            except Exception as exc:
                last_error = str(exc).strip() or exc.__class__.__name__
                lowered = last_error.lower()
                if not any(marker in lowered for marker in ("exist", "already", "存在", "重复")):
                    raise
        raise RuntimeError(f"Cloud Mail 连续生成邮箱失败: {last_error}")

    @staticmethod
    def _message_id(message: dict) -> str:
        for key in ("emailId", "email_id", "id"):
            value = message.get(key)
            if value not in (None, ""):
                return str(value)
        normalized = json.dumps(message, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return "body:" + hashlib.sha256(normalized.encode("utf-8")).hexdigest()

    @staticmethod
    def _message_text(message: dict) -> str:
        subject = str(message.get("subject") or "")
        plain = str(message.get("text") or "")
        content = str(message.get("content") or "")
        content = re.sub(r"(?is)<(?:script|style)[^>]*>.*?</(?:script|style)>", " ", content)
        content = re.sub(r"(?s)<[^>]+>", " ", content)
        content = html.unescape(content)
        return "\n".join(part for part in (subject, plain, content) if part).strip()

    @staticmethod
    def _message_link_text(message: dict) -> str:
        return "\n".join(
            str(message.get(key) or "") for key in ("subject", "text", "content")
        )

    @staticmethod
    def _match_code(text: str, pattern: re.Pattern[str]) -> str:
        match = pattern.search(text)
        if not match:
            return ""
        return match.group(1) if match.groups() else match.group(0)

    @classmethod
    def _extract_code(cls, text: str, code_pattern: str | None = None) -> str:
        if code_pattern:
            return cls._match_code(text, re.compile(code_pattern))

        labelled = re.search(
            r"(?:验证码|校验码|动态码|confirmation\s*code|verification\s*code|"
            r"temporary\s+(?:login\s+)?code|one[- ]?time\s*code|login\s*code|otp|code)"
            r"\s*(?:is|为)?\s*[:：#-]?\s*"
            r"((?=[A-Z0-9-]{4,17}\b)(?=[A-Z0-9-]*\d)[A-Z0-9]{3,10}(?:-[A-Z0-9]{2,10})?)",
            text,
            flags=re.IGNORECASE,
        )
        if labelled:
            return labelled.group(1)

        safe_text = re.sub(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", " ", text, flags=re.IGNORECASE)
        safe_text = re.sub(r"https?://\S+", " ", safe_text, flags=re.IGNORECASE)
        candidates = list(dict.fromkeys(re.findall(DEFAULT_CODE_PATTERN, safe_text)))
        return candidates[0] if len(candidates) == 1 else ""

    def get_current_ids(self, account: MailboxAccount) -> set:
        try:
            return {self._message_id(message) for message in self._email_list(account.email)}
        except Exception:
            return set()

    def wait_for_code(
        self,
        account: MailboxAccount,
        keyword: str = "",
        timeout: int = 120,
        before_ids: set | None = None,
        code_pattern: str | None = None,
    ) -> str:
        seen = {str(value) for value in (before_ids or set())}
        deadline = time.monotonic() + timeout
        last_error = ""
        while time.monotonic() < deadline:
            try:
                messages = self._email_list(account.email)
                for message in messages:
                    message_id = self._message_id(message)
                    if message_id in seen:
                        continue
                    text = self._message_text(message)
                    seen.add(message_id)
                    if keyword and keyword.lower() not in text.lower():
                        continue
                    code = self._extract_code(text, code_pattern=code_pattern)
                    if code:
                        return code
            except Exception as exc:
                last_error = str(exc).strip() or exc.__class__.__name__
            time.sleep(self.poll_interval)
        suffix = f"，最后错误: {last_error}" if last_error else ""
        raise TimeoutError(f"等待 Cloud Mail 验证码超时 ({timeout}s){suffix}")

    def wait_for_link(
        self,
        account: MailboxAccount,
        keyword: str = "",
        timeout: int = 120,
        before_ids: set | None = None,
    ) -> str:
        seen = {str(value) for value in (before_ids or set())}
        deadline = time.monotonic() + timeout
        last_error = ""
        while time.monotonic() < deadline:
            try:
                messages = self._email_list(account.email)
                for message in messages:
                    message_id = self._message_id(message)
                    if message_id in seen:
                        continue
                    seen.add(message_id)
                    link = _extract_verification_link(self._message_link_text(message), keyword)
                    if link:
                        return link
            except Exception as exc:
                last_error = str(exc).strip() or exc.__class__.__name__
            time.sleep(self.poll_interval)
        suffix = f"，最后错误: {last_error}" if last_error else ""
        raise TimeoutError(f"等待 Cloud Mail 验证链接超时 ({timeout}s){suffix}")

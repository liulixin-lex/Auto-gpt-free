"""Catch-all / domain IMAP mailbox for high-survival ChatGPT registration.

Generate random addresses under your own domain that land in a single IMAP
inbox (plus-addressing or full catch-all). Not a public free temp-mail service.
"""
from __future__ import annotations

import email
import imaplib
import random
import re
import secrets
import string
import time
from email.header import decode_header, make_header
from typing import Any

from core.base_mailbox import BaseMailbox, MailboxAccount, _extract_verification_link

DEFAULT_CODE_PATTERN = r"(?<!#)(?<!\d)(\d{6})(?!\d)"


def _decode_mime(value: str) -> str:
    try:
        return str(make_header(decode_header(value or "")))
    except Exception:
        return str(value or "")


class DomainImapMailboxPool(BaseMailbox):
    """Create local+tag@domain addresses and poll IMAP for OTP/links."""

    def __init__(
        self,
        *,
        domain: str,
        imap_host: str,
        imap_user: str,
        imap_password: str,
        imap_port: int | str = 993,
        use_ssl: bool | str = True,
        local_prefix: str = "fai",
        poll_interval: float | str = 3,
        proxy: str | None = None,
    ):
        self.domain = str(domain or "").strip().lower().lstrip("@").rstrip(".")
        self.imap_host = str(imap_host or "").strip()
        self.imap_user = str(imap_user or "").strip()
        self.imap_password = str(imap_password or "")
        try:
            self.imap_port = int(imap_port or 993)
        except Exception:
            self.imap_port = 993
        self.use_ssl = str(use_ssl).strip().lower() not in {"0", "false", "no", "off"}
        self.local_prefix = re.sub(r"[^a-z0-9]", "", str(local_prefix or "fai").lower()) or "fai"
        self.poll_interval = max(0.5, float(3 if poll_interval in (None, "") else poll_interval))
        del proxy  # IMAP path does not use HTTP proxy in this simple client.
        if not self.domain:
            raise ValueError("domain_imap 需要配置邮箱域名")
        if not self.imap_host or not self.imap_user or not self.imap_password:
            raise ValueError("domain_imap 需要 IMAP 主机 / 用户名 / 密码")

    @classmethod
    def from_config(cls, config: dict) -> "DomainImapMailboxPool":
        return cls(
            domain=config.get("domain_imap_domain", ""),
            imap_host=config.get("domain_imap_host", ""),
            imap_user=config.get("domain_imap_user", ""),
            imap_password=config.get("domain_imap_password", ""),
            imap_port=config.get("domain_imap_port", 993),
            use_ssl=config.get("domain_imap_ssl", True),
            local_prefix=config.get("domain_imap_prefix", "fai"),
            poll_interval=config.get("domain_imap_poll_interval", 3),
            proxy=config.get("proxy") or None,
        )

    def get_email(self) -> MailboxAccount:
        tag = secrets.token_hex(4)
        local = f"{self.local_prefix}{tag}"
        address = f"{local}@{self.domain}"
        return MailboxAccount(
            email=address,
            password=secrets.token_urlsafe(12),
            extra={
                "mailbox_provider_key": "domain_imap",
                "provider_name": "domain_imap",
                "imap_host": self.imap_host,
                "local_part": local,
            },
        )

    def _connect(self) -> imaplib.IMAP4:
        if self.use_ssl:
            client: imaplib.IMAP4 = imaplib.IMAP4_SSL(self.imap_host, self.imap_port)
        else:
            client = imaplib.IMAP4(self.imap_host, self.imap_port)
        client.login(self.imap_user, self.imap_password)
        client.select("INBOX")
        return client

    def _message_text(self, raw: bytes) -> str:
        msg = email.message_from_bytes(raw)
        parts: list[str] = []
        subject = _decode_mime(str(msg.get("Subject") or ""))
        if subject:
            parts.append(subject)
        if msg.is_multipart():
            for part in msg.walk():
                ctype = str(part.get_content_type() or "")
                if ctype not in {"text/plain", "text/html"}:
                    continue
                try:
                    payload = part.get_payload(decode=True) or b""
                    charset = part.get_content_charset() or "utf-8"
                    parts.append(payload.decode(charset, errors="ignore"))
                except Exception:
                    continue
        else:
            try:
                payload = msg.get_payload(decode=True) or b""
                charset = msg.get_content_charset() or "utf-8"
                parts.append(payload.decode(charset, errors="ignore"))
            except Exception:
                pass
        return "\n".join(parts)

    def _list_recent(self, account: MailboxAccount, *, limit: int = 30) -> list[tuple[str, str]]:
        """Return [(id, text), ...] newest first for messages mentioning account email."""
        client = self._connect()
        try:
            status, data = client.uid("search", None, "ALL")
            if status != "OK" or not data or not data[0]:
                return []
            uids = data[0].split()
            uids = uids[-limit:]
            out: list[tuple[str, str]] = []
            needle = (account.email or "").lower()
            local = needle.split("@", 1)[0]
            for uid in reversed(uids):
                st, fetched = client.uid("fetch", uid, "(RFC822)")
                if st != "OK" or not fetched:
                    continue
                raw = b""
                for item in fetched:
                    if isinstance(item, tuple) and len(item) >= 2 and isinstance(item[1], (bytes, bytearray)):
                        raw = bytes(item[1])
                        break
                if not raw:
                    continue
                text = self._message_text(raw)
                lowered = text.lower()
                if needle and needle not in lowered and local and local not in lowered:
                    # Catch-all may still deliver without repeating full address in body.
                    # Keep subject/body OpenAI-ish messages.
                    if "openai" not in lowered and "chatgpt" not in lowered and "verification" not in lowered:
                        continue
                out.append((uid.decode() if isinstance(uid, bytes) else str(uid), text))
            return out
        finally:
            try:
                client.logout()
            except Exception:
                pass

    def get_current_ids(self, account: MailboxAccount) -> set:
        try:
            return {mid for mid, _ in self._list_recent(account, limit=40)}
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
        seen = {str(v) for v in (before_ids or set())}
        deadline = time.monotonic() + max(int(timeout or 120), 1)
        pattern = re.compile(code_pattern or DEFAULT_CODE_PATTERN)
        last_error = ""
        while time.monotonic() < deadline:
            try:
                for mid, text in self._list_recent(account, limit=40):
                    if mid in seen:
                        continue
                    seen.add(mid)
                    if keyword and keyword.lower() not in text.lower():
                        continue
                    match = pattern.search(text)
                    if match:
                        return match.group(1)
            except Exception as exc:
                last_error = str(exc)
            time.sleep(self.poll_interval)
        suffix = f"，最后错误: {last_error}" if last_error else ""
        raise TimeoutError(f"等待 Domain IMAP 验证码超时 ({timeout}s){suffix}")

    def wait_for_link(
        self,
        account: MailboxAccount,
        keyword: str = "",
        timeout: int = 120,
        before_ids: set | None = None,
    ) -> str:
        seen = {str(v) for v in (before_ids or set())}
        deadline = time.monotonic() + max(int(timeout or 120), 1)
        last_error = ""
        while time.monotonic() < deadline:
            try:
                for mid, text in self._list_recent(account, limit=40):
                    if mid in seen:
                        continue
                    seen.add(mid)
                    link = _extract_verification_link(text, keyword)
                    if link:
                        return link
            except Exception as exc:
                last_error = str(exc)
            time.sleep(self.poll_interval)
        suffix = f"，最后错误: {last_error}" if last_error else ""
        raise TimeoutError(f"等待 Domain IMAP 验证链接超时 ({timeout}s){suffix}")

from __future__ import annotations

from typing import Any

from .errors import BrowserReuseRequiredError, IdentityResolutionError, RegistrationUnsupportedError
from .models import RegistrationContext


def has_reusable_oauth_browser(identity: Any) -> bool:
    return bool((getattr(identity, "chrome_user_data_dir", "") or "").strip() or (getattr(identity, "chrome_cdp_url", "") or "").strip())


def resolve_timeout(extra: dict[str, Any], keys: tuple[str, ...], default: int) -> int:
    for key in keys:
        value = extra.get(key)
        if value not in (None, ""):
            return int(value)
    return int(default)


def ensure_identity_email(ctx: RegistrationContext, message: str) -> None:
    if not getattr(ctx.identity, "email", ""):
        raise IdentityResolutionError(message)


def ensure_mailbox_identity(ctx: RegistrationContext, message: str) -> None:
    if not getattr(ctx.identity, "has_mailbox", False):
        raise IdentityResolutionError(message)


def ensure_oauth_executor_allowed(ctx: RegistrationContext, allowed_executor_types: tuple[str, ...] | None, message: str | None = None) -> None:
    if not allowed_executor_types:
        return
    if ctx.executor_type not in allowed_executor_types:
        expected = ", ".join(allowed_executor_types)
        raise RegistrationUnsupportedError(message or f"{ctx.platform_display_name} 当前 OAuth 仅支持 executor_type={expected}")


def ensure_oauth_browser_reuse(ctx: RegistrationContext, message: str) -> None:
    if not has_reusable_oauth_browser(ctx.identity):
        raise BrowserReuseRequiredError(message)


def _chunked_mailbox_wait(
    *,
    wait_fn,
    total_timeout: int | None,
    cancel_check,
    wait_message: str,
    log_fn,
    chunk_seconds: int = 5,
):
    """Poll mailbox in short slices so per-account cancel/timeout can abort fast."""
    import time

    total = int(total_timeout if total_timeout is not None else 90)
    total = max(total, 1)
    chunk = max(int(chunk_seconds or 5), 1)
    deadline = time.monotonic() + total
    last_error: Exception | None = None
    log_fn(wait_message)
    while True:
        if callable(cancel_check) and cancel_check():
            raise RuntimeError("任务已取消")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        slice_timeout = min(chunk, max(int(remaining), 1))
        try:
            code = wait_fn(timeout=slice_timeout)
            if code:
                return code
        except TimeoutError as exc:
            last_error = exc
        except Exception as exc:
            # Some mailboxes raise TimeoutError subclasses / RuntimeError on empty wait.
            msg = str(exc).lower()
            if "超时" in str(exc) or "timeout" in msg:
                last_error = exc
            else:
                raise
        if callable(cancel_check) and cancel_check():
            raise RuntimeError("任务已取消")
    timeout_error = TimeoutError(f"等待验证码超时 ({total}s)")
    if last_error:
        raise timeout_error from last_error
    raise timeout_error


def build_otp_callback(
    ctx: RegistrationContext,
    *,
    keyword: str = "",
    timeout: int | None = None,
    code_pattern: str | None = None,
    wait_message: str = "等待验证码...",
    success_label: str = "验证码",
):
    mailbox = getattr(ctx.platform, "mailbox", None)
    mail_acct = getattr(ctx.identity, "mailbox_account", None)
    if not mailbox or not mail_acct:
        return None

    cancel_check = getattr(ctx.platform, "is_cancel_requested", None)

    before_ids = set(getattr(ctx.identity, "before_ids", set()) or set())

    def _advance_cursor() -> None:
        current_ids = set(mailbox.get_current_ids(mail_acct) or set())
        before_ids.update(current_ids)
        ctx.identity.before_ids = set(before_ids)

    def otp_cb():

        def _wait(timeout: int):
            kwargs = {"keyword": keyword, "before_ids": before_ids, "timeout": timeout}
            if code_pattern:
                kwargs["code_pattern"] = code_pattern
            return mailbox.wait_for_code(mail_acct, **kwargs)

        code = _chunked_mailbox_wait(
            wait_fn=_wait,
            total_timeout=timeout,
            cancel_check=cancel_check,
            wait_message=wait_message,
            log_fn=ctx.log,
        )
        if code:
            try:
                _advance_cursor()
            except Exception:
                pass
            ctx.log(f"{success_label}已收到")
        return code

    otp_cb.advance_cursor = _advance_cursor
    return otp_cb


def build_link_callback(
    ctx: RegistrationContext,
    *,
    keyword: str = "",
    timeout: int | None = None,
    wait_message: str = "等待验证链接邮件...",
    success_label: str = "验证链接",
    preview_chars: int = 80,
):
    mailbox = getattr(ctx.platform, "mailbox", None)
    mail_acct = getattr(ctx.identity, "mailbox_account", None)
    if not mailbox or not mail_acct:
        return None

    cancel_check = getattr(ctx.platform, "is_cancel_requested", None)

    def link_cb():
        before_ids = mailbox.get_current_ids(mail_acct)

        def _wait(timeout: int):
            return mailbox.wait_for_link(
                mail_acct,
                keyword=keyword,
                timeout=timeout,
                before_ids=before_ids,
            )

        link = _chunked_mailbox_wait(
            wait_fn=_wait,
            total_timeout=timeout,
            cancel_check=cancel_check,
            wait_message=wait_message,
            log_fn=ctx.log,
        )
        if link:
            preview = link if len(link) <= preview_chars else f"{link[:preview_chars]}..."
            ctx.log(f"{success_label}: {preview}")
        return link

    return link_cb

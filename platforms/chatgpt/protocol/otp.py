from __future__ import annotations

from dataclasses import dataclass
from typing import Callable


class OtpProviderError(RuntimeError):
    pass


class OtpTimeoutError(RuntimeError):
    pass


class OtpInvalidError(RuntimeError):
    pass


@dataclass(slots=True)
class OtpResult:
    code: str
    validation: dict
    resend_count: int = 0


class OtpCoordinator:
    def __init__(
        self,
        *,
        receive: Callable[[], str],
        validate: Callable[[str], dict],
        resend: Callable[[], None] | None = None,
        advance_cursor: Callable[[], None] | None = None,
        cancel_check: Callable[[], bool] | None = None,
        log: Callable[[str], None] | None = None,
    ) -> None:
        self.receive = receive
        self.validate = validate
        self.resend = resend
        self.advance_cursor = advance_cursor
        self.cancel_check = cancel_check or (lambda: False)
        self.log = log or (lambda _message: None)

    def _resend_once(self, message: str) -> None:
        self.log(message)
        try:
            if self.advance_cursor is not None:
                self.advance_cursor()
            if self.resend is not None:
                self.resend()
        except Exception as exc:
            raise OtpProviderError(f"邮箱验证码 resend 失败: {exc}") from exc

    def run(self) -> OtpResult:
        resend_count = 0
        while True:
            if self.cancel_check():
                raise RuntimeError("任务已取消")
            try:
                code = str(self.receive() or "").strip()
            except (OtpTimeoutError, TimeoutError) as exc:
                if self.resend is not None and resend_count == 0:
                    resend_count += 1
                    self._resend_once("验证码等待超时，执行一次 resend")
                    continue
                raise OtpTimeoutError(str(exc) or "等待验证码超时") from exc
            except Exception as exc:
                raise OtpProviderError(f"邮箱服务读取验证码失败: {exc}") from exc
            if not code:
                if self.resend is not None and resend_count == 0:
                    resend_count += 1
                    self._resend_once("未收到验证码，执行一次 resend")
                    continue
                raise OtpTimeoutError("未收到验证码")
            try:
                validation = self.validate(code)
                return OtpResult(code=code, validation=validation, resend_count=resend_count)
            except Exception as exc:
                lowered = str(exc).lower()
                invalid = any(
                    marker in lowered
                    for marker in (
                        "invalid_otp",
                        "invalid otp",
                        "invalid_code",
                        "invalid code",
                        "incorrect code",
                        "verification code is invalid",
                        "验证码无效",
                        "验证码错误",
                        "验证码不正确",
                    )
                )
                if invalid and self.resend is not None and resend_count == 0:
                    resend_count += 1
                    self._resend_once("验证码无效，执行一次 resend")
                    continue
                if invalid:
                    raise OtpInvalidError(str(exc)) from exc
                raise

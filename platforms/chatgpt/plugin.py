"""ChatGPT / Codex CLI 平台插件"""
import secrets
from core.base_platform import BasePlatform, Account, AccountStatus, RegisterConfig
from core.base_mailbox import BaseMailbox
from core.registration import BrowserRegistrationAdapter, OtpSpec, ProtocolMailboxAdapter, RegistrationResult
from core.registry import register
from core.proxy_pool import proxy_pool


def _generate_chatgpt_registration_password(length: int = 16) -> str:
    """生成更稳定通过 OpenAI 注册页校验的密码。

    旧协议流已经验证过：至少带小写、数字、符号时，成功率明显更稳。
    这里再补一个大写字符，避免浏览器流随机生成出“看起来够长但组合不够强”的密码。
    """
    specials = ",._!@#"
    minimum_length = 12
    size = max(int(length or minimum_length), minimum_length)
    required = [
        secrets.choice("abcdefghijklmnopqrstuvwxyz"),
        secrets.choice("ABCDEFGHIJKLMNOPQRSTUVWXYZ"),
        secrets.choice("0123456789"),
        secrets.choice(specials),
    ]
    pool = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789" + specials
    required.extend(secrets.choice(pool) for _ in range(size - len(required)))
    secrets.SystemRandom().shuffle(required)
    return "".join(required)


@register
class ChatGPTPlatform(BasePlatform):
    name = "chatgpt"
    display_name = "ChatGPT"
    version = "1.0.0"
    supported_executors = ["protocol", "headless", "headed"]
    supported_identity_modes = ["mailbox"]
    supported_oauth_providers = []

    def __init__(self, config: RegisterConfig = None, mailbox: BaseMailbox = None):
        super().__init__(config)
        self.mailbox = mailbox

    def check_valid(self, account: Account) -> bool:
        self._last_check_overview = {}
        try:
            from platforms.chatgpt.subscription import fetch_subscription_status_details
            from core.proxy_pool import proxy_pool
            class _A: pass
            a = _A()
            extra = account.extra or {}
            a.access_token = extra.get("access_token") or account.token
            a.id_token = extra.get("id_token", "")
            a.cookies = extra.get("cookies", "")
            a.extra = extra

            region = str(getattr(account, "region", "") or extra.get("region", "") or "").strip()
            configured_proxy = self.config.proxy if self.config else None
            proxy_candidates: list[tuple[str | None, bool]] = []
            if configured_proxy:
                proxy_candidates.append((configured_proxy, False))
            else:
                pooled_proxy = proxy_pool.get_next(region=region)
                if pooled_proxy:
                    proxy_candidates.append((pooled_proxy, True))
            proxy_candidates.append((None, False))

            for proxy, should_report in proxy_candidates:
                try:
                    details = fetch_subscription_status_details(a, proxy=proxy)
                    if should_report and proxy:
                        proxy_pool.report_success(proxy)
                    status = details.get("status")
                    # 把订阅状态同步映射成前端能用的 plan_state / chips
                    # 来源（避免老 chips 还带 "Plus" 但实际已 free）。
                    if status == "plus":
                        plan_state = "subscribed"
                        chips = ["Plus"]
                    elif status == "team":
                        plan_state = "subscribed"
                        chips = ["Team"]
                    elif status == "free":
                        plan_state = "free"
                        chips = ["Free"]
                    elif status in ("expired", "invalid", "banned"):
                        plan_state = "expired"
                        chips = []
                    else:
                        plan_state = "unknown"
                        chips = []
                    overview = {
                        "plan": status,
                        "plan_name": status,
                        "plan_state": plan_state,
                        "chips": chips,
                        "check_source": details.get("source"),
                    }
                    if isinstance(details.get("usage"), dict):
                        overview["chatgpt_usage"] = details["usage"]
                    self._last_check_overview = overview
                    return status not in ("expired", "invalid", "banned", None)
                except Exception:
                    if should_report and proxy:
                        proxy_pool.report_fail(proxy)
                    continue
        except Exception:
            return False
        return False

    def get_last_check_overview(self) -> dict:
        return dict(getattr(self, "_last_check_overview", {}) or {})

    def _prepare_registration_password(self, password: str | None) -> str | None:
        if password:
            return password
        return _generate_chatgpt_registration_password()

    def _map_chatgpt_result(
        self,
        result: dict,
        *,
        password: str = "",
        user_id: str = "",
    ) -> RegistrationResult:
        return RegistrationResult(
            email=result.get("email", ""),
            password=password or result.get("password", ""),
            user_id=user_id or result.get("account_id", ""),
            token=result.get("access_token", ""),
            status=AccountStatus.REGISTERED,
            extra={
                "account_id": result.get("account_id", ""),
                "access_token": result.get("access_token", ""),
                "refresh_token": result.get("refresh_token", ""),
                "id_token": result.get("id_token", ""),
                "session_token": result.get("session_token", ""),
                "workspace_id": result.get("workspace_id", ""),
                "cookies": result.get("cookies", ""),
                "profile": result.get("profile", {}),
                "expires_at": result.get("expires_at", ""),
            },
        )

    def build_browser_registration_adapter(self):
        def _build_browser_worker(ctx, artifacts):
            from platforms.chatgpt.browser_register import ChatGPTBrowserRegister

            extra = dict(ctx.extra or {})
            profile = extra.get("browser_profile") if isinstance(extra.get("browser_profile"), dict) else {}
            raw_logger = getattr(ctx.platform, "_log_fn", None)

            def _stage(stage, message: str, action: str) -> None:
                if stage.value in {"otp_trigger", "otp_wait"}:
                    mailbox = getattr(ctx.platform, "mailbox", None)
                    marker = getattr(mailbox, "mark_current", None)
                    if callable(marker):
                        marker("side_effect_started")
                if callable(raw_logger):
                    try:
                        raw_logger(
                            message,
                            event_type="stage",
                            detail={
                                "kind": "stage",
                                "stage": stage.value,
                                "action": action,
                                "event_code": f"registration.{stage.value}.{action}",
                                "schema_version": 2,
                            },
                        )
                        return
                    except TypeError:
                        pass
                ctx.log(f"[{stage.value}:{action}] {message}")

            def _solve_turnstile(page_url: str, site_key: str, **challenge) -> str:
                return ctx.platform.solve_turnstile_with_fallback(
                    page_url,
                    site_key,
                    proxy_url=challenge.get("proxy_url") or ctx.proxy,
                    user_agent=str(challenge.get("user_agent") or profile.get("user_agent") or ""),
                    fingerprint_id=str(profile.get("fingerprint_id") or profile.get("key") or ""),
                    action=str(challenge.get("action") or ""),
                    cdata=str(challenge.get("cdata") or ""),
                    pagedata=str(challenge.get("pagedata") or ""),
                    attempt_id=str(challenge.get("attempt_id") or extra.get("registration_attempt_id") or ""),
                )

            return ChatGPTBrowserRegister(
                headless=(ctx.executor_type == "headless"),
                proxy=ctx.proxy,
                otp_callback=artifacts.otp_callback,
                log_fn=ctx.log,
                browser_profile=profile,
                attempt_id=str(extra.get("registration_attempt_id") or ""),
                artifact_root=extra.get("registration_artifact_dir"),
                turnstile_solver=_solve_turnstile,
                stage_callback=_stage,
            )

        return BrowserRegistrationAdapter(
            result_mapper=lambda ctx, result: self._map_chatgpt_result(result),
            browser_worker_builder=_build_browser_worker,
            browser_register_runner=lambda worker, ctx, artifacts: worker.run(
                email=ctx.identity.email or "",
                password=ctx.password or "",
            ),
            # Fail-fast: do not park a browser worker for 10 minutes on one OTP.
            otp_spec=OtpSpec(wait_message="等待验证码...", timeout=120),
        )
    def build_protocol_mailbox_adapter(self):
        def _build_protocol_worker(ctx, artifacts):
            from platforms.chatgpt.browser_profiles import pick_chrome_profile
            from platforms.chatgpt.protocol_register import ChatGPTProtocolRegister

            extra = dict(ctx.extra or {})
            profile = extra.get("browser_profile")
            if not isinstance(profile, dict) or not profile.get("impersonate"):
                profile = pick_chrome_profile()

            raw_logger = getattr(ctx.platform, "_log_fn", None)

            def _stage(stage, message: str, action: str) -> None:
                if stage.value in {"otp_trigger", "otp_wait"}:
                    mailbox = getattr(ctx.platform, "mailbox", None)
                    marker = getattr(mailbox, "mark_current", None)
                    if callable(marker):
                        marker("side_effect_started")
                if callable(raw_logger):
                    try:
                        raw_logger(
                            message,
                            event_type="stage",
                            detail={
                                "kind": "stage",
                                "stage": stage.value,
                                "action": action,
                                "event_code": f"registration.{stage.value}.{action}",
                                "schema_version": 2,
                            },
                        )
                        return
                    except TypeError:
                        pass
                ctx.log(f"[{stage.value}:{action}] {message}")

            return ChatGPTProtocolRegister(
                proxy=ctx.proxy,
                otp_callback=artifacts.otp_callback,
                log_fn=ctx.log,
                cancel_check=ctx.platform.is_cancel_requested,
                browser_profile=profile,
                impersonate=str(profile.get("impersonate") or ""),
                sentinel_runtime=bool(extra.get("sentinel_browser_runtime", True)),
                stage_callback=_stage,
                attempt_id=str(extra.get("registration_attempt_id") or ""),
            )

        return ProtocolMailboxAdapter(
            result_mapper=lambda ctx, result: self._map_chatgpt_result(
                result,
                password=ctx.password or "",
            ),
            worker_builder=_build_protocol_worker,
            register_runner=lambda worker, ctx, artifacts: worker.run(
                email=ctx.identity.email or "",
                password=ctx.password or "",
            ),
            otp_spec=OtpSpec(
                # ChatGPT's current OTP emails use subjects such as
                # "Your temporary ChatGPT login code" and do not always
                # contain the literal "OpenAI".  The mailbox provider already
                # filters stale messages and extracts a six-digit code, so a
                # sender/brand keyword here only causes valid messages to be
                # discarded.
                keyword="",
                wait_message="等待验证码...",
                # Industry fail-fast: drop dead mailboxes quickly and free the slot.
                timeout=90,
            ),
        )

__all__ = ["ChatGPTPlatform"]

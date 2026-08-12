"""Killable process runner for browser-based registration attempts."""
from __future__ import annotations

from dataclasses import asdict, dataclass
import multiprocessing
import os
import queue
import signal
import subprocess
import threading
import time
import traceback
from typing import Any, Callable

from core.base_platform import Account, AccountStatus


@dataclass(slots=True)
class BrowserProcessRequest:
    platform_name: str
    payload: dict[str, Any]
    resolved_proxy: str | None
    email: str | None
    password: str | None
    heartbeat_seconds: float = 5.0


class BrowserWorkerError(RuntimeError):
    def __init__(self, message: str, *, metadata: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.metadata = dict(metadata or {})
        self.mailbox_resource_id = str(self.metadata.get("mailbox_resource_id") or "")
        self.mailbox_lease_id = str(self.metadata.get("mailbox_lease_id") or "")


class BrowserWorkerTimeout(BrowserWorkerError):
    pass


class BrowserWorkerCancelled(BrowserWorkerError):
    pass


class _QueueLogger:
    def __init__(self, messages) -> None:
        self._messages = messages

    def log(
        self,
        message: str,
        *,
        level: str = "info",
        event_type: str = "log",
        detail: dict | None = None,
    ) -> None:
        _queue_put(
            self._messages,
            {
                "type": "log",
                "message": str(message or ""),
                "level": str(level or "info"),
                "event_type": str(event_type or "log"),
                "detail": dict(detail or {}),
            },
        )


def _queue_put(messages, item: dict[str, Any]) -> None:
    try:
        messages.put(item, timeout=0.25)
    except Exception:
        return


def _serialize_account(account: Account) -> dict[str, Any]:
    data = asdict(account)
    status = data.get("status")
    data["status"] = status.value if isinstance(status, AccountStatus) else str(status or "registered")
    return data


def _build_platform(request: BrowserProcessRequest, logger: _QueueLogger, stop_event):
    from core.base_identity import normalize_identity_provider
    from core.base_mailbox import create_mailbox
    from core.base_platform import RegisterConfig
    from core.registry import get, load_all

    load_all()
    payload = dict(request.payload or {})
    extra = dict(payload.get("extra") or {})
    config = RegisterConfig(
        executor_type=str(payload.get("executor_type") or "headless"),
        captcha_solver=str(payload.get("captcha_solver") or "auto"),
        proxy=request.resolved_proxy,
        extra=extra,
    )
    mailbox = None
    if normalize_identity_provider(extra.get("identity_provider", "mailbox")) == "mailbox":
        if not extra.get("mail_provider"):
            from infrastructure.provider_settings_repository import ProviderSettingsRepository

            extra["mail_provider"] = ProviderSettingsRepository().get_default_provider_key("mailbox")
            config.extra = extra
        mailbox = create_mailbox(
            provider=extra.get("mail_provider", ""),
            extra=extra,
            proxy=request.resolved_proxy,
        )
        from services.registration_mailbox import lease_mailbox_for_attempt

        mailbox = lease_mailbox_for_attempt(
            mailbox,
            owner_attempt_id=str(extra.get("registration_attempt_id") or ""),
            provider=str(extra.get("mail_provider") or ""),
            ttl_seconds=int(extra.get("registration_lease_ttl_seconds") or 300),
        )

    platform = get(request.platform_name)(config=config, mailbox=mailbox)
    platform.set_logger(logger.log)
    platform.set_cancel_checker(stop_event.is_set)
    return platform


def browser_worker_main(request: BrowserProcessRequest, messages, stop_event) -> None:
    """Process entrypoint; it must remain top-level for Windows spawn."""
    if os.name != "nt":
        try:
            os.setsid()
        except OSError:
            pass

    finished = threading.Event()

    def _heartbeat() -> None:
        while not finished.wait(max(float(request.heartbeat_seconds), 1.0)):
            _queue_put(messages, {"type": "heartbeat", "monotonic": time.monotonic()})

    heartbeat = threading.Thread(target=_heartbeat, daemon=True, name="registration-heartbeat")
    heartbeat.start()
    logger = _QueueLogger(messages)
    platform = None
    try:
        platform = _build_platform(request, logger, stop_event)
        account = platform.register(email=request.email, password=request.password)
        _queue_put(messages, {"type": "result", "account": _serialize_account(account)})
    except BaseException as exc:  # process boundary: return a stable error envelope
        artifacts = getattr(exc, "artifacts", None)
        if artifacts is not None:
            try:
                artifacts = asdict(artifacts)
            except (TypeError, ValueError):
                artifacts = dict(artifacts) if isinstance(artifacts, dict) else {}
        identity = getattr(platform, "_last_identity", None) if platform is not None else None
        mailbox_account = getattr(identity, "mailbox_account", None)
        mailbox_email = str(getattr(mailbox_account, "email", "") or "")
        mailbox_extra = dict(getattr(mailbox_account, "extra", {}) or {}) if mailbox_account else {}
        metadata: dict[str, Any] = {}
        if mailbox_email:
            from domain.registration_runtime import stable_resource_ref

            metadata["mailbox_resource_id"] = stable_resource_ref(mailbox_email.lower())
            _local, _separator, domain = mailbox_email.lower().rpartition("@")
            if domain:
                metadata["mailbox_domain_ref"] = stable_resource_ref(domain)
        if mailbox_extra.get("resource_lease_id"):
            metadata["mailbox_lease_id"] = str(mailbox_extra["resource_lease_id"])
        _queue_put(
            messages,
            {
                "type": "error",
                "error": str(exc),
                "error_type": type(exc).__name__,
                "traceback": "".join(traceback.format_exception(exc))[-8000:],
                "artifacts": artifacts or {},
                "metadata": metadata,
            },
        )
    finally:
        finished.set()
        _queue_put(messages, {"type": "stopped"})


def _terminate_process_tree(process: multiprocessing.Process, *, grace_seconds: float = 2.0) -> None:
    if not process.is_alive():
        process.join(timeout=0.2)
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    else:
        try:
            os.killpg(os.getpgid(process.pid), signal.SIGTERM)
        except (ProcessLookupError, PermissionError, OSError):
            process.terminate()
    process.join(timeout=max(float(grace_seconds), 0.1))
    if process.is_alive():
        if os.name != "nt":
            try:
                os.killpg(os.getpgid(process.pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                process.kill()
        else:
            process.kill()
        process.join(timeout=2.0)


class BrowserProcessSupervisor:
    def __init__(
        self,
        *,
        context_name: str = "spawn",
        worker_target: Callable[..., None] = browser_worker_main,
    ) -> None:
        self._context_name = context_name
        self._worker_target = worker_target

    def run(
        self,
        request: BrowserProcessRequest,
        *,
        timeout_seconds: float,
        cancel_check: Callable[[], bool],
        log_callback: Callable[..., None],
        heartbeat_timeout_seconds: float = 15.0,
    ) -> Account:
        context = multiprocessing.get_context(self._context_name)
        messages = context.Queue(maxsize=1000)
        stop_event = context.Event()
        process = context.Process(
            target=self._worker_target,
            args=(request, messages, stop_event),
            daemon=False,
            name=f"browser-register-{request.payload.get('executor_type', 'headless')}",
        )
        process.start()
        deadline = time.monotonic() + max(float(timeout_seconds), 1.0)
        last_heartbeat = time.monotonic()
        result: dict[str, Any] | None = None
        error: dict[str, Any] | None = None

        try:
            while time.monotonic() < deadline:
                if cancel_check():
                    stop_event.set()
                    raise BrowserWorkerCancelled("browser registration cancelled")
                try:
                    item = messages.get(timeout=0.25)
                except queue.Empty:
                    if not process.is_alive():
                        break
                    if time.monotonic() - last_heartbeat > heartbeat_timeout_seconds:
                        raise BrowserWorkerTimeout("browser worker heartbeat timed out")
                    continue

                message_type = str(item.get("type") or "")
                if message_type == "heartbeat":
                    last_heartbeat = time.monotonic()
                elif message_type == "log":
                    log_callback(
                        item.get("message", ""),
                        level=item.get("level", "info"),
                        event_type=item.get("event_type", "log"),
                        detail=item.get("detail") or None,
                    )
                elif message_type == "result":
                    result = dict(item.get("account") or {})
                    break
                elif message_type == "error":
                    error = item
                    break

            if result is None and error is None:
                if time.monotonic() >= deadline:
                    raise BrowserWorkerTimeout(
                        f"browser registration exceeded {int(timeout_seconds)}s deadline"
                    )
                raise BrowserWorkerError(
                    f"browser worker exited without result (exit_code={process.exitcode})"
                )
            if error is not None:
                artifact_bundle = dict(error.get("artifacts") or {})
                if artifact_bundle:
                    from infrastructure.registration_repository import registration_artifacts

                    extra = dict(request.payload.get("extra") or {})
                    records = registration_artifacts.add_bundle(
                        task_id=str(extra.get("registration_task_id") or ""),
                        attempt_id=str(extra.get("registration_attempt_id") or ""),
                        bundle=artifact_bundle,
                    )
                    log_callback(
                        "浏览器失败证据已登记",
                        level="error",
                        event_type="diagnostic",
                        detail={
                            "kind": "diagnostic",
                            "action": "artifact_bundle",
                            "artifact_ids": [item["id"] for item in records],
                            "schema_version": 2,
                        },
                    )
                raise BrowserWorkerError(
                    str(error.get("error") or error.get("error_type") or "browser worker failed"),
                    metadata=dict(error.get("metadata") or {}),
                )

            status_value = str(result.pop("status", "registered") or "registered")
            try:
                status = AccountStatus(status_value)
            except ValueError:
                status = AccountStatus.REGISTERED
            return Account(status=status, **result)
        finally:
            stop_event.set()
            process.join(timeout=3.0)
            if process.is_alive():
                _terminate_process_tree(process)
            try:
                messages.close()
                messages.join_thread()
            except Exception:
                pass


browser_process_supervisor = BrowserProcessSupervisor()

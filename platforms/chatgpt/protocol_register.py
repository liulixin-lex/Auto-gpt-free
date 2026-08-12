"""ChatGPT email registration through the OpenAI web protocol.

Signup remains direct HTTP; a hidden Chromium page is used only to execute the
official Sentinel JavaScript required for the create-account security token.
"""
from __future__ import annotations

import base64
import json
import random
import threading
import time
import uuid
from datetime import datetime, timedelta
from typing import Callable
from urllib.parse import urlencode, urljoin, urlparse

from curl_cffi import requests

from domain.registration_runtime import (
    RegistrationStage,
    classify_registration_error,
    redact_registration_text,
)

from .browser_profiles import api_headers, navigate_headers, pick_chrome_profile
from .constants import (
    CHATGPT_APP,
    CODEX_CLIENT_ID,
    CODEX_REDIRECT_URI,
    CODEX_SCOPE,
    OPENAI_API_ENDPOINTS,
    OPENAI_AUTH,
    OAUTH_TOKEN_URL,
    SENTINEL_BASE,
    SENTINEL_FRAME_URL,
    SENTINEL_REQ_URL,
    SENTINEL_SDK_URL,
)
from .protocol import (
    OtpCoordinator,
    OAuthPkceClient,
    ProtocolStateMachine,
    ProtocolTransport,
    SentinelSdkDriftError,
    SentinelSdkResolver,
    SessionResolver,
)

FIRST_NAMES = (
    "James", "John", "Robert", "Michael", "David", "William", "Richard",
    "Joseph", "Thomas", "Daniel", "Matthew", "Anthony", "Mary", "Linda",
    "Jennifer", "Sarah", "Jessica", "Elizabeth",
)
LAST_NAMES = (
    "Smith", "Johnson", "Williams", "Brown", "Jones", "Garcia", "Miller",
    "Davis", "Wilson", "Anderson", "Taylor", "Thomas", "Moore", "Martin",
    "Lee", "White",
)


def _random_profile() -> tuple[str, str]:
    name = f"{random.choice(FIRST_NAMES)} {random.choice(LAST_NAMES)}"
    age = random.randint(24, 36)
    birthdate = (datetime.now() - timedelta(days=age * 365)).strftime("%Y-%m-%d")
    return name, birthdate


def _decode_jwt_payload(token: str) -> dict:
    try:
        parts = str(token or "").split(".")
        if len(parts) < 2:
            return {}
        payload = parts[1] + "=" * (-len(parts[1]) % 4)
        return json.loads(base64.urlsafe_b64decode(payload.encode("ascii")))
    except Exception:
        return {}


def _response_json(response) -> dict:
    try:
        payload = response.json()
        return payload if isinstance(payload, dict) else {}
    except Exception:
        return {}


def _response_error(response, payload: dict | None = None) -> str:
    data = payload or _response_json(response)
    error = data.get("error") if isinstance(data, dict) else None
    if isinstance(error, dict):
        code = str(error.get("code") or "").strip()
        message = str(error.get("message") or "").strip()
        if code and message and code not in message:
            return f"{code}: {message}"
        if message or code:
            return message or code
    if isinstance(error, str) and error:
        return error
    text = str(getattr(response, "text", "") or "").strip()
    return text[:300] or f"HTTP {getattr(response, 'status_code', 0)}"


def _auth_flow_state(payload: dict | None = None, current_url: str = "") -> dict:
    """Normalize OpenAI auth JSON/redirect responses into one small state."""

    raw = payload if isinstance(payload, dict) else {}
    page = raw.get("page") if isinstance(raw.get("page"), dict) else {}
    page_payload = page.get("payload") if isinstance(page.get("payload"), dict) else {}
    continue_url = str(raw.get("continue_url") or page_payload.get("url") or "").strip()
    if continue_url:
        continue_url = urljoin(OPENAI_AUTH, continue_url)
    effective_url = continue_url or str(current_url or "")
    page_type = str(page.get("type") or "").strip().lower()
    page_type = page_type.replace("-", "_").replace("/", "_").replace(" ", "_")
    lowered_url = effective_url.lower()
    if not page_type:
        if "create-account/password" in lowered_url:
            page_type = "create_account_password"
        elif "email-verification" in lowered_url or "email-otp" in lowered_url:
            page_type = "email_otp_verification"
        elif "about-you" in lowered_url:
            page_type = "about_you"
        elif "log-in/password" in lowered_url:
            page_type = "login_password"
        elif "log-in" in lowered_url:
            page_type = "login"
        elif "code=" in lowered_url:
            page_type = "oauth_callback"
    return {
        "page_type": page_type,
        "continue_url": continue_url,
        "current_url": effective_url,
        "method": str(raw.get("method") or page_payload.get("method") or "GET").upper(),
        "raw": raw,
    }


class _SentinelTokenGenerator:
    """Generate the requirements/enforcement PoW used by OpenAI Sentinel."""

    def __init__(self, user_agent: str):
        self.user_agent = user_agent
        self.sid = str(uuid.uuid4())

    @staticmethod
    def _fnv1a32(text: str) -> str:
        value = 2166136261
        for char in text:
            value ^= ord(char)
            value = (value * 16777619) & 0xFFFFFFFF
        value ^= value >> 16
        value = (value * 2246822507) & 0xFFFFFFFF
        value ^= value >> 13
        value = (value * 3266489909) & 0xFFFFFFFF
        value ^= value >> 16
        return f"{value & 0xFFFFFFFF:08x}"

    @staticmethod
    def _encode(value) -> str:
        raw = json.dumps(value, separators=(",", ":")).encode("utf-8")
        return base64.b64encode(raw).decode("ascii")

    def _fingerprint(self) -> list:
        perf_now = 1000 + random.random() * 49000
        return [
            "1920x1080",
            time.strftime(
                "%a, %d %b %Y %H:%M:%S GMT+0000 (Coordinated Universal Time)",
                time.gmtime(),
            ),
            4294705152,
            random.random(),
            self.user_agent,
            SENTINEL_SDK_URL,
            None,
            None,
            "en-US",
            "en-US,en",
            random.random(),
            "webkitTemporaryStorage−undefined",
            "location",
            "Object",
            perf_now,
            self.sid,
            "",
            random.choice((4, 8, 12, 16)),
            int(time.time() * 1000 - perf_now),
        ]

    def _reference_fingerprint(self) -> list:
        """25-field fingerprint used by the current Sentinel SDK."""
        now = datetime.now().astimezone()
        perf_now = round(
            time.time() * 1000 - 1_000_000 + random.uniform(1000, 5000), 1
        )
        time_origin = round(time.time() * 1000 - 50_000, 1)
        return [
            3000,
            str(now),
            4294705152,
            0,
            self.user_agent,
            SENTINEL_SDK_URL,
            None,
            "en-US",
            "en-US,en",
            0,
            "webkitTemporaryStorage\u2212undefined",
            "location",
            "Object",
            perf_now,
            self.sid,
            "",
            8,
            time_origin,
            0,
            0,
            0,
            0,
            0,
            0,
            0,
        ]

    def _solve_reference_pow(self, seed: str, difficulty: str, data: list) -> str:
        started = time.perf_counter()
        target = str(difficulty or "0")
        for nonce in range(500_000):
            data[3] = nonce
            data[9] = round((time.perf_counter() - started) * 1000)
            encoded = self._encode(data)
            digest = self._fnv1a32(str(seed or "") + encoded)
            if digest[: len(target)] <= target:
                return encoded + "~S"
        return self._encode("e")

    def requirements(self) -> str:
        config = self._reference_fingerprint()
        config[3] = 1
        config[9] = round(5 + random.random() * 45)
        return "gAAAAAC" + self._solve_reference_pow(
            str(random.random()), "0", config
        )

    def enforcement(self, seed: str, difficulty: str) -> str:
        return "gAAAAAB" + self._solve_reference_pow(
            seed, difficulty, self._reference_fingerprint()
        )


class _SentinelBrowserRuntime:
    """Run the official Sentinel SDK in a hidden browser context.

    The signup flow itself remains direct curl_cffi HTTP. Chromium is used
    only as the JavaScript/browser runtime required to produce Sentinel's
    encrypted turnstile proof and optional session-observer token.
    """

    def __init__(
        self,
        session,
        *,
        user_agent: str,
        proxy: str | None,
        profile: dict | None = None,
    ):
        from playwright.sync_api import sync_playwright

        self.profile = dict(profile or {})
        self._playwright = None
        self._browser = None
        self._context = None
        self._sdk_code = ""
        self._sdk_hash = ""
        try:
            self._playwright = sync_playwright().start()
            launch_options: dict = {"headless": True}
            if proxy:
                parsed = urlparse(proxy)
                if parsed.scheme and parsed.hostname:
                    server = f"{parsed.scheme}://{parsed.hostname}"
                    if parsed.port:
                        server += f":{parsed.port}"
                    proxy_options = {"server": server}
                    if parsed.username:
                        proxy_options["username"] = parsed.username
                    if parsed.password:
                        proxy_options["password"] = parsed.password
                    launch_options["proxy"] = proxy_options
            self._browser = self._playwright.chromium.launch(**launch_options)
            self._context = self._browser.new_context(
                user_agent=user_agent,
                locale=str(self.profile.get("locale") or "en-US"),
                timezone_id=str(self.profile.get("timezone_id") or "America/New_York"),
                viewport=dict(self.profile.get("viewport") or {"width": 1366, "height": 768}),
                screen=dict(
                    self.profile.get("screen")
                    or self.profile.get("viewport")
                    or {"width": 1366, "height": 768}
                ),
            )
            hardware_concurrency = int(self.profile.get("hardware_concurrency") or 8)
            device_memory = int(self.profile.get("device_memory") or 8)
            self._context.add_init_script(
                f"""
                Object.defineProperty(navigator, 'hardwareConcurrency', {{get: () => {hardware_concurrency}}});
                Object.defineProperty(navigator, 'deviceMemory', {{get: () => {device_memory}}});
                """
            )

            hook = "t.token=ye,t}({});"
            bundle = SentinelSdkResolver(
                session,
                fallback_url=SENTINEL_SDK_URL,
                bootstrap_url=f"{OPENAI_AUTH}/about-you",
            ).load(compatibility_hook=hook)
            replacement = (
                "t.___n=_n,t.__Nt=Nt,t.__D=D,t.__jt=jt,"
                "t.token=ye,t}({});"
            )
            self._sdk_code = bundle.content.replace(hook, replacement)
            self._sdk_hash = bundle.sha256
            page = self._new_sdk_page(required=("__D", "___n", "__Nt", "__jt", "token"))
            page.close()
        except Exception:
            try:
                self.close()
            except Exception:
                pass
            raise

    def _new_sdk_page(self, *, required: tuple[str, ...]):
        if self._context is None:
            raise RuntimeError("Sentinel browser context is closed")
        page = self._context.new_page()
        try:
            page.goto(
                f"{OPENAI_AUTH}/about-you",
                wait_until="domcontentloaded",
                timeout=30_000,
            )
            page.evaluate(
                """code => {
                    window.eval(code);
                    window.__RegistrationSentinelSDK = window.SentinelSDK;
                }""",
                self._sdk_code,
            )
            contract = page.evaluate(
                """names => {
                    const sdk = window.__RegistrationSentinelSDK;
                    const result = { sdk: typeof sdk };
                    for (const name of names) result[name] = typeof sdk?.[name];
                    return result;
                }""",
                list(required),
            )
            missing = [
                name
                for name in required
                if not isinstance(contract, dict) or contract.get(name) != "function"
            ]
            if not isinstance(contract, dict) or contract.get("sdk") != "object":
                missing.insert(0, "SentinelSDK")
            if missing:
                SentinelSdkResolver.report_runtime_drift(
                    self._sdk_hash,
                    missing=missing,
                )
            return page
        except Exception:
            try:
                page.close()
            except Exception:
                pass
            raise

    @staticmethod
    def _looks_like_vm_error(value: str) -> bool:
        try:
            decoded = base64.b64decode(value + "=" * (-len(value) % 4)).decode(
                "utf-8", errors="ignore"
            )
        except Exception:
            return False
        lowered = decoded.lower()
        return "syntaxerror" in lowered or "typeerror" in lowered or "error:" in lowered

    def vm_tokens(self, chat_req: dict, cached_proof: str) -> dict[str, str]:
        page = self._new_sdk_page(required=("__D", "___n", "__Nt", "__jt"))
        try:
            result = page.evaluate(
                """async ({ chatReq, cachedProof }) => {
                    const sdk = window.__RegistrationSentinelSDK;
                    sdk.__D(chatReq, cachedProof);
                    const turnstile = chatReq.turnstile || {};
                    const t = turnstile.dx
                        ? await sdk.___n(chatReq, turnstile.dx)
                        : null;
                    let so = null;
                    const observer = chatReq.so || {};
                    if (observer.collector_dx) so = await sdk.__Nt(observer.collector_dx);
                    let soFallback = null;
                    if (observer.snapshot_dx) {
                        soFallback = await sdk.__jt(observer.snapshot_dx, cachedProof);
                    }
                    return { t, so, soFallback };
                }""",
                {"chatReq": chat_req, "cachedProof": cached_proof},
            )
        finally:
            page.close()
        t_value = str((result or {}).get("t") or "")
        if (chat_req.get("turnstile", {}).get("required") and not t_value):
            raise RuntimeError("Sentinel Turnstile VM 未生成 t token")
        so_value = str((result or {}).get("so") or "")
        if so_value and self._looks_like_vm_error(so_value):
            so_value = ""
        if not so_value:
            fallback = str((result or {}).get("soFallback") or "")
            if fallback and not self._looks_like_vm_error(fallback):
                so_value = fallback
        return {"t": t_value, "so": so_value}

    def token_headers(self, flow: str) -> dict[str, str]:
        page = self._new_sdk_page(required=("token",))
        try:
            result = page.evaluate(
                """async flow => {
                    const sdk = window.__RegistrationSentinelSDK;
                    const token = await sdk.token(flow);
                    let so = null;
                    if (typeof sdk.sessionObserverToken === "function") {
                        so = await sdk.sessionObserverToken(flow);
                    }
                    return { token, so };
                }""",
                flow,
            )
        finally:
            page.close()
        token = result.get("token") if isinstance(result, dict) else None
        if isinstance(token, str):
            try:
                token = json.loads(token)
            except json.JSONDecodeError as exc:
                raise RuntimeError("Sentinel SDK 返回的 token 不是 JSON") from exc
        if not isinstance(token, dict):
            raise RuntimeError("Sentinel SDK 未返回 token")
        missing = [
            key for key in ("p", "t", "c", "id", "flow")
            if not str(token.get(key) or "")
        ]
        if missing:
            raise RuntimeError("Sentinel token 缺少字段: " + ", ".join(missing))

        headers = {
            "openai-sentinel-token": json.dumps(token, separators=(",", ":")),
        }
        so = result.get("so") if isinstance(result, dict) else None
        if isinstance(so, str):
            try:
                so = json.loads(so)
            except json.JSONDecodeError:
                so = None
        if isinstance(so, dict) and so:
            headers["openai-sentinel-so-token"] = json.dumps(
                so, separators=(",", ":")
            )
        return headers

    def close(self) -> None:
        errors: list[str] = []
        for name, resource, method_name in (
            ("context", getattr(self, "_context", None), "close"),
            ("browser", getattr(self, "_browser", None), "close"),
            ("playwright", getattr(self, "_playwright", None), "stop"),
        ):
            try:
                if resource is not None:
                    getattr(resource, method_name)()
            except Exception as exc:
                errors.append(f"{name}:{type(exc).__name__}")
            finally:
                setattr(self, f"_{name}", None)
        if errors:
            raise RuntimeError("Sentinel runtime cleanup failed " + ",".join(errors))


class OpenAISentinelClient:
    """Sentinel token client backed by an isolated, profile-bound JS runtime."""

    def __init__(
        self,
        session,
        *,
        user_agent: str,
        proxy: str | None = None,
        profile: dict | None = None,
        use_browser_runtime: bool = True,
        log_fn: Callable[[str], None] | None = None,
        transport: ProtocolTransport | None = None,
    ):
        self.session = session
        self.user_agent = user_agent
        self.proxy = proxy
        self.profile = dict(profile or {})
        self.use_browser_runtime = use_browser_runtime
        self.log = log_fn or (lambda _m: None)
        self.transport = transport or ProtocolTransport(session)
        self._browser_runtime: _SentinelBrowserRuntime | None = None
        self._browser_failed = False

    def build_headers(self, device_id: str, flow: str) -> dict[str, str]:
        if not self.use_browser_runtime:
            raise RuntimeError("Sentinel proof requires the isolated SDK runtime")
        if self._browser_failed:
            raise RuntimeError("Sentinel isolated SDK runtime is unavailable")
        last_error: Exception | None = None
        for attempt in range(2):
            try:
                return self._build_browser_headers(device_id, flow)
            except Exception as exc:
                if isinstance(exc, SentinelSdkDriftError):
                    raise
                last_error = exc
                try:
                    if self._browser_runtime is not None:
                        self._browser_runtime.close()
                except Exception as cleanup_exc:
                    detail = redact_registration_text(cleanup_exc)[:160]
                    self.log(
                        "Sentinel runtime cleanup failed during rebuild "
                        f"type={type(cleanup_exc).__name__} detail={detail or '-'}"
                    )
                self._browser_runtime = None
                if attempt == 0:
                    detail = redact_registration_text(exc)[:160]
                    self.log(
                        "Sentinel SDK runtime 首次生成失败，重建隔离运行时后重试 "
                        f"type={type(exc).__name__} detail={detail or '-'}"
                    )
                    continue
                self._browser_failed = True
        detail = redact_registration_text(last_error)[:160]
        raise RuntimeError(
            "Sentinel isolated SDK runtime failed "
            f"type={type(last_error).__name__ if last_error else 'unknown'} "
            f"detail={detail or '-'}"
        ) from last_error

    def build_header(self, device_id: str, flow: str) -> str:
        return self.build_headers(device_id, flow)["openai-sentinel-token"]

    def warmup(self) -> None:
        """Start the isolated SDK runtime before the short-lived auth session."""

        if not self.use_browser_runtime:
            raise RuntimeError("Sentinel proof requires the isolated SDK runtime")
        if self._browser_failed:
            raise RuntimeError("Sentinel isolated SDK runtime is unavailable")
        if self._browser_runtime is None:
            self._browser_runtime = _SentinelBrowserRuntime(
                self.session,
                user_agent=self.user_agent,
                proxy=self.proxy,
                profile=self.profile,
            )

    def _challenge_request(self, device_id: str, flow: str, proof: str) -> tuple[object, dict]:
        response = self.transport.post(
            SENTINEL_REQ_URL,
            side_effect=False,
            data=json.dumps({"p": proof, "id": device_id, "flow": flow}),
            headers={
                "accept": "*/*",
                "content-type": "text/plain;charset=UTF-8",
                "origin": SENTINEL_BASE,
                "referer": SENTINEL_FRAME_URL,
                "user-agent": self.user_agent,
            },
        )
        payload = _response_json(response)
        return response, payload

    def _build_browser_headers(self, device_id: str, flow: str) -> dict[str, str]:
        generator = _SentinelTokenGenerator(self.user_agent)
        proof = generator.requirements()
        response, chat_req = self._challenge_request(device_id, flow, proof)
        challenge = str(chat_req.get("token") or "").strip()
        if getattr(response, "status_code", 0) >= 400 or not challenge:
            raise RuntimeError(
                f"Sentinel challenge 获取失败: {_response_error(response, chat_req)}"
            )
        if self._browser_runtime is None:
            self._browser_runtime = _SentinelBrowserRuntime(
                self.session,
                user_agent=self.user_agent,
                proxy=self.proxy,
                profile=self.profile,
            )
        vm = self._browser_runtime.vm_tokens(chat_req, proof)
        pow_info = chat_req.get("proofofwork") or {}
        if pow_info.get("required") and pow_info.get("seed"):
            enforcement = generator.enforcement(
                str(pow_info.get("seed") or ""),
                str(pow_info.get("difficulty") or "0"),
            )
        else:
            enforcement = proof
        token = {
            "p": enforcement,
            "t": vm.get("t") or "",
            "c": challenge,
            "id": device_id,
            "flow": flow,
        }
        headers = {
            "openai-sentinel-token": json.dumps(token, separators=(",", ":"))
        }
        if vm.get("so"):
            so_token = {
                "so": vm["so"],
                "c": challenge,
                "id": device_id,
                "flow": flow,
            }
            headers["openai-sentinel-so-token"] = json.dumps(
                so_token, separators=(",", ":")
            )
        return headers

    def close(self) -> None:
        runtime = self._browser_runtime
        self._browser_runtime = None
        if runtime is not None:
            runtime.close()


class ChatGPTProtocolRegister:
    """Synchronous worker compatible with ``ProtocolMailboxAdapter``."""

    def __init__(
        self,
        *,
        proxy: str | None = None,
        otp_callback: Callable[[], str] | None = None,
        log_fn: Callable[[str], None] | None = None,
        cancel_check: Callable[[], bool] | None = None,
        impersonate: str = "",
        session=None,
        sentinel_runtime: bool = True,
        browser_profile: dict | None = None,
        stage_callback: Callable[[RegistrationStage, str, str], None] | None = None,
        oauth_pkce: bool = True,
        oauth_client_factory: Callable[..., OAuthPkceClient] | None = None,
        attempt_id: str = "",
    ):
        self.proxy = str(proxy or "").strip() or None
        self.otp_callback = otp_callback
        self.log = log_fn or (lambda _message: None)
        self.cancel_check = cancel_check or (lambda: False)
        self.oauth_pkce_enabled = bool(oauth_pkce)
        self.oauth_client_factory = oauth_client_factory or OAuthPkceClient
        self._owns_session = session is None
        self._sentinel_runtime_enabled = bool(sentinel_runtime)
        self._stage_callback = stage_callback
        self.profile = dict(browser_profile or pick_chrome_profile())
        from domain.registration_runtime import stable_resource_ref

        self.profile.setdefault("proxy_lease_id", stable_resource_ref(self.proxy))
        self.attempt_id = str(attempt_id or uuid.uuid4()).strip()
        base_fingerprint = str(
            self.profile.get("fingerprint_id") or self.profile.get("key") or "default"
        )
        self.profile["fingerprint_id"] = (
            f"{base_fingerprint}:{stable_resource_ref(self.attempt_id)}"
        )
        if impersonate:
            self.profile["impersonate"] = impersonate
        self.device_id = str(uuid.uuid4())
        from domain.transport_identity import build_transport_identity

        self.transport_identity = build_transport_identity(
            self.profile,
            proxy_url=self.proxy,
            device_id=self.device_id,
        )
        self.profile = self.transport_identity.apply(self.profile)
        self.user_agent = self.transport_identity.user_agent
        if session is None:
            kwargs = {
                # The transport identity is the single source of truth.  A
                # separate fallback here (previously chrome136) could leave
                # curl TLS on one Chrome major while Sentinel/profile state
                # advertised another major.
                "impersonate": self.transport_identity.curl_impersonate,
                "timeout": 60,
            }
            if self.proxy:
                kwargs["proxies"] = {"http": self.proxy, "https": self.proxy}
            session = requests.Session(**kwargs)
        self.session = session
        self.transport = ProtocolTransport(session, cancel_check=self.cancel_check)
        self.state_machine = ProtocolStateMachine(
            emit=stage_callback
            or (lambda stage, message, action: self.log(f"[{stage.value}:{action}] {message}"))
        )
        self.sentinel = OpenAISentinelClient(
            session,
            user_agent=self.user_agent,
            proxy=self.proxy,
            profile=self.profile,
            use_browser_runtime=sentinel_runtime,
            log_fn=self.log,
            transport=self.transport,
        )
        self.profile_key = str(self.profile.get("key") or "")

    def _replace_owned_transport_session(self, *, host: str) -> None:
        """Recreate curl/TLS transport after a real clearance fixes the UA."""
        if not self._owns_session:
            raise RuntimeError(
                "AUTH_SESSION_DESYNC: external protocol session cannot be rebound"
            )
        from domain.transport_identity import build_transport_identity

        previous = self.session
        # Rebuild the immutable identity before opening the replacement
        # session.  The replacement must use exactly the UA/TLS profile that
        # clearance alignment selected, never a hard-coded Chrome major.
        self.transport_identity = build_transport_identity(
            self.profile,
            proxy_url=self.proxy,
            device_id=self.device_id,
        )
        self.profile = self.transport_identity.apply(self.profile)
        self.user_agent = self.transport_identity.user_agent or str(
            self.profile.get("user_agent") or self.user_agent
        )
        kwargs = {
            "impersonate": self.transport_identity.curl_impersonate,
            "timeout": 60,
        }
        if self.proxy:
            kwargs["proxies"] = {"http": self.proxy, "https": self.proxy}
        replacement = requests.Session(**kwargs)
        copied = 0
        try:
            jar = getattr(getattr(previous, "cookies", None), "jar", None)
            if jar is not None:
                for cookie in jar:
                    replacement.cookies.set(
                        cookie.name,
                        cookie.value,
                        domain=cookie.domain or None,
                        path=cookie.path or "/",
                    )
                    copied += 1
        except Exception:
            copied = 0
        if copied == 0:
            try:
                for name, value in dict(previous.cookies.get_dict()).items():
                    replacement.cookies.set(name, value)
            except Exception:
                pass
        try:
            self.sentinel.close()
        except Exception:
            pass
        try:
            previous.close()
        except Exception:
            pass
        self.session = replacement
        self.transport = ProtocolTransport(replacement, cancel_check=self.cancel_check)
        self.profile_key = str(self.profile.get("key") or "")
        self.sentinel = OpenAISentinelClient(
            replacement,
            user_agent=self.user_agent,
            proxy=self.proxy,
            profile=self.profile,
            use_browser_runtime=self._sentinel_runtime_enabled,
            log_fn=self.log,
            transport=self.transport,
        )
        self.log(f"Protocol transport identity rebound before side effects host={host}")

    def _check_cancelled(self) -> None:
        if self.cancel_check():
            raise RuntimeError("任务已取消")

    def _common_headers(self, referer: str) -> dict:
        # Cloudflare transport cookies are already stored in this attempt's
        # cookie jar. Do not inject FlareSolverr's unrelated identity cookies
        # (for example a foreign oai-did) into the OpenAI auth session.
        return api_headers(
            self.profile,
            origin=OPENAI_AUTH,
            referer=referer,
            content_type="application/json",
        )

    def _get(self, url: str, **kwargs):
        return self.transport.get(url, **kwargs)

    def _post(self, url: str, *, side_effect: bool = True, **kwargs):
        return self.transport.post(url, side_effect=side_effect, **kwargs)

    def _follow_authorize_chain(self, location: str) -> str:
        from core.proxy_runtime import validate_upstream_target
        from domain.challenge_runtime import ChallengeClassifier, ChallengeKind
        from domain.registration_runtime import RegistrationErrorCode

        current = str(location or "").strip()
        last_url = urljoin(OPENAI_AUTH, current) if current else ""
        referer = f"{OPENAI_AUTH}/"
        auth_host = urlparse(OPENAI_AUTH).hostname or "auth.openai.com"
        for _ in range(15):
            if not current:
                return last_url
            self._check_cancelled()
            target_url = urljoin(OPENAI_AUTH, current)
            target_host = urlparse(target_url).hostname or auth_host
            try:
                target_host, target_url = validate_upstream_target(target_host, target_url)
            except ValueError as exc:
                raise RuntimeError(
                    "AUTH_REDIRECT: OpenAI authorization target is not allowed"
                ) from exc
            response = None
            for challenge_attempt in range(2):
                response = self._get(
                    target_url,
                    allow_redirects=False,
                    headers=navigate_headers(self.profile, referer=referer),
                )
                status = int(getattr(response, "status_code", 0) or 0)
                body = str(getattr(response, "text", "") or "")[:4000]
                content_type = str(
                    getattr(response, "headers", {}).get("content-type") or ""
                ).split(";", 1)[0]
                response_url = str(getattr(response, "url", "") or target_url)
                classification = ChallengeClassifier.classify(
                    status_code=status,
                    url=response_url,
                    headers=getattr(response, "headers", {}),
                    body=body,
                )
                # A normal OpenAI auth page contains Sentinel SDK references.
                # Sentinel evidence is not a Cloudflare navigation block.
                blocked = classification.kind in {
                    ChallengeKind.CLOUDFLARE_MANAGED,
                    ChallengeKind.TURNSTILE,
                } or (
                    status == 403
                    and classification.error_code == RegistrationErrorCode.CF_CHALLENGE
                )
                response_path = urlparse(response_url).path or "/"
                has_location = bool(
                    str(getattr(response, "headers", {}).get("location") or "").strip()
                )
                self.log(
                    "OpenAI 授权跳转 "
                    f"host={target_host} path={response_path} status={status} "
                    f"content_type={content_type or '-'} location={'yes' if has_location else 'no'}"
                )
                if not blocked:
                    break
                if challenge_attempt == 0:
                    self.log(f"OpenAI 授权入口触发 Cloudflare，刷新 {target_host} 过盾后重试")
                    self._bind_clearance(
                        host=target_host,
                        force=True,
                        required=True,
                        target_url=target_url,
                    )
                    continue
                raise RuntimeError(
                    "CF_CHALLENGE: OpenAI 授权入口仍被 Cloudflare 拦截 "
                    f"host={target_host} status={status}"
                )
            if response is None:
                raise RuntimeError("AUTH_REDIRECT: OpenAI 授权入口无响应")
            status = int(getattr(response, "status_code", 0) or 0)
            if status >= 400:
                raise RuntimeError(
                    "AUTH_REDIRECT: OpenAI 授权跳转失败 "
                    f"host={target_host} status={status}"
                )
            last_url = str(getattr(response, "url", "") or target_url)
            current = str(response.headers.get("location") or "").strip()
            referer = target_url
        raise RuntimeError("OpenAI 授权重定向次数过多")

    def _bind_clearance(
        self,
        *,
        host: str = "",
        force: bool = False,
        required: bool = False,
        target_url: str = "",
    ) -> bool:
        """Bind only identity-compatible, transferable Cloudflare clearance."""
        try:
            from core.proxy_runtime import apply_clearance_to_profile

            host = str(host or urlparse(CHATGPT_APP).hostname or "chatgpt.com").strip().lower()
            try:
                bundle = apply_clearance_to_profile(
                    self.profile,
                    host,
                    force=force,
                    proxy_url=self.proxy,
                    require_clearance=required,
                    target_url=target_url,
                )
            except TypeError as exc:
                # Keep compatibility with injected/test adapters that predate the
                # strict-clearance keyword while the production adapter adopts it.
                if not any(name in str(exc) for name in ("require_clearance", "target_url")):
                    raise
                try:
                    bundle = apply_clearance_to_profile(
                        self.profile,
                        host,
                        force=force,
                        proxy_url=self.proxy,
                        require_clearance=required,
                    )
                except TypeError as fallback_exc:
                    if "require_clearance" not in str(fallback_exc):
                        raise
                    bundle = apply_clearance_to_profile(
                        self.profile,
                        host,
                        force=force,
                        proxy_url=self.proxy,
                    )
            status = str(bundle.get("status") or "")
            if not status:
                if bool(bundle.get("has_cf_clearance")):
                    status = "valid_clearance"
                elif required:
                    status = "clearance_missing"
                else:
                    status = "not_required"
            # `not_required` is a successful solver outcome when the target
            # returned a normal page.  Only an explicitly detected challenge
            # without cf_clearance is a hard failure.
            challenge_detected = bool(bundle.get("challenge_detected", False))
            if required and status not in {"valid_clearance", "not_required"}:
                raise RuntimeError(
                    f"CF_CLEARANCE_UNAVAILABLE: host={host} status={status or 'solver_error'}"
                )
            if required and status == "not_required" and challenge_detected:
                raise RuntimeError(
                    f"CF_CLEARANCE_UNAVAILABLE: host={host} status=clearance_missing"
                )
            bundle_ua = str(bundle.get("user_agent") or "")
            # When the real transport has just been challenged, a solver may
            # legitimately return a normal page without a cookie.  Its UA is
            # still the only concrete identity we can use to make the next
            # curl request comparable to the solver browser.  Align and
            # rebuild before retrying the challenged URL; ordinary optional
            # probes keep the old identity and do not bind solver cookies.
            if (
                bundle_ua
                and bundle_ua != self.user_agent
                and (status == "valid_clearance" or required)
            ):
                from platforms.chatgpt.browser_profiles import align_chrome_profile_to_user_agent

                self.profile.update(
                    align_chrome_profile_to_user_agent(self.profile, bundle_ua)
                )
                self._replace_owned_transport_session(host=host)
            cookie = str(bundle.get("cookie") or "").strip()
            if cookie and status == "valid_clearance":
                # Seed session jar so subsequent API calls keep clearance.
                for part in cookie.split(";"):
                    part = part.strip()
                    if not part or "=" not in part:
                        continue
                    name, value = part.split("=", 1)
                    name, value = name.strip(), value.strip()
                    lowered_name = name.lower()
                    is_cf_transport_cookie = (
                        lowered_name == "cf_clearance"
                        or lowered_name == "_cfuvid"
                        or lowered_name.startswith("__cf")
                        or lowered_name.startswith("cf_")
                    )
                    if not name or not is_cf_transport_cookie:
                        continue
                    try:
                        self.session.cookies.set(name, value, domain=f".{host}")
                    except Exception:
                        try:
                            self.session.cookies.set(name, value)
                        except Exception:
                            pass
                src = bundle.get("source") or "clearance"
                self.log(
                    f"CF 过盾已绑定 host={host} source={src} "
                    f"cf_clearance={'yes' if bundle.get('has_cf_clearance') else 'no'}"
                )
                return True
            if status == "not_required":
                self.log(f"CF 未触发挑战 host={host}，继续使用当前传输身份")
                return True
        except Exception as exc:
            if required or "AUTH_SESSION_DESYNC" in str(exc):
                raise
            self.log(f"CF 过盾绑定跳过: {exc}")
        return False

    def _nav_headers(self, *, referer: str = "", force_clearance: bool = False) -> dict:
        del force_clearance
        return navigate_headers(self.profile, referer=referer)

    def _sync_device_id_from_cookie(self) -> None:
        try:
            cookie_device_id = str(self.session.cookies.get("oai-did") or "").strip()
            if cookie_device_id:
                self.device_id = cookie_device_id
        except Exception:
            pass

    def _bootstrap_chatgpt_homepage(self) -> None:
        """Visit the full homepage only after the API-first path is unavailable."""
        last_err = ""
        response = None
        for attempt in range(2):
            if attempt:
                self.log("ChatGPT API path hit Cloudflare; solving once and retrying homepage")
                self._bind_clearance(force=True, required=True)
            response = self._get(
                CHATGPT_APP,
                allow_redirects=True,
                headers=self._nav_headers(force_clearance=attempt > 0),
            )
            code = int(getattr(response, "status_code", 0) or 0)
            body = str(getattr(response, "text", "") or "")[:1200]
            try:
                from core.proxy_runtime import is_cloudflare_blocked

                blocked = is_cloudflare_blocked(code, body)
            except Exception:
                blocked = code >= 400 or "just a moment" in body.lower()
            if not blocked:
                self._sync_device_id_from_cookie()
                return
            last_err = _response_error(response)
        if response is None:
            raise RuntimeError("ChatGPT homepage bootstrap failed: no response")
        raise RuntimeError(f"CF_CHALLENGE: ChatGPT homepage remained blocked: {last_err}")

    def _nextauth_signin(self, email: str, *, attempts: int) -> tuple[object | None, str, str]:
        signin_response = None
        location = ""
        last_error = ""
        for signin_attempt in range(1, max(int(attempts), 1) + 1):
            csrf_response = self._get(
                f"{CHATGPT_APP}/api/auth/csrf",
                headers=api_headers(
                    self.profile,
                    origin=CHATGPT_APP,
                    referer=f"{CHATGPT_APP}/",
                ),
            )
            csrf_payload = _response_json(csrf_response)
            csrf_token = str(csrf_payload.get("csrfToken") or "").strip()
            if getattr(csrf_response, "status_code", 0) != 200 or not csrf_token:
                last_error = f"CSRF failed: {_response_error(csrf_response, csrf_payload)}"
            else:
                query = urlencode(
                    {
                        "prompt": "login",
                        "ext-oai-did": self.device_id,
                        "auth_session_logging_id": str(uuid.uuid4()),
                        "screen_hint": "login_or_signup",
                        "login_hint": email,
                    }
                )
                signin_response = self._post(
                    f"{CHATGPT_APP}/api/auth/signin/openai?{query}",
                    data=urlencode(
                        {
                            "callbackUrl": f"{CHATGPT_APP}/",
                            "csrfToken": csrf_token,
                            "json": "true",
                        }
                    ),
                    headers=api_headers(
                        self.profile,
                        origin=CHATGPT_APP,
                        referer=f"{CHATGPT_APP}/",
                        content_type="application/x-www-form-urlencoded",
                    ),
                    allow_redirects=False,
                )
                signin_payload = _response_json(signin_response)
                location = str(
                    signin_payload.get("url")
                    or signin_response.headers.get("location")
                    or ""
                ).strip()
                if getattr(signin_response, "status_code", 0) < 400 and location:
                    return signin_response, location, ""
                content_type = str(
                    getattr(signin_response, "headers", {}).get("content-type") or ""
                ).split(";", 1)[0]
                last_error = (
                    f"status={getattr(signin_response, 'status_code', 0)} "
                    f"content_type={content_type or '-'} "
                    f"detail={_response_error(signin_response, signin_payload)}"
                )
            if signin_attempt < attempts:
                time.sleep(0.5 * signin_attempt)
        return signin_response, location, last_error

    def _initialize_signup(self, email: str) -> dict:
        self.log(
            f"Initialize ChatGPT protocol session API-first profile={self.profile_key} "
            f"impersonate={self.profile.get('impersonate')}"
        )
        self._sync_device_id_from_cookie()
        signin_response, location, last_error = self._nextauth_signin(email, attempts=2)
        if not location:
            self.log("NextAuth API-first path unavailable; bootstrapping ChatGPT homepage once")
            self._bootstrap_chatgpt_homepage()
            signin_response, location, last_error = self._nextauth_signin(email, attempts=3)
        if signin_response is None or not location:
            raise RuntimeError(f"OpenAI registration authorization failed: {last_error or 'no response'}")
        final_url = self._follow_authorize_chain(location)
        return _auth_flow_state(current_url=final_url)

    def _submit_email(self, email: str) -> dict:
        """Submit the mailbox when the redirect chain stops at the login page."""

        cookie_flags = []
        for cookie_name in ("login_session", "oai-client-auth-session", "oai-did"):
            try:
                present = bool(str(self.session.cookies.get(cookie_name) or "").strip())
            except Exception:
                present = False
            cookie_flags.append(f"{cookie_name}={'yes' if present else 'no'}")
        self.log("OpenAI 授权 Cookie: " + ", ".join(cookie_flags))
        headers = self._common_headers(f"{OPENAI_AUTH}/create-account")
        headers.update(self.sentinel.build_headers(self.device_id, "authorize_continue"))
        response = self._post(
            OPENAI_API_ENDPOINTS["signup"],
            json={
                "username": {"value": email, "kind": "email"},
                "screen_hint": "signup",
            },
            headers=headers,
        )
        payload = _response_json(response)
        if getattr(response, "status_code", 0) >= 400 or payload.get("error"):
            raise RuntimeError(f"提交 ChatGPT 注册邮箱失败: {_response_error(response, payload)}")
        return _auth_flow_state(
            payload,
            str(getattr(response, "url", "") or f"{OPENAI_AUTH}/create-account"),
        )

    def _resolve_signup_state(self, email: str) -> dict:
        accepted = {
            "create_account_password",
            "password",
            "email_otp_send",
            "email_otp_verification",
        }
        auth_state = self._initialize_signup(email)
        if str(auth_state.get("page_type") or "") in accepted:
            return auth_state
        try:
            return self._submit_email(email)
        except RuntimeError as exc:
            if "invalid_state" not in str(exc).lower():
                raise
            self.log("OpenAI 登录会话已失效，重新初始化授权后重试一次")
        auth_state = self._initialize_signup(email)
        if str(auth_state.get("page_type") or "") in accepted:
            return auth_state
        return self._submit_email(email)

    def _validate_otp(self, code: str) -> dict:
        response = self._post(
            OPENAI_API_ENDPOINTS["validate_otp"],
            json={"code": code},
            headers=self._common_headers(f"{OPENAI_AUTH}/email-verification"),
        )
        payload = _response_json(response)
        if getattr(response, "status_code", 0) >= 400 or payload.get("error"):
            error_text = _response_error(response, payload)
            if "invalid_auth_step" in error_text.lower():
                return {"_invalid_auth_step": True, "_error": error_text}
            raise RuntimeError(f"邮箱验证码校验失败: {error_text}")
        return payload

    def _send_otp(self, *, referer: str) -> None:
        response = self._get(
            OPENAI_API_ENDPOINTS["send_otp"],
            side_effect=True,
            headers=self._common_headers(referer),
        )
        payload = _response_json(response)
        if getattr(response, "status_code", 0) >= 400 or payload.get("error"):
            raise RuntimeError(f"触发邮箱验证码失败: {_response_error(response, payload)}")
        self.log(
            "OpenAI send_otp 请求完成 "
            f"status={int(getattr(response, 'status_code', 0) or 0)}"
        )

    def _resend_otp(self) -> None:
        self._send_otp(referer=f"{OPENAI_AUTH}/email-verification")

    def _register_password(self, email: str, password: str) -> dict:
        headers = self._common_headers(f"{OPENAI_AUTH}/create-account/password")
        headers.update(self.sentinel.build_headers(
            self.device_id,
            "username_password_create",
        ))
        response = self._post(
            OPENAI_API_ENDPOINTS["register"],
            json={"password": password, "username": email},
            headers=headers,
        )
        payload = _response_json(response)
        if getattr(response, "status_code", 0) >= 400 or payload.get("error"):
            raise RuntimeError(f"设置 ChatGPT 密码失败: {_response_error(response, payload)}")
        return payload

    def _create_account(self, name: str, birthdate: str) -> dict:
        last_error = ""
        for attempt in range(3):
            self._check_cancelled()
            # Generate a fresh Sentinel proof for each retry.  Reusing a
            # rejected proof makes registration_disallowed retries ineffective.
            headers = self._common_headers(f"{OPENAI_AUTH}/about-you")
            headers.update(
                self.sentinel.build_headers(self.device_id, "oauth_create_account")
            )
            response = self._post(
                OPENAI_API_ENDPOINTS["create_account"],
                json={"name": name, "birthdate": birthdate},
                headers=headers,
            )
            payload = _response_json(response)
            if getattr(response, "status_code", 0) < 400 and not payload.get("error"):
                return payload
            last_error = _response_error(response, payload)
            if "registration_disallowed" not in last_error or attempt >= 2:
                break
            self.log(f"创建账号被临时拒绝，正在重试 ({attempt + 1}/3)...")
            time.sleep(2)
        raise RuntimeError(f"创建 ChatGPT 账号失败: {last_error}")

    def _session_result(self, email: str, password: str) -> dict:
        response = self._get(
            f"{CHATGPT_APP}/api/auth/session",
            headers=api_headers(
                self.profile,
                origin=CHATGPT_APP,
                referer=f"{CHATGPT_APP}/",
            ),
        )
        payload = _response_json(response)
        access_token = str(payload.get("accessToken") or "").strip()
        if getattr(response, "status_code", 0) != 200 or not access_token:
            raise RuntimeError(f"注册完成但获取 ChatGPT session 失败: {_response_error(response, payload)}")
        account = payload.get("account") if isinstance(payload.get("account"), dict) else {}
        claims = _decode_jwt_payload(access_token)
        auth_claims = claims.get("https://api.openai.com/auth")
        if not isinstance(auth_claims, dict):
            auth_claims = {}
        account_id = str(
            auth_claims.get("chatgpt_account_id")
            or account.get("id")
            or ""
        )
        workspace_id = str(auth_claims.get("organization_id") or account_id)
        try:
            cookies = self.session.cookies.get_dict()
        except Exception:
            cookies = {}
        return {
            "email": email,
            "password": password,
            "account_id": account_id,
            "workspace_id": workspace_id,
            "access_token": access_token,
            "session_token": str(payload.get("sessionToken") or ""),
            "refresh_token": "",
            "id_token": "",
            "cookies": cookies,
            "profile": account,
            "expires_at": payload.get("expires") or "",
            "browser_profile": self.profile_key,
        }

    def _try_session_result(self, email: str, password: str) -> dict | None:
        try:
            return self._session_result(email, password)
        except Exception:
            return None

    def _oauth_headers(self, referer: str) -> dict:
        return navigate_headers(self.profile, referer=referer)

    def _acquire_oauth_credentials(self, result: dict) -> dict:
        """Run Codex PKCE after Web Session creation without mode fallback.

        A valid Web Session remains a recoverable credential state if the
        independent OAuth phase is unavailable.  Cancellation is never hidden.
        """
        enriched = dict(result)
        if not self.oauth_pkce_enabled:
            enriched["oauth_status"] = "disabled"
            return enriched
        self._check_cancelled()
        try:
            client = self.oauth_client_factory(
                self.transport,
                auth_url=f"{OPENAI_AUTH}/oauth/authorize",
                token_url=OAUTH_TOKEN_URL,
                client_id=CODEX_CLIENT_ID,
                redirect_uri=CODEX_REDIRECT_URI,
                scope=CODEX_SCOPE,
                header_factory=self._oauth_headers,
            )
            tokens = client.run()
            self._check_cancelled()
            web_access_token = str(enriched.get("access_token") or "")
            enriched.update(tokens.as_dict())
            enriched["session_access_token"] = web_access_token
            enriched["oauth_status"] = "complete"
            enriched["oauth_error_code"] = ""
            self.log("OAuth PKCE 凭证阶段完成")
        except Exception as exc:
            if self.cancel_check():
                raise
            error_code = str(
                getattr(exc, "error_code", "")
                or classify_registration_error(exc).value
            )
            enriched["oauth_status"] = "recoverable"
            enriched["oauth_error_code"] = error_code
            self.log(f"OAuth PKCE 凭证阶段待恢复 error_code={error_code}")
        return enriched

    def _complete_session_stage(self, email: str, password: str, *, result: dict | None = None) -> dict:
        resolved = result or SessionResolver(
            lambda: self._session_result(email, password),
            cancel_check=self.cancel_check,
            attempts=3,
            delay_seconds=1.0,
        ).run()
        return self._acquire_oauth_credentials(resolved)

    def run(self, *, email: str, password: str) -> dict:
        if not str(email or "").strip():
            raise RuntimeError("协议注册缺少邮箱")
        if not callable(self.otp_callback):
            raise RuntimeError("协议注册缺少验证码回调")
        self._check_cancelled()
        self.state_machine.transition(
            RegistrationStage.PREPARE,
            f"开始 ChatGPT 协议注册 profile={self.profile_key}",
            action="start",
        )
        try:
            self.state_machine.transition(RegistrationStage.PREFLIGHT, "准备协议会话与挑战运行时")
            self.sentinel.warmup()
            self.state_machine.transition(RegistrationStage.AUTH_BEGIN, "初始化 OpenAI 授权会话")
            auth_state = self._resolve_signup_state(email)
            page_type = str(auth_state.get("page_type") or "")
            self.state_machine.transition(
                RegistrationStage.EMAIL_SUBMIT,
                "邮箱已提交",
                side_effect_committed=True,
            )

            password_registered = False
            otp_triggered = page_type == "email_otp_verification"
            if page_type == "email_otp_send":
                self._send_otp(referer=f"{OPENAI_AUTH}/create-account")
                otp_triggered = True
            if page_type in {"create_account_password", "password"}:
                password_result = self._register_password(email, password)
                password_registered = True
                self.log("ChatGPT 登录密码设置成功")
                password_state = _auth_flow_state(
                    password_result,
                    str(password_result.get("continue_url") or ""),
                )
                password_page_type = str(password_state.get("page_type") or "")
                if password_page_type not in {
                    "email_otp_send",
                    "email_otp_verification",
                }:
                    raise RuntimeError(
                        "AUTH_INVALID_STEP: 密码注册后未进入邮箱验证码阶段 "
                        f"page={password_page_type or '-'}"
                    )
                if password_page_type == "email_otp_send":
                    self._send_otp(referer=f"{OPENAI_AUTH}/create-account/password")
                otp_triggered = True

            if not otp_triggered:
                raise RuntimeError(
                    "AUTH_INVALID_STEP: 注册邮箱提交后未进入验证码阶段 "
                    f"page={page_type or '-'}"
                )
            self.state_machine.transition(
                RegistrationStage.OTP_TRIGGER,
                "验证码已触发",
                side_effect_committed=True,
            )
            self.state_machine.transition(RegistrationStage.OTP_WAIT, "等待邮箱验证码")
            otp_result = OtpCoordinator(
                receive=self.otp_callback,
                validate=self._validate_otp,
                resend=self._resend_otp,
                advance_cursor=getattr(self.otp_callback, "advance_cursor", None),
                cancel_check=self.cancel_check,
                log=self.log,
            ).run()
            validation = otp_result.validation
            self.state_machine.transition(
                RegistrationStage.OTP_SUBMIT,
                "邮箱验证码校验完成",
                side_effect_committed=True,
            )
            continue_url = str(validation.get("continue_url") or "").strip()
            if continue_url:
                self._get(
                    urljoin(OPENAI_AUTH, continue_url),
                    headers=navigate_headers(
                        self.profile,
                        referer=f"{OPENAI_AUTH}/email-verification",
                    ),
                    allow_redirects=True,
                )
            early_session = self._try_session_result(email, password)
            if early_session is not None:
                self.state_machine.recover_to_session("OTP 后已直接建立 Session")
                early_session = self._complete_session_stage(
                    email,
                    password,
                    result=early_session,
                )
                self.state_machine.transition(
                    RegistrationStage.DONE,
                    "ChatGPT 协议注册完成",
                    action="complete",
                )
                return early_session
            if validation.get("_invalid_auth_step"):
                raise RuntimeError(
                    "AUTH_INVALID_STEP: " + str(validation.get("_error") or "session unavailable")
                )
            if "password" in continue_url.lower() and not password_registered:
                password_result = self._register_password(email, password)
                self.log("ChatGPT 登录密码设置成功")
                password_continue_url = str(password_result.get("continue_url") or "").strip()
                if password_continue_url:
                    self._get(
                        urljoin(OPENAI_AUTH, password_continue_url),
                        headers=navigate_headers(
                            self.profile,
                            referer=f"{OPENAI_AUTH}/create-account/password",
                        ),
                        allow_redirects=True,
                    )
            self.state_machine.transition(RegistrationStage.PROFILE_CREATE, "创建账号资料")
            name, birthdate = _random_profile()
            created = self._create_account(name, birthdate)
            callback_url = str(created.get("continue_url") or "").strip()
            self.state_machine.transition(
                RegistrationStage.CALLBACK,
                "完成授权回调",
                continue_url=callback_url,
                side_effect_committed=True,
            )
            if callback_url:
                self._get(
                    urljoin(OPENAI_AUTH, callback_url),
                    headers=navigate_headers(self.profile, referer=f"{OPENAI_AUTH}/about-you"),
                    allow_redirects=True,
                )
            self.state_machine.transition(RegistrationStage.SESSION_VALIDATE, "验证 ChatGPT Session")
            result = self._complete_session_stage(email, password)
            self.state_machine.transition(
                RegistrationStage.DONE,
                "ChatGPT 协议注册完成并已获取 Session",
                action="complete",
            )
            return result
        finally:
            try:
                self.sentinel.close()
            except Exception as exc:
                detail = redact_registration_text(exc)[:160]
                self.log(
                    "Sentinel runtime cleanup failed "
                    f"type={type(exc).__name__} detail={detail or '-'}"
                )
            try:
                self.session.close()
            except Exception:
                pass
            try:
                from core.proxy_runtime import release_clearance_aliases

                release_clearance_aliases(
                    proxy_lease_id=self.transport_identity.proxy_lease_id,
                    fingerprint_id=self.transport_identity.fingerprint_id,
                )
            except Exception:
                pass

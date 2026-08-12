"""Camoufox-only browser runtime and failure evidence collection."""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform as host_platform
import re
from typing import Any, Iterable
from urllib.parse import urlsplit, urlunsplit

try:
    from camoufox import DefaultAddons as _DefaultAddons
    from camoufox.sync_api import Camoufox as _Camoufox
except Exception:  # Import errors are reported when the engine is opened.
    _DefaultAddons = None
    _Camoufox = None

from domain.registration_runtime import redact_registration_text


CAMOUFOX_WINDOW_SIZES: tuple[tuple[int, int], ...] = (
    (1366, 768),
    (1440, 900),
    (1536, 864),
    (1600, 900),
    (1920, 1080),
)

_EMAIL_RE = re.compile(r"(?i)\b[a-z0-9.!#$%&'*+/=?^_`{|}~-]+@[a-z0-9.-]+\.[a-z]{2,}\b")
_JWT_RE = re.compile(r"\beyJ[a-zA-Z0-9_-]+\.[a-zA-Z0-9_-]+(?:\.[a-zA-Z0-9_-]+)?\b")
_VALUE_ATTR_RE = re.compile(r"(?i)(\bvalue\s*=\s*)([\"']).*?\2", re.DOTALL)
_SENSITIVE_JSON_RE = re.compile(
    r'(?i)([\"\']?(?:password|otp|code|token|cookie|authorization|pkce_verifier)[\"\']?\s*[:=]\s*)([\"\']?)([^\s,;<>{}\"\']+)\2'
)


def build_camoufox_proxy(proxy: str | None) -> dict[str, str] | None:
    """Convert a proxy URL to Playwright/Camoufox's structured format."""
    value = str(proxy or "").strip()
    if not value:
        return None
    parsed = urlsplit(value)
    if not parsed.scheme or not parsed.hostname or not parsed.port:
        return {"server": value}
    config = {"server": f"{parsed.scheme}://{parsed.hostname}:{parsed.port}"}
    if parsed.username:
        config["username"] = parsed.username
    if parsed.password:
        config["password"] = parsed.password
    return config


def _stable_window(profile: dict[str, Any], attempt_id: str) -> tuple[int, int]:
    viewport = profile.get("viewport") if isinstance(profile, dict) else None
    if isinstance(viewport, dict):
        width = int(viewport.get("width") or 0)
        height = int(viewport.get("height") or 0)
        if width >= 1024 and height >= 700:
            return width, height
    digest = hashlib.sha256(str(attempt_id or "camoufox").encode("utf-8")).digest()
    return CAMOUFOX_WINDOW_SIZES[digest[0] % len(CAMOUFOX_WINDOW_SIZES)]


def _headless_setting(requested: bool, system_name: str) -> bool | str:
    if not requested:
        return False
    if system_name.lower() != "linux":
        return True
    mode = str(os.getenv("CAMOUFOX_LINUX_HEADLESS", "virtual") or "virtual").strip().lower()
    if mode in {"native", "true", "1"}:
        return True
    if mode in {"display", "x11", "false", "0"}:
        return False
    return "virtual"


def _bundled_executable_path() -> str:
    value = str(os.getenv("CAMOUFOX_EXECUTABLE_PATH", "") or "").strip()
    return value if value and Path(value).is_file() else ""


def _bundled_firefox_major(executable_path: str) -> int | None:
    version_path = Path(executable_path).parent / "version.json"
    try:
        version = str(json.loads(version_path.read_text(encoding="utf-8"))["version"])
        major = int(version.split(".", 1)[0])
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None
    return major if major > 0 else None


def _configure_bundled_geoip() -> None:
    value = str(os.getenv("CAMOUFOX_GEOIP_DIR", "") or "").strip()
    root = Path(value) if value else None
    if root is None or not root.is_dir():
        return
    try:
        from camoufox import geolocation

        geolocation.GEOIP_DIR = root
        geolocation.MMDB_DIR = root / "mmdb"
        geolocation.GEOIP_CONFIG = root / "config.yml"
    except Exception:
        return


def _redact_evidence(value: Any, secrets: Iterable[str] = ()) -> str:
    text = str(value or "")
    for secret in sorted(
        {str(item) for item in secrets if item and len(str(item)) >= 3},
        key=len,
        reverse=True,
    ):
        text = text.replace(secret, "***")
    text = _VALUE_ATTR_RE.sub(r"\1\2***\2", text)
    text = _SENSITIVE_JSON_RE.sub(r"\1\2***\2", text)
    text = _JWT_RE.sub("[TOKEN]", text)
    text = _EMAIL_RE.sub("[EMAIL]", text)
    return redact_registration_text(text)


def _redact_url(url: str, secrets: Iterable[str] = ()) -> str:
    try:
        parsed = urlsplit(str(url or ""))
        host = parsed.hostname or ""
        if parsed.port:
            host = f"{host}:{parsed.port}"
        clean = urlunsplit((parsed.scheme, host, parsed.path, "", ""))
    except Exception:
        clean = str(url or "").split("?", 1)[0].split("#", 1)[0]
    return _redact_evidence(clean, secrets)


@dataclass(frozen=True, slots=True)
class BrowserFailureArtifacts:
    directory: str
    screenshot: str = ""
    dom: str = ""
    diagnostic: str = ""


class BrowserFailureArtifactCollector:
    """Collect a small, redacted evidence bundle for one failed attempt."""

    def __init__(self, attempt_id: str, *, root: str | Path | None = None) -> None:
        safe_attempt = re.sub(r"[^a-zA-Z0-9_.-]+", "-", str(attempt_id or "manual"))[:96]
        default_root = Path(__file__).resolve().parents[2] / "data" / "registration-artifacts"
        self.root = Path(root or os.getenv("REGISTRATION_ARTIFACT_DIR") or default_root)
        self.attempt_id = safe_attempt or "manual"
        self.console_messages: deque[str] = deque(maxlen=100)

    def observe(self, page) -> None:
        def _on_console(message) -> None:
            try:
                value = message.text
                if callable(value):
                    value = value()
            except Exception:
                value = str(message)
            self.console_messages.append(str(value or ""))

        try:
            page.on("console", _on_console)
        except Exception:
            return

    @staticmethod
    def _mask_page(page, secrets: list[str]) -> bool:
        try:
            page.evaluate(
                """
                (secrets) => {
                  const style = document.createElement('style');
                  style.setAttribute('data-registration-redaction', '1');
                  style.textContent = `
                    input, textarea, [contenteditable="true"],
                    [data-registration-sensitive="1"] {
                      filter: blur(12px) !important;
                      color: transparent !important;
                      text-shadow: none !important;
                    }
                  `;
                  document.documentElement.appendChild(style);
                  const needles = (secrets || []).filter((item) => String(item || '').length >= 3);
                  for (const node of document.querySelectorAll('body *')) {
                    const ownText = String(node.childElementCount ? '' : (node.textContent || ''));
                    if (needles.some((needle) => ownText.includes(String(needle)))) {
                      node.setAttribute('data-registration-sensitive', '1');
                    }
                  }
                  return true;
                }
                """,
                secrets,
            )
            return True
        except Exception:
            return False

    def capture(
        self,
        page,
        error: BaseException,
        *,
        secrets: Iterable[str] = (),
        label: str = "failure",
    ) -> BrowserFailureArtifacts:
        known_secrets = [str(item) for item in secrets if item]
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
        safe_label = re.sub(r"[^a-zA-Z0-9_.-]+", "-", str(label or "failure"))[:48]
        target = self.root / self.attempt_id / f"{timestamp}-{safe_label}"
        target.mkdir(parents=True, exist_ok=False)

        screenshot_path = target / "screenshot.png"
        screenshot = ""
        if self._mask_page(page, known_secrets):
            try:
                page.screenshot(path=str(screenshot_path), full_page=True)
                screenshot = str(screenshot_path.resolve())
            except Exception:
                pass

        dom_path = target / "dom.html"
        dom = ""
        try:
            content = _redact_evidence(page.content(), known_secrets)
            dom_path.write_text(content, encoding="utf-8")
            dom = str(dom_path.resolve())
        except Exception:
            pass

        try:
            current_url = page.url
        except Exception:
            current_url = ""
        diagnostic = {
            "schema_version": 1,
            "attempt_id": self.attempt_id,
            "captured_at": datetime.now(timezone.utc).isoformat(),
            "url": _redact_url(current_url, known_secrets),
            "error_type": type(error).__name__,
            "error": _redact_evidence(error, known_secrets)[:2000],
            "console": [
                _redact_evidence(message, known_secrets)[:1000]
                for message in self.console_messages
            ],
            "files": {
                "screenshot": screenshot_path.name if screenshot else "",
                "dom": dom_path.name if dom else "",
            },
        }
        diagnostic_path = target / "diagnostic.json"
        diagnostic_path.write_text(
            json.dumps(diagnostic, ensure_ascii=True, indent=2),
            encoding="utf-8",
        )
        return BrowserFailureArtifacts(
            directory=str(target.resolve()),
            screenshot=screenshot,
            dom=dom,
            diagnostic=str(diagnostic_path.resolve()),
        )


class CamoufoxEngine:
    """Build and open one isolated Camoufox instance for an attempt."""

    def __init__(
        self,
        *,
        headless: bool,
        proxy: str | None = None,
        profile: dict[str, Any] | None = None,
        attempt_id: str = "",
        artifact_root: str | Path | None = None,
        camoufox_class: Any = _Camoufox,
        system_name: str | None = None,
    ) -> None:
        self.headless = bool(headless)
        self.proxy = str(proxy or "").strip() or None
        self.profile = dict(profile or {})
        self.attempt_id = str(attempt_id or "manual")
        self._camoufox_class = camoufox_class
        self._system_name = str(system_name or host_platform.system())
        self.artifacts = BrowserFailureArtifactCollector(
            self.attempt_id,
            root=artifact_root,
        )

    def build_launch_options(self) -> dict[str, Any]:
        options: dict[str, Any] = {
            "headless": _headless_setting(self.headless, self._system_name),
            "window": _stable_window(self.profile, self.attempt_id),
            "locale": self.profile.get("locale") or "en-US",
            "os": self.profile.get("os") or "windows",
            "humanize": True,
            "block_webrtc": bool(self.proxy),
        }
        proxy = build_camoufox_proxy(self.proxy)
        if proxy:
            options["proxy"] = proxy
            options["geoip"] = True
        executable_path = _bundled_executable_path()
        if executable_path:
            options["executable_path"] = executable_path
            firefox_major = _bundled_firefox_major(executable_path)
            if firefox_major:
                options["ff_version"] = firefox_major
            addon_path = Path(str(os.getenv("CAMOUFOX_ADDON_PATH", "") or "").strip())
            if addon_path.is_dir() and (addon_path / "manifest.json").is_file():
                options["addons"] = [str(addon_path)]
            if _DefaultAddons is not None:
                options["exclude_addons"] = [_DefaultAddons.UBO]
            _configure_bundled_geoip()
        return options

    def open(self):
        if self._camoufox_class is None:
            raise RuntimeError("Camoufox is unavailable; install it and run python -m camoufox fetch")
        return self._camoufox_class(**self.build_launch_options())


__all__ = [
    "BrowserFailureArtifactCollector",
    "BrowserFailureArtifacts",
    "CAMOUFOX_WINDOW_SIZES",
    "CamoufoxEngine",
    "build_camoufox_proxy",
]

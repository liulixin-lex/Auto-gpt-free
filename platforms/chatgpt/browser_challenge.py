"""Deterministic challenge handling for the Camoufox registration flow."""
from __future__ import annotations

from dataclasses import dataclass
import re
import time
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

from domain.challenge_runtime import ChallengeClassification, ChallengeClassifier, ChallengeKind
from domain.registration_runtime import RegistrationErrorCode


@dataclass(frozen=True, slots=True)
class TurnstileParameters:
    sitekey: str
    action: str = ""
    cdata: str = ""
    pagedata: str = ""


_CAPTURE_SCRIPT = r"""
() => {
  if (window.__registrationTurnstileCaptureInstalled) return;
  window.__registrationTurnstileCaptureInstalled = true;
  const install = () => {
    if (!window.turnstile || typeof window.turnstile.render !== 'function') return false;
    if (window.turnstile.render.__registrationWrapped) return true;
    const original = window.turnstile.render.bind(window.turnstile);
    const wrapped = (container, options) => {
      const value = options || {};
      window.__registrationTurnstile = {
        sitekey: String(value.sitekey || ''),
        action: String(value.action || ''),
        cdata: String(value.cData || value.cdata || ''),
        pagedata: String(value.chlPageData || value.pagedata || ''),
        callback: typeof value.callback === 'function' ? value.callback : null,
      };
      return original(container, options);
    };
    wrapped.__registrationWrapped = true;
    window.turnstile.render = wrapped;
    return true;
  };
  if (install()) return;
  const timer = window.setInterval(() => {
    if (install()) window.clearInterval(timer);
  }, 10);
  window.setTimeout(() => window.clearInterval(timer), 30000);
}
"""


def _page_snapshot(page) -> tuple[str, str]:
    try:
        url = str(page.url or "")
    except Exception:
        url = ""
    try:
        body = str(page.content() or "")[:500_000]
    except Exception:
        body = ""
    return url, body


def classify_browser_page(page) -> ChallengeClassification:
    url, body = _page_snapshot(page)
    return ChallengeClassifier.classify(url=url, body=body)


def extract_turnstile_parameters(page) -> TurnstileParameters | None:
    try:
        value = page.evaluate(
            """
            () => {
              const captured = window.__registrationTurnstile || {};
              let widget = document.querySelector('.cf-turnstile, [data-sitekey]');
              let iframeKey = '';
              for (const frame of document.querySelectorAll('iframe[src]')) {
                try {
                  const url = new URL(frame.src, location.href);
                  if (!url.hostname.includes('cloudflare.com')) continue;
                  iframeKey = url.searchParams.get('sitekey') || url.searchParams.get('k') || '';
                  if (iframeKey) break;
                } catch (_) {}
              }
              return {
                sitekey: String(captured.sitekey || widget?.getAttribute('data-sitekey') || iframeKey || ''),
                action: String(captured.action || widget?.getAttribute('data-action') || ''),
                cdata: String(captured.cdata || widget?.getAttribute('data-cdata') || ''),
                pagedata: String(captured.pagedata || widget?.getAttribute('data-pagedata') || ''),
              };
            }
            """
        )
        if isinstance(value, dict) and str(value.get("sitekey") or "").strip():
            return TurnstileParameters(
                sitekey=str(value.get("sitekey") or "").strip(),
                action=str(value.get("action") or "").strip(),
                cdata=str(value.get("cdata") or "").strip(),
                pagedata=str(value.get("pagedata") or "").strip(),
            )
    except Exception:
        pass

    _, body = _page_snapshot(page)
    matches = (
        r"data-sitekey\s*=\s*['\"]([^'\"]+)['\"]",
        r"[?&](?:sitekey|k)=([^&'\"<>\s]+)",
        r"['\"]sitekey['\"]\s*:\s*['\"]([^'\"]+)['\"]",
    )
    sitekey = ""
    for pattern in matches:
        match = re.search(pattern, body, flags=re.I)
        if match:
            sitekey = match.group(1)
            break
    if not sitekey:
        for iframe_url in re.findall(r"<iframe[^>]+src=['\"]([^'\"]+)", body, flags=re.I):
            query = parse_qs(urlparse(iframe_url).query)
            sitekey = str((query.get("sitekey") or query.get("k") or [""])[0])
            if sitekey:
                break
    return TurnstileParameters(sitekey=sitekey) if sitekey else None


def extract_turnstile_sitekey(page) -> str:
    value = extract_turnstile_parameters(page)
    return value.sitekey if value else ""


def inject_turnstile_token(page, token: str) -> bool:
    if not str(token or "").strip():
        return False
    try:
        return bool(
            page.evaluate(
                """
                (token) => {
                  let fields = Array.from(document.querySelectorAll(
                    'input[name="cf-turnstile-response"], textarea[name="cf-turnstile-response"], input[name="g-recaptcha-response"], textarea[name="g-recaptcha-response"]'
                  ));
                  for (const name of ['cf-turnstile-response', 'g-recaptcha-response']) {
                    if (!fields.some((field) => field.name === name)) {
                      const field = document.createElement('textarea');
                      field.name = name;
                      field.hidden = true;
                      (document.querySelector('form') || document.body).appendChild(field);
                      fields.push(field);
                    }
                  }
                  for (const field of fields) {
                    const setter = Object.getOwnPropertyDescriptor(
                      Object.getPrototypeOf(field), 'value'
                    )?.set;
                    if (setter) setter.call(field, token);
                    else field.value = token;
                    field.dispatchEvent(new Event('input', { bubbles: true }));
                    field.dispatchEvent(new Event('change', { bubbles: true }));
                  }
                  for (const node of document.querySelectorAll('[data-callback]')) {
                    const name = String(node.getAttribute('data-callback') || '');
                    if (name && typeof window[name] === 'function') window[name](token);
                  }
                  if (typeof window.turnstileCallback === 'function') {
                    window.turnstileCallback(token);
                  }
                  const captured = window.__registrationTurnstile || {};
                  if (typeof captured.callback === 'function') captured.callback(token);
                  return true;
                }
                """,
                token,
            )
        )
    except Exception:
        return False


class BrowserChallengeGuard:
    """Classify a page and resolve at most one token per widget instance."""

    def __init__(
        self,
        *,
        solver: Callable[[str, str], str] | None,
        log: Callable[[str], None],
        managed_wait_seconds: float = 15.0,
        proxy: str | None = None,
        user_agent: str = "",
        attempt_id: str = "",
    ) -> None:
        self.solver = solver
        self.log = log
        self.managed_wait_seconds = max(float(managed_wait_seconds), 0.0)
        self.proxy = str(proxy or "").strip() or None
        self.user_agent = str(user_agent or "")
        self.attempt_id = str(attempt_id or "")
        self._last_http: ChallengeClassification | None = None
        self._solved: set[tuple[str, str]] = set()

    def observe(self, page) -> None:
        try:
            page.add_init_script(_CAPTURE_SCRIPT)
        except Exception:
            pass

        def _response(response) -> None:
            try:
                response_url = str(response.url or "")
                host = (urlparse(response_url).hostname or "").lower()
                if not (
                    host == "openai.com"
                    or host.endswith(".openai.com")
                    or host == "chatgpt.com"
                    or host.endswith(".chatgpt.com")
                    or host == "cloudflare.com"
                    or host.endswith(".cloudflare.com")
                ):
                    return
                headers = response.headers
                if callable(headers):
                    headers = headers()
                result = ChallengeClassifier.classify(
                    status_code=int(response.status),
                    url=response_url,
                    headers=headers or {},
                )
                if result.kind is not ChallengeKind.NONE:
                    self._last_http = result
            except Exception:
                return

        try:
            page.on("response", _response)
        except Exception:
            pass

    @staticmethod
    def _pause(page, seconds: float) -> None:
        try:
            page.wait_for_timeout(max(int(seconds * 1000), 1))
        except Exception:
            time.sleep(max(seconds, 0.0))

    def _current(self, page) -> ChallengeClassification:
        current = classify_browser_page(page)
        if current.kind is not ChallengeKind.NONE:
            return current
        if self._last_http and self._last_http.kind in {
            ChallengeKind.CLOUDFLARE_MANAGED,
            ChallengeKind.HTTP_RATE_LIMIT,
            ChallengeKind.HTTP_FORBIDDEN,
        }:
            return self._last_http
        return current

    def resolve(self, page) -> bool:
        result = self._current(page)
        explicit_turnstile: TurnstileParameters | None = None
        if result.kind is ChallengeKind.NONE:
            return False
        if result.kind is ChallengeKind.HTTP_RATE_LIMIT:
            raise RuntimeError("HTTP_RATE_LIMIT: browser response returned 429")
        if result.kind is ChallengeKind.HTTP_FORBIDDEN:
            raise RuntimeError("CF_CHALLENGE: browser response returned 403")

        if result.kind is ChallengeKind.CLOUDFLARE_MANAGED:
            self.log("检测到 Cloudflare Managed Challenge，等待浏览器自动完成")
            # Cloudflare's managed page can already contain an explicit
            # Turnstile widget.  The page text still says "Performing security
            # verification", so the transport classifier correctly reports
            # Managed first; once a real sitekey/rendered widget is present we
            # can hand it to the configured solver instead of waiting for a
            # checkbox that a headless browser cannot complete by itself.
            explicit_turnstile = extract_turnstile_parameters(page)
            if explicit_turnstile:
                result = ChallengeClassification(
                    ChallengeKind.TURNSTILE,
                    RegistrationErrorCode.CF_CHALLENGE,
                    True,
                    result.status_code,
                    "managed_page_with_explicit_turnstile",
                )
            else:
                deadline = time.monotonic() + self.managed_wait_seconds
                while time.monotonic() < deadline:
                    self._pause(page, min(1.0, max(deadline - time.monotonic(), 0.01)))
                    explicit_turnstile = extract_turnstile_parameters(page)
                    if explicit_turnstile:
                        result = ChallengeClassification(
                            ChallengeKind.TURNSTILE,
                            RegistrationErrorCode.CF_CHALLENGE,
                            True,
                            result.status_code,
                            "managed_page_with_explicit_turnstile",
                        )
                        break
                    current = classify_browser_page(page)
                    if current.kind is ChallengeKind.NONE:
                        self._last_http = None
                        return True
                    if current.kind is ChallengeKind.TURNSTILE:
                        result = current
                        break
                else:
                    raise RuntimeError("CF_CHALLENGE: managed challenge did not clear")

        if result.kind is ChallengeKind.TURNSTILE:
            parameters = explicit_turnstile or extract_turnstile_parameters(page)
            if not parameters:
                raise RuntimeError("CF_CHALLENGE: Turnstile sitekey missing")
            url, _ = _page_snapshot(page)
            challenge_key = (url.split("#", 1)[0], parameters.sitekey)
            if challenge_key in self._solved:
                raise RuntimeError("CF_CHALLENGE: Turnstile remained after token injection")
            if not self.solver:
                raise RuntimeError("CF_CHALLENGE: no Turnstile provider is configured")
            self.log("检测到 Turnstile，使用当前 attempt 的验证码 provider")
            try:
                token = self.solver(
                    url,
                    parameters.sitekey,
                    proxy_url=self.proxy,
                    user_agent=self.user_agent,
                    action=parameters.action,
                    cdata=parameters.cdata,
                    pagedata=parameters.pagedata,
                    attempt_id=self.attempt_id,
                )
            except TypeError as exc:
                if "unexpected keyword" not in str(exc):
                    raise
                token = self.solver(url, parameters.sitekey)
            token = str(token or "").strip()
            if not token:
                raise RuntimeError("CF_CHALLENGE: Turnstile provider returned an empty token")
            if not inject_turnstile_token(page, token):
                raise RuntimeError("CF_CHALLENGE: Turnstile token injection failed")
            self._solved.add(challenge_key)
            self._last_http = None
            self._pause(page, 1.5)
            return True

        return False


__all__ = [
    "BrowserChallengeGuard",
    "classify_browser_page",
    "extract_turnstile_parameters",
    "extract_turnstile_sitekey",
    "inject_turnstile_token",
    "TurnstileParameters",
]

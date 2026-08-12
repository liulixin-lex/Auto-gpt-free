"""ChatGPT 浏览器注册流程（Camoufox）。"""
import base64
import json
import random
import re
import secrets
import threading
import time
import uuid
from typing import Any, Callable, Optional
from urllib.parse import urljoin, urlparse

from domain.registration_runtime import RegistrationStage

from .browser_challenge import BrowserChallengeGuard
from .browser_engine import CamoufoxEngine
from .constants import (
    OPENAI_AUTH,
    CHATGPT_APP,
    PLATFORM_LOGIN_ENTRY,
    SENTINEL_SDK_URL,
    SENTINEL_REQ_URL,
    SENTINEL_FRAME_URL,
    SENTINEL_BASE,
    OAUTH_CONSENT_FORM_SELECTOR,
)


def _is_transient_nav_error(exc: BaseException) -> bool:
    """page.goto / page.reload 抛错是否属于可重试的瞬时网络断连。

    覆盖 Chromium/Firefox 常见的瞬时网络错误码。业务/页面错误（4xx、选择器
    超时等）不在此列，不会被误判重试。
    """
    msg = str(exc or "").lower()
    return any(
        token in msg
        for token in (
            "err_connection_closed",
            "err_connection_reset",
            "err_connection_refused",
            "err_connection_aborted",
            "err_connection_failed",
            "err_timed_out",
            "err_network_changed",
            "err_empty_response",
            "err_socks_connection_failed",
            "err_proxy_connection_failed",
            "err_tunnel_connection_failed",
            "err_name_not_resolved",
            "err_address_unreachable",
            "ns_error_net",            # Firefox/Camoufox 网络错误前缀
            "neterror",
            "navigating to",           # Playwright 包装的导航失败常带这句
            "execution context was destroyed",
            "most likely because of a navigation",
        )
    )


def _goto_with_retry(
    page,
    url: str,
    *,
    wait_until: str = "domcontentloaded",
    timeout: int = 30000,
    attempts: int = 3,
    log: Optional[Callable[[str], None]] = None,
):
    """``page.goto`` 带瞬时网络错误重试（默认 3 次，指数退避）。

    全局统一：注册流程里所有打开页面都该走这个，避免一次网络波动
    （ERR_CONNECTION_CLOSED / RESET / TIMED_OUT 等）就直接判失败。
    瞬时错误重试；业务错误（页面 4xx、选择器问题）原样抛出不重试。
    """
    _log = log or (lambda *_a, **_k: None)
    last_exc: Optional[BaseException] = None
    for attempt in range(1, max(int(attempts), 1) + 1):
        try:
            return page.goto(url, wait_until=wait_until, timeout=timeout)
        except Exception as exc:  # noqa: BLE001 - 按错误内容判定是否重试
            last_exc = exc
            if attempt >= attempts or not _is_transient_nav_error(exc):
                raise
            backoff = 1.5 * attempt
            _log(
                f"打开页面瞬时网络失败（第 {attempt}/{attempts} 次，{backoff:.1f}s 后重试）："
                f"{str(exc)[:120]}"
            )
            time.sleep(backoff)
    if last_exc is not None:
        raise last_exc


def _reload_with_retry(
    page,
    *,
    wait_until: str = "domcontentloaded",
    timeout: int = 30000,
    attempts: int = 3,
    log: Optional[Callable[[str], None]] = None,
):
    """``page.reload`` 带瞬时网络错误重试。"""
    _log = log or (lambda *_a, **_k: None)
    last_exc: Optional[BaseException] = None
    for attempt in range(1, max(int(attempts), 1) + 1):
        try:
            return page.reload(wait_until=wait_until, timeout=timeout)
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            if attempt >= attempts or not _is_transient_nav_error(exc):
                raise
            time.sleep(1.5 * attempt)
    if last_exc is not None:
        raise last_exc

EMAIL_INPUT_SELECTORS = [
    'input#login-email',
    'input[type="email"]',
    'input[name="email"]',
    'input[name="username"]',
    'input[autocomplete="username"]',
    'input[autocomplete*="username"]',
    'input[inputmode="email"]',
    'input[id*="email"]',
]

PASSWORD_INPUT_SELECTORS = [
    'input[type="password"]',
    'input[name="password"]',
    'input[autocomplete="new-password"]',
]

EMAIL_SUBMIT_SELECTORS = [
    'button[type="submit"]',
    'button[data-testid="continue-button"]',
    'button:has-text("Continue")',
    'button:has-text("continue")',
    'button:has-text("Next")',
    'button:has-text("next")',
    'button:has-text("続ける")',
    'button:has-text("続行")',
    'button:has-text("次へ")',
]

PASSWORD_SUBMIT_SELECTORS = [
    'button[type="submit"]',
    'button[data-testid="continue-button"]',
    'button:has-text("Continue")',
    'button:has-text("continue")',
    'button:has-text("Sign up")',
    'button:has-text("sign up")',
    'button:has-text("Create account")',
    'button:has-text("create account")',
    'button:has-text("続ける")',
    'button:has-text("続行")',
    'button:has-text("登録")',
    'button:has-text("新規登録")',
    'button:has-text("アカウントを作成")',
    'button:has-text("サインアップ")',
]

OTP_INPUT_SELECTORS = [
    "input[inputmode='numeric']",
    "input[autocomplete='one-time-code']",
    "input[type='tel']",
    "input[type='number']",
    "input[name*='code' i]",
    "input[id*='code' i]",
]

SIGNUP_RECOVERY_SELECTORS = [
    'a:has-text("Sign up")',
    'button:has-text("Sign up")',
    'a:has-text("sign up")',
    'button:has-text("sign up")',
    'a:has-text("Register")',
    'button:has-text("Register")',
    'a:has-text("Create account")',
    'button:has-text("Create account")',
    'a:has-text("创建账号")',
    'button:has-text("创建账号")',
    'a:has-text("注册")',
    'button:has-text("注册")',
    'a:has-text("登録")',
    'button:has-text("登録")',
    'a:has-text("新規登録")',
    'button:has-text("新規登録")',
    'a:has-text("アカウントを作成")',
    'button:has-text("アカウントを作成")',
    'a:has-text("サインアップ")',
    'button:has-text("サインアップ")',
]

PASSWORDLESS_LOGIN_SELECTORS = [
    'button[name="intent"][value="passwordless_login_send_otp"]',
    'button[value="passwordless_login_send_otp"]',
    'button:has-text("one-time code")',
    'button:has-text("one time code")',
    'button:has-text("passwordless")',
    'button:has-text("一次性验证码")',
    'button:has-text("驗證碼")',
    'button:has-text("验证码")',
    'button:has-text("código único")',
    'button:has-text("code unique")',
    'button:has-text("Einmalcode")',
    'button:has-text("código de uso único")',
    'button:has-text("ワンタイムコード")',
    'button:has-text("一回限りのコード")',
    'button:has-text("認証コード")',
]

# add-phone 页面国际拨号码 -> 国家名映射（用于 UI 下拉选择）
AUTH_TIMEOUT_TITLE_RE = re.compile(r"oops,\s*an\s*error\s*occurred|出错|發生錯誤|エラーが発生|問題が発生", re.I)
AUTH_TIMEOUT_DETAIL_RE = re.compile(
    r"operation\s+timed\s+out|route\s+error|405\s+method\s+not\s+allowed|failed\s+to\s+fetch|network\s+error|fetch\s+failed|タイムアウト|ネットワークエラー|取得に失敗",
    re.I,
)
AUTH_RETRY_TEXT_RE = re.compile(r"try\s+again|重试|重試|再試行|もう一度|やり直す", re.I)


def _is_auth_timeout_retry_text(text: str) -> bool:
    value = str(text or "")
    return bool(
        AUTH_RETRY_TEXT_RE.search(value)
        and (AUTH_TIMEOUT_TITLE_RE.search(value) or AUTH_TIMEOUT_DETAIL_RE.search(value))
    )


def _wait_for_url(page, substring: str, timeout: int = 60) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if substring in page.url:
            return True
        time.sleep(1)
    return False


def _find_first_selector(page, selectors: list[str]) -> str | None:
    for sel in selectors:
        try:
            node = page.query_selector(sel)
        except Exception:
            node = None
        if node:
            return sel
    return None


def _wait_for_any_selector(page, selectors: list[str], timeout: int = 30):
    deadline = time.time() + timeout
    while time.time() < deadline:
        found = _find_first_selector(page, selectors)
        if found:
            return found
        time.sleep(0.5)
    return None


def _click_first(page, selectors: list[str], *, timeout: int = 10) -> str | None:
    found = _wait_for_any_selector(page, selectors, timeout=timeout)
    if not found:
        return None
    try:
        page.click(found)
        return found
    except Exception:
        return None


def _click_first_no_wait(page, selectors: list[str], *, timeout: int = 10) -> str | None:
    """Click a visible element without waiting for navigation.

    OpenAI's add-phone page sometimes leaves the submit XHR pending long enough
    that Playwright reports "Operation timed out" even though the click was
    delivered. This helper treats that as a click problem only after a
    no-wait click and a DOM fallback both fail.
    """
    found = _wait_for_any_selector(page, selectors, timeout=timeout)
    if not found:
        return None
    for kwargs in (
        {"timeout": 3000, "no_wait_after": True},
        {"timeout": 3000, "force": True, "no_wait_after": True},
    ):
        try:
            page.click(found, **kwargs)
            return found
        except Exception:
            pass
    try:
        clicked = bool(
            page.evaluate(
                """
                (selector) => {
                  const visible = (el) => {
                    const style = window.getComputedStyle(el);
                    const rect = el.getBoundingClientRect();
                    return style && style.display !== 'none' && style.visibility !== 'hidden' && rect.width > 0 && rect.height > 0;
                  };
                  let target = null;
                  try {
                    target = document.querySelector(selector);
                  } catch (_) {
                    const textMatch = selector.match(/:has-text\\(["'](.+?)["']\\)/);
                    const tag = String(selector.split(':')[0] || 'button').trim() || 'button';
                    const needle = textMatch ? textMatch[1].toLowerCase() : '';
                    target = Array.from(document.querySelectorAll(tag)).find((el) => {
                      const text = String(el.innerText || el.textContent || '').trim().toLowerCase();
                      return visible(el) && (!needle || text.includes(needle));
                    });
                  }
                  if (!target || !visible(target) || target.disabled) return false;
                  target.click();
                  return true;
                }
                """,
                found,
            )
        )
        return found if clicked else None
    except Exception:
        return None


def _auth_timeout_retry_page_state(page, *, path_patterns: list[str] | None = None) -> dict:
    try:
        result = page.evaluate(
            """
            (pathPatterns) => {
              const visible = (el) => {
                if (!el) return false;
                const style = window.getComputedStyle(el);
                const rect = el.getBoundingClientRect();
                return style && style.display !== 'none' && style.visibility !== 'hidden' && rect.width > 0 && rect.height > 0;
              };
              const pathname = String(location.pathname || '');
              if (Array.isArray(pathPatterns) && pathPatterns.length) {
                const matched = pathPatterns.some((raw) => {
                  try { return new RegExp(raw, 'i').test(pathname); } catch (_) { return false; }
                });
                if (!matched) return { retryPage: false, url: location.href, text: '' };
              }
              const text = String(document.body?.innerText || '').replace(/\\s+/g, ' ').trim();
              const buttons = Array.from(document.querySelectorAll('button, [role="button"], input[type="button"], input[type="submit"]'));
              const retryButton = document.querySelector('button[data-dd-action-name="Try again"]')
                || buttons.find((button) => {
                  const label = String([button.value, button.textContent, button.getAttribute?.('aria-label'), button.getAttribute?.('title')].filter(Boolean).join(' '));
                  return visible(button) && /try\\s+again|重试|重試|再試行|もう一度|やり直す/i.test(label);
                });
              return {
                retryPage: Boolean(retryButton && /try\\s+again|重试|重試/i.test(text) && (/oops,?\\s*an\\s*error\\s*occurred|operation\\s+timed\\s+out|route\\s+error|405\\s+method\\s+not\\s+allowed|failed\\s+to\\s+fetch|network\\s+error/i.test(text))),
                retryEnabled: Boolean(retryButton && visible(retryButton) && !retryButton.disabled && retryButton.getAttribute('aria-disabled') !== 'true'),
                url: location.href,
                text,
              };
            }
            """,
            path_patterns or [],
        )
        if isinstance(result, dict):
            result["retryPage"] = bool(result.get("retryPage") or _is_auth_timeout_retry_text(str(result.get("text") or "")))
            return result
    except Exception:
        pass
    return {"retryPage": False, "retryEnabled": False, "url": str(page.url or ""), "text": ""}


def _recover_auth_timeout_retry_page(
    page,
    log,
    *,
    path_patterns: list[str] | None = None,
    max_clicks: int = 3,
    wait_after_click: float = 3.0,
) -> dict:
    last_state = {}
    for attempt in range(1, max_clicks + 1):
        state = _auth_timeout_retry_page_state(page, path_patterns=path_patterns)
        last_state = state
        if not state.get("retryPage"):
            return {"recovered": attempt > 1, "clicks": attempt - 1, "url": str(state.get("url") or page.url)}
        if not state.get("retryEnabled"):
            time.sleep(0.5)
            continue
        log(f"  检测到 OpenAI auth 超时重试页，点击 Try again ({attempt}/{max_clicks})")
        clicked = _click_first_no_wait(
            page,
            [
                'button[data-dd-action-name="Try again"]',
                'button:has-text("Try again")',
                'button:has-text("try again")',
                'button:has-text("重试")',
                'button:has-text("重試")',
                'button:has-text("再試行")',
                'button:has-text("もう一度")',
                'button:has-text("やり直す")',
            ],
            timeout=2,
        )
        if not clicked:
            try:
                clicked = "dom" if page.evaluate(
                    """
                    () => {
                      const visible = (el) => {
                        const style = window.getComputedStyle(el);
                        const rect = el.getBoundingClientRect();
                        return style && style.display !== 'none' && style.visibility !== 'hidden' && rect.width > 0 && rect.height > 0;
                      };
                      const direct = document.querySelector('button[data-dd-action-name="Try again"]');
                      const target = direct || Array.from(document.querySelectorAll('button, [role="button"]')).find((el) => {
                        const text = String(el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim();
                        return visible(el) && /try\\s+again|重试|重試/i.test(text);
                      });
                      if (!target) return false;
                      target.click();
                      return true;
                    }
                    """
                ) else ""
            except Exception:
                clicked = ""
        if not clicked:
            break
        time.sleep(wait_after_click)
        state = _auth_timeout_retry_page_state(page, path_patterns=path_patterns)
        last_state = state
        if not state.get("retryPage"):
            return {"recovered": True, "clicks": attempt, "url": str(state.get("url") or page.url)}
    return {
        "recovered": False,
        "clicks": max_clicks,
        "url": str(last_state.get("url") or page.url),
        "text": str(last_state.get("text") or "")[:300],
    }


def _is_login_password_url(url: str) -> bool:
    return bool(re.search(r"(?:auth|accounts)\.openai\.com/.*log-?in/password", str(url or ""), flags=re.I))


def _build_manual_flow_state(page_type: str, current_url: str) -> dict:
    state = _extract_flow_state(None, current_url)
    state["page_type"] = page_type
    state["current_url"] = current_url
    return state


def _get_visible_page_text(page) -> str:
    try:
        return str(page.evaluate("() => document.body?.innerText || ''") or "")
    except Exception:
        return ""


def _has_signup_registration_choice(page) -> bool:
    if not _is_login_password_url(str(page.url or "")):
        return False
    if _find_first_selector(page, SIGNUP_RECOVERY_SELECTORS):
        return True
    text = _get_visible_page_text(page)
    return bool(re.search(r"sign\s*up|register|create\s*account|还没有帐户|还没有账户|請註冊|请注册|去注册|注册", text, flags=re.I))


def _click_passwordless_login_if_available(page, log, *, context: str) -> bool:
    selector = _click_first(page, PASSWORDLESS_LOGIN_SELECTORS, timeout=1)
    if selector:
        log(f"{context} 已选择一次性验证码登录: {selector}")
        time.sleep(1)
        return True
    try:
        clicked = bool(
            page.evaluate(
                """
                () => {
                  const nodes = Array.from(document.querySelectorAll('button, [role="button"], a'));
                  const visible = (el) => {
                    const style = window.getComputedStyle(el);
                    const rect = el.getBoundingClientRect();
                    return style && style.display !== 'none' && style.visibility !== 'hidden' && rect.width > 0 && rect.height > 0;
                  };
                  const target = nodes.find((el) => {
                    const text = String(el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim();
                    return visible(el) && /使用一次性验证码登录|使用一次性驗證碼登入|one-time code|one time code|passwordless|ワンタイムコード|一回限りのコード|認証コード/i.test(text);
                  });
                  if (!target) return false;
                  target.click();
                  return true;
                }
                """
            )
        )
    except Exception:
        clicked = False
    if clicked:
        log(f"{context} 已选择一次性验证码登录")
        time.sleep(1)
    return clicked


def _get_page_oauth_url(page) -> str:
    try:
        return str(
            page.evaluate(
                """
                () => {
                  const visible = (el) => {
                    const style = window.getComputedStyle(el);
                    const rect = el.getBoundingClientRect();
                    return style && style.display !== 'none' && style.visibility !== 'hidden' && rect.width > 0 && rect.height > 0;
                  };
                  const anchors = Array.from(document.querySelectorAll('a[href*="/api/oauth/authorize"], a[href*="/oauth/authorize"]'));
                  const anchor = anchors.find((el) => visible(el));
                  return anchor ? String(anchor.href || anchor.getAttribute('href') || '') : '';
                }
                """
            )
            or ""
        ).strip()
    except Exception:
        return ""


def _oauth_url_matches_state(url: str, state: str) -> bool:
    if not url or not state:
        return False
    return f"state={state}" in url or f"state%3D{state}" in url


def _extract_auth_error_text(page) -> str:
    selectors = [
        "text=Failed to create account",
        "text=Sorry, we cannot create your account",
        "text=Please try again",
        "text=Invalid code",
        "text=Enter a valid age to continue",
        "text=doesn't look right",
        "[role='alert']",
        ".error, [class*='error'], [class*='Error']",
    ]
    for selector in selectors:
        try:
            text = str(page.locator(selector).first.text_content(timeout=350) or "").strip()
        except Exception:
            text = ""
        if text and "oai_log" not in text and "SSR_HTML" not in text:
            return text
    return ""


def _classify_authentication_error_page(page) -> str:
    try:
        current_url = str(page.url or "").lower()
    except Exception:
        current_url = ""
    try:
        body = str(page.locator("body").inner_text(timeout=500) or "").lower()[:6000]
    except Exception:
        body = ""
    combined = f"{current_url}\n{body}"
    if "identity_provider_mismatch" in combined:
        return "AUTH_IDENTITY_PROVIDER_MISMATCH: identity_provider_mismatch"
    if "authentication error" in combined or "auth_error" in combined:
        return "AUTH_SESSION_DESYNC: authentication error page"
    return ""


def _fill_input_like_user(page, selector: str, value: str) -> bool:
    try:
        locator = page.locator(selector).first
        locator.wait_for(state="visible", timeout=2000)
        current = str(locator.input_value() or "").strip()
        if current == str(value).strip():
            return True
        locator.click(timeout=1500)
        _browser_pause(page)
        try:
            locator.fill("")
        except Exception:
            pass
        _browser_pause(page, headed=False)
        try:
            locator.type(value, delay=random.randint(35, 85))
        except Exception:
            try:
                page.fill(selector, value)
            except Exception:
                return False
        final_value = str(locator.input_value() or "").strip()
        if final_value == str(value):
            return True
    except Exception:
        pass

    try:
        ok = page.evaluate(
            """
            ({ selector, value }) => {
              const input = document.querySelector(selector);
              if (!input) return false;
              const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')?.set;
              if (!setter) return false;
              setter.call(input, value);
              input.dispatchEvent(new Event('input', { bubbles: true }));
              input.dispatchEvent(new Event('change', { bubbles: true }));
              return String(input.value || '') === String(value || '');
            }
            """,
            {"selector": selector, "value": value},
        )
        return bool(ok)
    except Exception:
        return False


def _submit_form_with_fallback(page, input_selector: str) -> bool:
    try:
        return bool(
            page.evaluate(
                """
                (selector) => {
                  const input = document.querySelector(selector);
                  if (!input) return false;
                  const form = input.form || input.closest?.('form');
                  if (form?.requestSubmit) {
                    form.requestSubmit();
                    return true;
                  }
                  if (form?.submit) {
                    form.submit();
                    return true;
                  }
                  input.focus?.();
                  for (const type of ['keydown', 'keypress', 'keyup']) {
                    input.dispatchEvent(new KeyboardEvent(type, {
                      key: 'Enter',
                      code: 'Enter',
                      bubbles: true,
                      cancelable: true,
                    }));
                  }
                  return true;
                }
                """,
                input_selector,
            )
        )
    except Exception:
        return False


def _sync_hidden_birthday_input(page, birthdate: str, log) -> bool:
    try:
        synced = bool(
            page.evaluate(
                """
                (value) => {
                  const input = document.querySelector("input[name='birthday']");
                  if (!input) return false;
                  input.value = value;
                  input.dispatchEvent(new Event('input', { bubbles: true }));
                  input.dispatchEvent(new Event('change', { bubbles: true }));
                  return String(input.value || '') === String(value || '');
                }
                """,
                birthdate,
            )
        )
    except Exception:
        synced = False
    if synced:
        log("about_you 已同步隐藏 birthday")
    return synced


def _react_aria_date_values_valid(
    *,
    month_value: str,
    day_value: str,
    year_value: str,
    hidden_value: str,
    expected_month: str,
    expected_day: str,
    expected_year: str,
) -> bool:
    """Validate React Aria DateField segments before submitting the form."""
    try:
        segments_match = (
            int(str(month_value or "0")) == int(expected_month)
            and int(str(day_value or "0")) == int(expected_day)
            and int(str(year_value or "0")) == int(expected_year)
        )
    except (TypeError, ValueError):
        return False
    if not segments_match:
        return False
    normalized_hidden = str(hidden_value or "").strip()
    if not normalized_hidden:
        return True
    expected_iso = f"{int(expected_year):04d}-{int(expected_month):02d}-{int(expected_day):02d}"
    return normalized_hidden == expected_iso


def _fill_react_aria_date_segments(page, month: str, day: str, year: str, log) -> bool:
    """Fill the contenteditable React Aria DateField used by about-you.

    The birthday control is not made of ``input`` elements.  Calling
    ``fill``/``input_value`` on its ``div[role=spinbutton]`` segments leaves
    the visible value empty (and emits Playwright "element is not an input"
    errors).  Use the segment keyboard contract first, then fall back to the
    hidden form value so a minor React Aria markup change does not abort the
    whole attempt before the server can validate the payload.
    """
    month_value = str(month or "").strip()
    day_value = str(day or "").strip()
    year_value = str(year or "").strip()
    if not (month_value and day_value and year_value):
        return False

    try:
        month_seg = page.locator('div[data-type="month"], input[data-type="month"]').first
        day_seg = page.locator('div[data-type="day"], input[data-type="day"]').first
        year_seg = page.locator('div[data-type="year"], input[data-type="year"]').first
        if page.locator('div[data-type="month"], input[data-type="month"]').count() < 1:
            return False
        if page.locator('div[data-type="day"], input[data-type="day"]').count() < 1:
            return False
        if page.locator('div[data-type="year"], input[data-type="year"]').count() < 1:
            return False
    except Exception:
        return False

    def _pump(milliseconds: int = 250) -> None:
        try:
            page.wait_for_timeout(milliseconds)
        except Exception:
            time.sleep(max(milliseconds, 0) / 1000)

    def _snapshot() -> tuple[str, str, str, str]:
        def attr(locator, name: str) -> str:
            try:
                return str(locator.get_attribute(name) or "")
            except Exception:
                return ""

        hidden = ""
        try:
            hidden = str(page.locator('input[name="birthday"]').first.input_value() or "")
        except Exception:
            try:
                hidden = str(
                    page.locator('input[name="birthday"]').first.get_attribute("value") or ""
                )
            except Exception:
                pass
        return (
            attr(month_seg, "aria-valuenow"),
            attr(day_seg, "aria-valuenow"),
            attr(year_seg, "aria-valuenow"),
            hidden,
        )

    def _valid(values: tuple[str, str, str, str]) -> bool:
        return _react_aria_date_values_valid(
            month_value=values[0],
            day_value=values[1],
            year_value=values[2],
            hidden_value=values[3],
            expected_month=month_value,
            expected_day=day_value,
            expected_year=year_value,
        )

    def _segments_match(values: tuple[str, str, str, str]) -> bool:
        try:
            return (
                int(values[0] or "0") == int(month_value)
                and int(values[1] or "0") == int(day_value)
                and int(values[2] or "0") == int(year_value)
            )
        except (TypeError, ValueError):
            return False

    def _type_segment(locator, value: str) -> None:
        locator.click(force=True)
        _pump(80)
        try:
            placeholder = str(locator.get_attribute("data-placeholder") or "").lower() == "true"
        except Exception:
            placeholder = False
        if not placeholder:
            try:
                locator.press("Control+A")
                locator.press("Backspace")
            except Exception:
                page.keyboard.press("Control+A")
                page.keyboard.press("Backspace")
        # React Aria segments consume numeric key events; direct DOM value
        # assignment is intentionally avoided because it bypasses DateField
        # state and produces truncated years such as "91".
        try:
            locator.press_sequentially(str(value), delay=180)
        except Exception:
            page.keyboard.type(str(value), delay=180)
        _pump(300)

    # Try each segment independently.  Do not press Tab: React Aria already
    # advances segments after a complete numeric value and Tab can skip the
    # day segment on current auth builds.
    for _attempt in range(3):
        try:
            _type_segment(month_seg, month_value)
            _type_segment(day_seg, day_value)
            _type_segment(year_seg, year_value)
            values = _snapshot()
            if _valid(values):
                return True
            # React Aria may update the hidden form input on the next render
            # tick even after all three visible segments are already valid.
            # Do not discard a correct segment state merely because the hidden
            # input has not caught up yet; the submit handler will validate the
            # final form payload and report a real 400 if it is still stale.
            if _segments_match(values):
                _pump(700)
                return True
        except Exception as exc:
            log(f"about_you 日期分段输入重试 type={type(exc).__name__}")

    # Last resort: synchronize the hidden form field using the native setter.
    # This keeps the browser session and CSRF/device context intact while
    # allowing the server-side form validator to make the final decision.
    try:
        hidden = page.locator('input[name="birthday"]').first
        iso = f"{int(year_value):04d}-{int(month_value):02d}-{int(day_value):02d}"
        applied = bool(
            hidden.evaluate(
                """
                (input, nextValue) => {
                  const setter = Object.getOwnPropertyDescriptor(
                    window.HTMLInputElement.prototype, 'value'
                  )?.set;
                  if (!setter) return false;
                  setter.call(input, nextValue);
                  input.dispatchEvent(new Event('input', { bubbles: true }));
                  input.dispatchEvent(new Event('change', { bubbles: true }));
                  return String(input.value || '') === String(nextValue || '');
                }
                """,
                iso,
            )
        )
        _pump(300)
        if applied:
            hidden_value = str(hidden.input_value() or "")
            if hidden_value == iso:
                log("about_you 已同步隐藏 Birthday 字段")
                return True
    except Exception as exc:
        log(f"about_you 隐藏 Birthday 同步失败 type={type(exc).__name__}")

    log("about_you 分段日期校验失败")
    return False


def _collect_visible_text_inputs(page) -> list[dict]:
    try:
        inputs = page.evaluate(
            """
            () => {
              const normalize = (value) => String(value || '').replace(/\\s+/g, ' ').trim();
              const nodes = Array.from(document.querySelectorAll("input:not([type='hidden']):not([disabled]):not([readonly])"));
              const visible = nodes.filter((el) => {
                const style = window.getComputedStyle(el);
                const rect = el.getBoundingClientRect();
                return style
                  && style.display !== 'none'
                  && style.visibility !== 'hidden'
                  && rect.width > 0
                  && rect.height > 0;
              });
              return visible.map((el, visibleIndex) => {
                const explicitLabels = Array.from(document.querySelectorAll('label'))
                  .filter((label) => String(label.getAttribute('for') || '') === String(el.id || ''))
                  .map((label) => normalize(label.textContent));
                const wrappedLabel = normalize(el.closest('label')?.textContent || '');
                const ariaLabel = normalize(el.getAttribute('aria-label'));
                const labelledByText = normalize(
                  String(el.getAttribute('aria-labelledby') || '')
                    .split(/\\s+/)
                    .filter(Boolean)
                    .map((id) => normalize(document.getElementById(id)?.textContent || ''))
                    .join(' ')
                );
                const parentText = normalize(el.parentElement?.textContent || '');
                return {
                  visibleIndex,
                  type: normalize(el.getAttribute('type') || el.type || ''),
                  name: normalize(el.getAttribute('name') || ''),
                  id: normalize(el.id || ''),
                  placeholder: normalize(el.getAttribute('placeholder') || ''),
                  ariaLabel,
                  labels: explicitLabels.filter(Boolean),
                  wrappedLabel,
                  labelledByText,
                  parentText,
                };
              });
            }
            """
        ) or []
    except Exception:
        inputs = []
    return [item for item in inputs if isinstance(item, dict)]


def _about_you_input_hints(entry: dict) -> str:
    parts: list[str] = []
    labels = entry.get("labels") or []
    if isinstance(labels, list):
        parts.extend(str(item or "") for item in labels)
    parts.extend(
        [
            str(entry.get("wrappedLabel") or ""),
            str(entry.get("labelledByText") or ""),
            str(entry.get("ariaLabel") or ""),
            str(entry.get("placeholder") or ""),
            str(entry.get("name") or ""),
            str(entry.get("id") or ""),
            str(entry.get("parentText") or ""),
        ]
    )
    return " ".join(part for part in parts if part).strip().lower()


def _pick_best_about_you_input(entries: list[dict], field: str, exclude_visible_indices: set[int] | None = None) -> dict | None:
    exclude = {int(value) for value in (exclude_visible_indices or set())}
    best_entry = None
    best_score = float("-inf")
    for entry in entries:
        try:
            visible_index = int(entry.get("visibleIndex"))
        except Exception:
            continue
        if visible_index in exclude:
            continue
        hints = _about_you_input_hints(entry)
        if not hints:
            continue

        score = 0
        if field == "name":
            if any(token in hints for token in ("full name", "fullname", "全名", "姓名", "nombre completo", "nom complet", "vollständiger name", "nome completo")):
                score += 10
            if any(token in hints for token in (" name ", "name", "autocomplete=name", "nombre", "nom", "nome")):
                score += 3
            if any(token in hints for token in ("age", "年龄", "edad", "âge", "alter", "idade", "birthday", "birth", "date of birth", "出生", "生日")):
                score -= 8
        elif field == "age":
            if any(token in hints for token in ("age", "年龄", "how old", "edad", "âge", "alter", "idade", "나이")):
                score += 10
            if any(token in hints for token in ("full name", "fullname", "全名", "姓名", "nombre completo", "nom complet")):
                score -= 10
            if "name" in hints and "age" not in hints and "年龄" not in hints and "edad" not in hints:
                score -= 6
            if any(token in hints for token in ("birthday", "birth", "date of birth", "出生", "生日", "fecha de nacimiento", "nascimento")):
                score -= 3
        else:
            continue

        if score > best_score:
            best_score = score
            best_entry = entry

    if best_score > 0:
        return best_entry

    if field == "age" and len(entries) == 2:
        ordered = []
        for entry in entries:
            try:
                visible_index = int(entry.get("visibleIndex"))
            except Exception:
                continue
            if visible_index not in exclude:
                ordered.append(entry)
        if len(ordered) == 1:
            return ordered[0]
        if len(ordered) == 2:
            return ordered[1]
    return None


def _derive_registration_state_from_page(page) -> dict:
    current_url = str(page.url or "")
    state = _extract_flow_state(None, current_url)
    if state.get("page_type"):
        return state

    if _find_first_selector(page, PASSWORD_INPUT_SELECTORS):
        page_type = "login_password" if _is_login_password_url(current_url) else "create_account_password"
        return _build_manual_flow_state(page_type, current_url)

    otp_selector = _find_first_selector(page, OTP_INPUT_SELECTORS)
    if otp_selector and "password" not in otp_selector:
        return _build_manual_flow_state("email_otp_verification", current_url)

    try:
        about_visible = bool(
            page.evaluate(
                """
                () => {
                  const inputs = Array.from(document.querySelectorAll("input:not([type='hidden'])"));
                  const text = String(document.body?.innerText || '').toLowerCase();
                  const hasName = inputs.some((el) => {
                    const hint = `${el.name || ''} ${el.id || ''} ${el.placeholder || ''}`.toLowerCase();
                    return hint.includes('name') || hint.includes('姓名') || hint.includes('全名');
                  });
                  const hasAgeOrBirth = inputs.some((el) => {
                    const hint = `${el.name || ''} ${el.id || ''} ${el.placeholder || ''}`.toLowerCase();
                    return hint.includes('age') || hint.includes('birth') || hint.includes('birthday') || hint.includes('年龄') || hint.includes('生日');
                  });
                  return (hasName && hasAgeOrBirth) || text.includes('about you');
                }
                """
            )
        )
    except Exception:
        about_visible = False
    if about_visible:
        return _build_manual_flow_state("about_you", current_url)

    return state


def _recover_signup_password_page(page, log) -> bool:
    if not _is_login_password_url(str(page.url or "")):
        return False
    if not _has_signup_registration_choice(page):
        return False
    selector = _click_first(page, SIGNUP_RECOVERY_SELECTORS, timeout=2)
    if not selector:
        return False
    log(f"密码页落到登录态，尝试点击注册入口恢复: {selector}")
    time.sleep(1.2)
    return True


def _wait_for_signup_entry_transition(page, log, timeout: int = 20) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _click_passwordless_login_if_available(page, log, context="邮箱页提交后"):
            time.sleep(0.5)
            continue
        state = _derive_registration_state_from_page(page)
        if state.get("page_type") in {
            "create_account_password",
            "login_password",
            "email_otp_verification",
            "about_you",
            "add_phone",
            "chatgpt_home",
            "oauth_callback",
        }:
            if state.get("page_type") == "login_password" and _recover_signup_password_page(page, log):
                return _derive_registration_state_from_page(page)
            return state
        error_text = _extract_auth_error_text(page)
        if error_text:
            raise RuntimeError(f"邮箱页提交失败: {error_text[:300]}")
        time.sleep(0.25)
    raise RuntimeError("邮箱页提交后未进入密码/验证码页面")


def _start_browser_signup_via_page(page, email: str, log) -> dict:
    for entry_url in (PLATFORM_LOGIN_ENTRY, f"{OPENAI_AUTH}/log-in"):
        try:
            log(f"打开 OpenAI 注册入口: {entry_url}")
            _goto_with_retry(page, entry_url, wait_until="domcontentloaded", timeout=30000, log=log)
        except Exception as exc:
            log(f"注册入口访问失败: {entry_url} -> {exc}")
            continue

        initial_state = _derive_registration_state_from_page(page)
        if initial_state.get("page_type") in {
            "create_account_password",
            "login_password",
            "email_otp_verification",
            "about_you",
            "add_phone",
        }:
            return initial_state

        email_selector = _wait_for_any_selector(page, EMAIL_INPUT_SELECTORS, timeout=12)
        if not email_selector:
            continue
        if not _fill_input_like_user(page, email_selector, email):
            raise RuntimeError("邮箱页填写失败")
        log(f"邮箱页输入框: {email_selector}")

        inline_state = _derive_registration_state_from_page(page)
        if inline_state.get("page_type") in {"create_account_password", "login_password"}:
            if inline_state.get("page_type") == "login_password" and _recover_signup_password_page(page, log):
                return _derive_registration_state_from_page(page)
            return inline_state

        submit_selector = _click_first(page, EMAIL_SUBMIT_SELECTORS, timeout=8)
        if submit_selector:
            log(f"邮箱页已点击继续按钮: {submit_selector}")
        elif _submit_form_with_fallback(page, email_selector):
            log("邮箱页未找到可点击 Continue，已使用表单 fallback 提交")
        else:
            raise RuntimeError("邮箱页未找到 Continue 按钮")

        return _wait_for_signup_entry_transition(page, log)

    raise RuntimeError("未找到 OpenAI 注册入口邮箱输入框")


def _start_browser_signup_via_authorize(page, email: str, device_id: str, log) -> dict:
    log("访问 ChatGPT 首页...")
    _goto_with_retry(page, f"{CHATGPT_APP}/", wait_until="domcontentloaded", timeout=30000, log=log)
    try:
        page.wait_for_load_state("domcontentloaded", timeout=10000)
    except Exception:
        pass
    # Extra settle: homepage often redirects again (locale / auth bootstrap).
    time.sleep(0.6)

    log("获取 CSRF token...")
    csrf_token = _get_browser_csrf_token(page, log=log)
    if not csrf_token:
        raise RuntimeError("获取 CSRF token 失败")

    log("提交注册邮箱")
    authorize_url = _start_browser_signin(page, email, device_id, csrf_token, log=log)
    if not authorize_url:
        raise RuntimeError("提交邮箱失败，未获取 authorize URL")

    final_url = _browser_authorize(page, authorize_url, log)
    if not final_url:
        raise RuntimeError("访问 authorize URL 失败")
    return _derive_registration_state_from_page(page)


def _get_cookies(page) -> dict:
    return {c["name"]: c["value"] for c in page.context.cookies()}


def _cookies_to_header(cookies_dict: dict) -> str:
    parts = []
    for name, value in (cookies_dict or {}).items():
        if name and value not in (None, ""):
            parts.append(f"{name}={value}")
    return "; ".join(parts)


def _decode_jwt_payload_no_verify(token: str) -> dict:
    try:
        parts = str(token or "").split(".")
        if len(parts) < 2:
            return {}
        payload = parts[1]
        payload += "=" * (-len(payload) % 4)
        raw = base64.urlsafe_b64decode(payload.encode("ascii"))
        data = json.loads(raw.decode("utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _extract_chatgpt_account_id(access_token: str) -> str:
    payload = _decode_jwt_payload_no_verify(access_token)
    auth_info = payload.get("https://api.openai.com/auth") or {}
    if isinstance(auth_info, dict):
        account_id = str(auth_info.get("chatgpt_account_id") or "").strip()
        if account_id:
            return account_id
    return str(payload.get("sub") or "").strip()


def _chatgpt_session_result_from_data(data: dict, page, cookies_dict: dict, log) -> tuple[dict | None, str]:
    if not isinstance(data, dict):
        return None, "session API JSON 不是对象"

    access_token = str(data.get("accessToken") or data.get("access_token") or "").strip()
    if not access_token:
        return None, "session API 未返回 accessToken"

    latest_cookies = dict(cookies_dict or {})
    try:
        latest_cookies.update(_get_cookies(page))
    except Exception as exc:
        log(f"ChatGPT session cookies 读取失败，使用已捕获 cookies: {exc}")
    session_token = str(latest_cookies.get("__Secure-next-auth.session-token") or "").strip()
    account_id = _extract_chatgpt_account_id(access_token)
    result = {
        "access_token": access_token,
        "refresh_token": str(data.get("refreshToken") or data.get("refresh_token") or "").strip(),
        "id_token": str(data.get("idToken") or data.get("id_token") or "").strip(),
        "session_token": session_token,
        "account_id": account_id,
        "workspace_id": str(data.get("workspaceId") or data.get("workspace_id") or "").strip(),
        "profile": data.get("user") if isinstance(data.get("user"), dict) else {},
        "expires_at": str(data.get("expires") or "").strip(),
        "cookies": _cookies_to_header(latest_cookies),
        "session": data,
    }
    log(
        "ChatGPT session 获取成功: "
        f"accessToken=yes, session_token={'yes' if session_token else 'no'}, "
        f"account_id={account_id or '-'}"
    )
    return result, ""


def _chatgpt_session_result_from_text(text: str, page, cookies_dict: dict, log) -> tuple[dict | None, str]:
    try:
        data = json.loads(text)
    except Exception as exc:
        return None, f"session API JSON 解析失败: {exc}"
    return _chatgpt_session_result_from_data(data, page, cookies_dict, log)


def _fetch_chatgpt_session_via_same_origin(page, cookies_dict: dict, log, session_url: str) -> tuple[dict | None, str, bool]:
    current_url = str(getattr(page, "url", "") or "")
    if "chatgpt.com" not in current_url.lower():
        return None, "", False

    log(f"浏览器内请求 ChatGPT session API: {session_url}")
    try:
        payload = page.evaluate(
            """
            async (sessionUrl) => {
              const response = await fetch(sessionUrl, {
                method: "GET",
                credentials: "include",
                headers: { "accept": "application/json" },
              });
              return {
                status: response.status,
                url: response.url,
                text: await response.text(),
              };
            }
            """,
            session_url,
        )
    except Exception as exc:
        return None, str(exc), True

    if not isinstance(payload, dict):
        return None, "session API 浏览器内请求未返回对象", True

    status = int(payload.get("status") or 0)
    response_url = str(payload.get("url") or "")
    text = str(payload.get("text") or "")
    log(f"ChatGPT session API 浏览器内请求状态: {status} url={response_url[:120]}")
    if status == 200 and text:
        return (*_chatgpt_session_result_from_text(text, page, cookies_dict, log), True)
    return None, f"session API HTTP {status}: {text[:200]}", True


def _fetch_chatgpt_session_from_page(page, cookies_dict: dict, log, timeout: int = 45) -> dict:
    deadline = time.time() + max(int(timeout or 0), 5)
    last_error = ""
    session_url = f"{CHATGPT_APP}/api/auth/session"
    log(f"打开 ChatGPT session API: {session_url}")

    while time.time() < deadline:
        same_origin_result, same_origin_error, same_origin_attempted = _fetch_chatgpt_session_via_same_origin(
            page,
            cookies_dict,
            log,
            session_url,
        )
        if same_origin_result:
            return same_origin_result
        if same_origin_attempted and same_origin_error:
            last_error = same_origin_error
            log(f"ChatGPT session API 浏览器内请求暂未拿到 token: {last_error}")
            if "object has no attribute 'evaluate'" not in last_error:
                time.sleep(2)
                continue

        try:
            response = page.goto(session_url, wait_until="domcontentloaded", timeout=15000)
            status = int(response.status if response else 0)
            if response:
                try:
                    text = response.text()
                except Exception as body_exc:
                    last_error = str(body_exc)
                    log(f"ChatGPT session API 响应体不可直接读取，改读页面正文: {last_error}")
                    text = page.locator("body").inner_text(timeout=3000)
            else:
                text = page.locator("body").inner_text(timeout=3000)
            current_url = str(getattr(page, "url", "") or "")
            log(f"ChatGPT session API 状态: {status} url={current_url[:120]}")
            if status == 200 and text:
                result, error = _chatgpt_session_result_from_text(text, page, cookies_dict, log)
                if result:
                    return result
                last_error = error
            else:
                last_error = f"session API HTTP {status}: {text[:200]}"
            log(f"ChatGPT session API 暂未拿到 token: {last_error}")
        except Exception as exc:
            last_error = str(exc)
            log(f"ChatGPT session API 打开异常: {last_error}")
        time.sleep(2)

    raise RuntimeError(f"ChatGPT session 未返回 accessToken: {last_error}")


def _random_chrome_ua() -> str:
    patch = random.randint(0, 220)
    return (
        f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        f"(KHTML, like Gecko) Chrome/136.0.7103.{patch} Safari/537.36"
    )


def _infer_sec_ch_ua(user_agent: str) -> str:
    match = re.search(r"Chrome/(\d+)", str(user_agent or ""))
    major = str(match.group(1) if match else "136")
    return f'"Chromium";v="{major}", "Google Chrome";v="{major}", "Not.A/Brand";v="99"'


def _build_browser_headers(
    *,
    user_agent: str,
    accept: str,
    referer: str = "",
    origin: str = "",
    content_type: str = "",
    navigation: bool = False,
    extra_headers: dict | None = None,
) -> dict:
    headers = {
        "user-agent": user_agent or _random_chrome_ua(),
        "accept-language": "en-US,en;q=0.9",
        "sec-ch-ua": _infer_sec_ch_ua(user_agent),
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "accept": accept,
    }
    if referer:
        headers["referer"] = referer
    if origin:
        headers["origin"] = origin
    if content_type:
        headers["content-type"] = content_type
    if navigation:
        headers["sec-fetch-dest"] = "document"
        headers["sec-fetch-mode"] = "navigate"
        headers["sec-fetch-user"] = "?1"
        headers["upgrade-insecure-requests"] = "1"
    else:
        headers["sec-fetch-dest"] = "empty"
        headers["sec-fetch-mode"] = "cors"
    for key, value in dict(extra_headers or {}).items():
        if value is not None:
            headers[key] = value
    return headers


def _browser_pause(page, *, headed: bool = True):
    delay_ms = random.randint(150, 450) if headed else random.randint(60, 180)
    try:
        page.wait_for_timeout(delay_ms)
    except Exception:
        time.sleep(delay_ms / 1000)


def _generate_datadog_trace_headers() -> dict:
    trace_hex = secrets.token_hex(8).rjust(16, "0")
    parent_hex = secrets.token_hex(8).rjust(16, "0")
    trace_id = str(int(trace_hex, 16))
    parent_id = str(int(parent_hex, 16))
    return {
        "traceparent": f"00-0000000000000000{trace_hex}-{parent_hex}-01",
        "tracestate": "dd=s:1;o:rum",
        "x-datadog-origin": "rum",
        "x-datadog-parent-id": parent_id,
        "x-datadog-sampling-priority": "1",
        "x-datadog-trace-id": trace_id,
    }


def _infer_page_type(data: dict | None, current_url: str = "") -> str:
    raw = data if isinstance(data, dict) else {}
    page_type = str(((raw.get("page") or {}).get("type")) or "").strip().lower().replace("-", "_").replace("/", "_").replace(" ", "_")
    if page_type:
        return page_type
    url = (current_url or "").lower()
    if "code=" in url:
        return "oauth_callback"
    if "create-account/password" in url:
        return "create_account_password"
    if "email-verification" in url or "email-otp" in url:
        return "email_otp_verification"
    if "about-you" in url:
        return "about_you"
    if "log-in/password" in url:
        return "login_password"
    if "sign-in-with-chatgpt" in url and "consent" in url:
        return "consent"
    if "workspace" in url and "select" in url:
        return "workspace_selection"
    if "organization" in url and "select" in url:
        return "organization_selection"
    if "add-phone" in url:
        return "add_phone"
    if "/api/oauth/oauth2/auth" in url:
        return "external_url"
    if "chatgpt.com" in url:
        return "chatgpt_home"
    return ""


class AuthResponseObserver:
    """Collect a small redacted auth-response envelope for state transitions."""

    _MARKERS = (
        "/api/accounts/",
        "/api/auth/",
        "/oauth/",
        "/sign-in-with-chatgpt/",
    )

    def __init__(self, *, flow_epoch: str = "") -> None:
        self.flow_epoch = str(flow_epoch or uuid.uuid4().hex[:12])
        self._lock = threading.Lock()
        self._items: list[dict[str, Any]] = []

    @staticmethod
    def _error_code(payload: dict[str, Any]) -> str:
        error = payload.get("error")
        if isinstance(error, dict):
            return str(
                error.get("code")
                or error.get("type")
                or error.get("error_code")
                or ""
            )
        errors = payload.get("errors")
        if isinstance(errors, list) and errors and isinstance(errors[0], dict):
            return str(
                errors[0].get("code")
                or errors[0].get("type")
                or errors[0].get("error_code")
                or ""
            )
        return str(
            payload.get("error_code")
            or payload.get("code")
            or (error if isinstance(error, str) else "")
        )

    def observe(self, page) -> None:
        page.on("response", self._on_response)

    def _on_response(self, response) -> None:
        url = str(getattr(response, "url", "") or "")
        if not any(marker in url for marker in self._MARKERS):
            return
        status = int(getattr(response, "status", 0) or 0)
        headers = {}
        try:
            headers = dict(getattr(response, "headers", {}) or {})
        except Exception:
            headers = {}
        payload: dict[str, Any] = {}
        content_type = str(headers.get("content-type") or "")
        if "json" in content_type:
            try:
                value = response.json()
                payload = value if isinstance(value, dict) else {}
            except Exception:
                payload = {}
        page_data = payload.get("page") if isinstance(payload.get("page"), dict) else {}
        page_type = str(page_data.get("type") or payload.get("page_type") or "")
        continue_path = str(
            payload.get("continue_url")
            or headers.get("location")
            or ""
        )
        if continue_path:
            try:
                continue_path = urlparse(continue_path).path or continue_path[:160]
            except Exception:
                continue_path = continue_path[:160]
        item = {
            "endpoint_group": next((marker.strip("/").replace("/", ".") for marker in self._MARKERS if marker in url), "auth"),
            "http_status": status,
            "error_code": self._error_code(payload),
            "page_type": page_type,
            "continue_path": continue_path,
            "cf_mitigated": str(headers.get("cf-mitigated") or "").lower() == "challenge",
            "flow_epoch": self.flow_epoch,
            "at": time.monotonic(),
        }
        with self._lock:
            self._items.append(item)
            del self._items[:-40]

    def latest(self, *, since: float = 0.0) -> dict[str, Any] | None:
        with self._lock:
            for item in reversed(self._items):
                if float(item.get("at") or 0) >= since:
                    return dict(item)
        return None

    def latest_error(self, *, since: float = 0.0) -> dict[str, Any] | None:
        with self._lock:
            for item in reversed(self._items):
                if float(item.get("at") or 0) < since:
                    continue
                if (
                    str(item.get("error_code") or "").strip()
                    or int(item.get("http_status") or 0) >= 400
                    or bool(item.get("cf_mitigated"))
                ):
                    return dict(item)
        return None


def _observed_auth_failure(
    observer: AuthResponseObserver | None,
    *,
    since: float,
    current_url: str,
) -> dict[str, Any] | None:
    observed = observer.latest_error(since=since) if observer else None
    if not observed:
        return None
    raw_code = str(observed.get("error_code") or "").strip()
    lowered = raw_code.lower()
    http_status = int(observed.get("http_status") or 0)
    if http_status == 429:
        stable_code = "HTTP_RATE_LIMIT"
    elif "identity_provider_mismatch" in lowered:
        stable_code = "AUTH_IDENTITY_PROVIDER_MISMATCH"
    elif bool(observed.get("cf_mitigated")):
        stable_code = "CF_CHALLENGE"
    elif any(
        marker in lowered
        for marker in (
            "invalid_code",
            "invalid_otp",
            "invalid otp",
            "invalid verification code",
        )
    ):
        stable_code = "OTP_INVALID"
    elif "session" in lowered or "state" in lowered:
        stable_code = "AUTH_SESSION_DESYNC"
    else:
        stable_code = "AUTH_INVALID_STEP"
    return {
        "ok": False,
        "status": http_status or 400,
        "url": current_url,
        "data": observed,
        "text": f"{stable_code}: {raw_code or 'auth request failed'}",
    }


def _extract_flow_state(data: dict | None, current_url: str = "") -> dict:
    raw = data if isinstance(data, dict) else {}
    page = raw.get("page") or {}
    payload = page.get("payload") or {}
    continue_url = str(raw.get("continue_url") or payload.get("url") or "").strip()
    if continue_url and continue_url.startswith("/"):
        continue_url = urljoin(OPENAI_AUTH, continue_url)
    effective_url = continue_url or current_url
    return {
        "page_type": _infer_page_type(raw, effective_url),
        "continue_url": continue_url,
        "method": str(raw.get("method") or payload.get("method") or "GET").upper(),
        "current_url": effective_url,
        "payload": payload if isinstance(payload, dict) else {},
        "raw": raw,
    }


def _extract_code_from_url(url: str) -> str:
    if not url or "code=" not in url:
        return ""
    try:
        from urllib.parse import parse_qs, urlparse as _up

        parsed = _up(url)
        values = parse_qs(parsed.query, keep_blank_values=True)
        return str((values.get("code") or [""])[0] or "").strip()
    except Exception:
        return ""


def _normalize_url(target_url: str, base_url: str = OPENAI_AUTH) -> str:
    value = str(target_url or "").strip()
    if not value:
        return ""
    if value.startswith(("http://", "https://")):
        return value
    try:
        return urljoin(base_url, value)
    except Exception:
        return value


def _decode_jwt_payload(token: str) -> dict:
    try:
        parts = token.split(".")
        if len(parts) < 2:
            return {}
        payload = parts[1]
        pad = "=" * ((4 - (len(payload) % 4)) % 4)
        return json.loads(base64.urlsafe_b64decode((payload + pad).encode("ascii")).decode("utf-8"))
    except Exception:
        return {}


class _SentinelTokenGenerator:
    def __init__(self, device_id: str, user_agent: str):
        self.device_id = device_id or str(uuid.uuid4())
        self.user_agent = user_agent or _random_chrome_ua()
        self.sid = str(uuid.uuid4())

    @staticmethod
    def _fnv1a32(text: str) -> str:
        h = 2166136261
        for ch in text:
            h ^= ord(ch)
            h = (h * 16777619) & 0xFFFFFFFF
        h ^= (h >> 16)
        h = (h * 2246822507) & 0xFFFFFFFF
        h ^= (h >> 13)
        h = (h * 3266489909) & 0xFFFFFFFF
        h ^= (h >> 16)
        return f"{h & 0xFFFFFFFF:08x}"

    @staticmethod
    def _b64(data) -> str:
        return base64.b64encode(json.dumps(data, separators=(",", ":")).encode("utf-8")).decode("ascii")

    def _config(self) -> list:
        perf_now = 1000 + random.random() * 49000
        return [
            "1920x1080",
            time.strftime("%a, %d %b %Y %H:%M:%S GMT+0000 (Coordinated Universal Time)", time.gmtime()),
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
            random.choice([4, 8, 12, 16]),
            int(time.time() * 1000 - perf_now),
        ]

    def generate_requirements_token(self) -> str:
        cfg = self._config()
        cfg[3] = 1
        cfg[9] = round(5 + random.random() * 45)
        return "gAAAAAC" + self._b64(cfg)

    def generate_token(self, seed: str, difficulty: str) -> str:
        max_attempts = 500000
        cfg = self._config()
        start_ms = int(time.time() * 1000)
        diff = str(difficulty or "0")
        for nonce in range(max_attempts):
            cfg[3] = nonce
            cfg[9] = round(int(time.time() * 1000) - start_ms)
            encoded = self._b64(cfg)
            digest = self._fnv1a32((seed or "") + encoded)
            if digest[: len(diff)] <= diff:
                return "gAAAAAB" + encoded + "~S"
        return "gAAAAAB" + self._b64(None)


def _is_execution_context_error(exc: BaseException) -> bool:
    msg = str(exc or "").lower()
    return any(
        token in msg
        for token in (
            "execution context was destroyed",
            "most likely because of a navigation",
            "cannot find context with specified id",
            "target closed",
            "frame was detached",
            "frame has been detached",
        )
    )


def _browser_fetch(
    page,
    url: str,
    *,
    method: str = "GET",
    headers: dict | None = None,
    body: str | None = None,
    redirect: str = "manual",
    timeout_ms: int = 30000,
) -> dict:
    """HTTP from browser storage without page.evaluate when possible.

    ChatGPT often navigates the homepage while NextAuth CSRF/signin runs.
    ``page.evaluate(fetch)`` then dies with "Execution context was destroyed".
    Prefer Playwright's context.request (cookie jar, no page JS world).
    """
    method_u = str(method or "GET").upper()
    hdrs = dict(headers or {})
    # Playwright Python uses milliseconds for request timeout.
    timeout = float(max(int(timeout_ms or 30000), 1000))

    # 1) Preferred: APIRequestContext — survives page navigations.
    try:
        req = page.context.request
        kwargs: dict[str, Any] = {"headers": hdrs, "timeout": timeout}
        if method_u == "GET":
            resp = req.get(url, **kwargs)
        elif method_u == "POST":
            if body is not None:
                kwargs["data"] = body
            resp = req.post(url, **kwargs)
        else:
            kwargs["method"] = method_u
            if body is not None:
                kwargs["data"] = body
            resp = req.fetch(url, **kwargs)
        text = ""
        try:
            text = resp.text()
        except Exception:
            pass
        data = None
        try:
            data = resp.json()
        except Exception:
            try:
                data = json.loads(text) if text else None
            except Exception:
                data = None
        status = int(getattr(resp, "status", 0) or 0)
        return {
            "ok": bool(getattr(resp, "ok", 200 <= status < 300)),
            "status": status,
            "url": str(getattr(resp, "url", None) or url),
            "headers": dict(getattr(resp, "headers", {}) or {}),
            "text": text,
            "data": data,
        }
    except Exception:
        pass

    # 2) Fallback: page.evaluate with navigation-safe retries.
    last_err = ""
    for attempt in range(1, 4):
        try:
            try:
                page.wait_for_load_state("domcontentloaded", timeout=8000)
            except Exception:
                pass
            return page.evaluate(
                """
                async ({ url, method, headers, body, redirect, timeoutMs }) => {
                  const controller = new AbortController();
                  const timer = setTimeout(
                    () => controller.abort(new Error(`fetch timeout after ${timeoutMs}ms`)),
                    timeoutMs,
                  );
                  try {
                    const resp = await fetch(url, {
                      method,
                      headers: headers || {},
                      body: body === null || body === undefined ? undefined : body,
                      redirect,
                      credentials: "include",
                      signal: controller.signal,
                    });
                    const respHeaders = {};
                    resp.headers.forEach((v, k) => { respHeaders[k] = v; });
                    let text = '';
                    try { text = await resp.text(); } catch {}
                    let data = null;
                    try { data = JSON.parse(text); } catch {}
                    return {
                      ok: resp.ok,
                      status: resp.status,
                      url: resp.url || url,
                      headers: respHeaders,
                      text,
                      data,
                    };
                  } catch (e) {
                    return {
                      ok: false,
                      status: 0,
                      url,
                      headers: {},
                      text: String(e && e.message || e),
                      data: null,
                    };
                  } finally {
                    clearTimeout(timer);
                  }
                }
                """,
                {
                    "url": url,
                    "method": method_u,
                    "headers": hdrs,
                    "body": body,
                    "redirect": redirect,
                    "timeoutMs": int(timeout_ms or 30000),
                },
            )
        except Exception as exc:
            last_err = str(exc)
            if attempt < 3 and _is_execution_context_error(exc):
                time.sleep(0.4 * attempt)
                try:
                    page.wait_for_load_state("domcontentloaded", timeout=10000)
                except Exception:
                    pass
                continue
            return {
                "ok": False,
                "status": 0,
                "url": url,
                "headers": {},
                "text": last_err or str(exc),
                "data": None,
            }
    return {
        "ok": False,
        "status": 0,
        "url": url,
        "headers": {},
        "text": last_err or "browser_fetch failed",
        "data": None,
    }


def _build_browser_sentinel_token(page, device_id: str, flow: str, user_agent: str) -> str:
    generator = _SentinelTokenGenerator(device_id, user_agent)
    req_body = json.dumps(
        {"p": generator.generate_requirements_token(), "id": device_id, "flow": flow},
        separators=(",", ":"),
    )
    result = _browser_fetch(
        page,
        SENTINEL_REQ_URL,
        method="POST",
        headers=_build_browser_headers(
            user_agent=user_agent,
            accept="*/*",
            referer=SENTINEL_FRAME_URL,
            origin=SENTINEL_BASE,
            content_type="text/plain;charset=UTF-8",
            extra_headers={
                "sec-fetch-site": "same-origin",
            },
        ),
        body=req_body,
        redirect="follow",
    )
    data = result.get("data") or {}
    challenge_token = str(data.get("token") or "").strip()
    if not challenge_token:
        return ""
    pow_meta = data.get("proofofwork") or {}
    if pow_meta.get("required") and pow_meta.get("seed"):
        p_value = generator.generate_token(str(pow_meta.get("seed") or ""), str(pow_meta.get("difficulty") or "0"))
    else:
        p_value = generator.generate_requirements_token()
    return json.dumps(
        {
            "p": p_value,
            "t": "",
            "c": challenge_token,
            "id": device_id,
            "flow": flow,
        },
        separators=(",", ":"),
    )


def _is_registration_complete(state: dict) -> bool:
    page_type = str(state.get("page_type") or "")
    return page_type in {"callback", "oauth_callback", "chatgpt_home"}


def _handle_post_signup_onboarding(page, log) -> None:
    current_url = str(page.url or "")
    if "chatgpt.com" not in current_url:
        return
    try:
        # 可能弹出 persistent storage 提示，优先点 Allow，不影响主流程也可点 Block。
        allow_selector = _click_first(
            page,
            [
                'button:has-text("Allow")',
                'button:has-text("allow")',
                'button:has-text("Block")',
                'button:has-text("block")',
                'button:has-text("許可")',
                'button:has-text("ブロック")',
                'button:has-text("拒否")',
            ],
            timeout=1,
        )
        if allow_selector:
            log(f"已处理浏览器弹窗: {allow_selector}")
    except Exception:
        pass

    # 新账号常见 onboarding 问卷页，优先 Skip。
    try:
        if page.locator("text=What brings you to ChatGPT?").first.count() > 0:
            skip_selector = _click_first(
                page,
                [
                    'button:has-text("Skip")',
                    'button:has-text("skip")',
                    'button:has-text("Next")',
                    'button:has-text("next")',
                    'button:has-text("スキップ")',
                    'button:has-text("次へ")',
                ],
                timeout=5,
            )
            if skip_selector:
                log(f"已处理 onboarding 页面: {skip_selector}")
                _browser_pause(page)
    except Exception:
        pass


def _is_password_registration(state: dict) -> bool:
    return str(state.get("page_type") or "") in {"create_account_password", "password"}


def _is_email_otp(state: dict) -> bool:
    target = f"{state.get('continue_url') or ''} {state.get('current_url') or ''}".lower()
    return str(state.get("page_type") or "") == "email_otp_verification" or "email-verification" in target or "email-otp" in target


def _is_about_you(state: dict) -> bool:
    target = f"{state.get('continue_url') or ''} {state.get('current_url') or ''}".lower()
    return str(state.get("page_type") or "") == "about_you" or "about-you" in target


def _requires_registration_navigation(state: dict) -> bool:
    if str(state.get("method") or "GET").upper() != "GET":
        return False
    if str(state.get("page_type") or "") == "external_url" and state.get("continue_url"):
        return True
    continue_url = str(state.get("continue_url") or "")
    current_url = str(state.get("current_url") or "")
    return bool(continue_url and continue_url != current_url)


def _browser_add_cookies(page, cookies: list[dict]) -> None:
    try:
        page.context.add_cookies(cookies)
    except Exception:
        pass


def _seed_browser_device_id(page, device_id: str) -> None:
    _browser_add_cookies(
        page,
        [
            {"name": "oai-did", "value": device_id, "domain": "chatgpt.com", "path": "/"},
            {"name": "oai-did", "value": device_id, "domain": ".chatgpt.com", "path": "/"},
            {"name": "oai-did", "value": device_id, "domain": "openai.com", "path": "/"},
            {"name": "oai-did", "value": device_id, "domain": "auth.openai.com", "path": "/"},
            {"name": "oai-did", "value": device_id, "domain": ".auth.openai.com", "path": "/"},
        ],
    )


def _get_browser_csrf_token(page, log: Optional[Callable[[str], None]] = None) -> str:
    _log = log or (lambda *_a, **_k: None)
    last_detail = ""
    for attempt in range(1, 4):
        result = _browser_fetch(
            page,
            f"{CHATGPT_APP}/api/auth/csrf",
            method="GET",
            headers={
                "accept": "application/json",
                "referer": f"{CHATGPT_APP}/",
                "sec-fetch-site": "same-origin",
            },
            redirect="follow",
        )
        if result.get("ok") and isinstance(result.get("data"), dict):
            token = str((result.get("data") or {}).get("csrfToken") or "").strip()
            if token:
                return token
        last_detail = str(result.get("text") or result.get("status") or "")
        if attempt < 3:
            _log(f"CSRF 未就绪（第 {attempt}/3 次）: {last_detail[:120]}")
            time.sleep(0.5 * attempt)
            try:
                page.wait_for_load_state("domcontentloaded", timeout=8000)
            except Exception:
                pass
    return ""


def _start_browser_signin(
    page,
    email: str,
    device_id: str,
    csrf_token: str,
    log: Optional[Callable[[str], None]] = None,
) -> str:
    from urllib.parse import urlencode

    _log = log or (lambda *_a, **_k: None)
    query = urlencode(
        {
            "prompt": "login",
            "ext-oai-did": device_id,
            "auth_session_logging_id": str(uuid.uuid4()),
            "screen_hint": "login_or_signup",
            "login_hint": email,
        }
    )
    body = urlencode(
        {
            "callbackUrl": f"{CHATGPT_APP}/",
            "csrfToken": csrf_token,
            "json": "true",
        }
    )
    last_detail = ""
    for attempt in range(1, 4):
        result = _browser_fetch(
            page,
            f"{CHATGPT_APP}/api/auth/signin/openai?{query}",
            method="POST",
            headers={
                "accept": "application/json",
                "referer": f"{CHATGPT_APP}/",
                "origin": CHATGPT_APP,
                "content-type": "application/x-www-form-urlencoded",
                "sec-fetch-site": "same-origin",
            },
            body=body,
            redirect="follow",
        )
        if result.get("ok") and isinstance(result.get("data"), dict):
            url = str((result.get("data") or {}).get("url") or "").strip()
            if url:
                return url
        last_detail = str(result.get("text") or result.get("status") or "")
        if attempt < 3:
            _log(f"signin 未返回 authorize URL（第 {attempt}/3 次）: {last_detail[:120]}")
            time.sleep(0.5 * attempt)
    return ""


def _browser_authorize(page, auth_url: str, log) -> str:
    if not auth_url:
        return ""
    try:
        _goto_with_retry(page, auth_url, wait_until="domcontentloaded", timeout=30000, log=log)
        final_url = page.url
        log(f"Authorize -> {final_url[:120]}")
        return final_url
    except Exception as exc:
        log(f"Authorize 失败: {exc}")
        return ""


def _submit_oauth_password_direct(
    page,
    password: str,
    log,
    auth_observer: AuthResponseObserver | None = None,
) -> dict:
    """OAuth 流程专用：直接填密码登录，不尝试恢复到注册态。"""
    input_selector = _wait_for_any_selector(page, PASSWORD_INPUT_SELECTORS, timeout=15)
    if not input_selector:
        # 密码输入框没出现，可能页面还在加载或跳转了
        # 等一下再试
        time.sleep(2)
        input_selector = _wait_for_any_selector(page, PASSWORD_INPUT_SELECTORS, timeout=10)
    if not input_selector:
        raise RuntimeError("OAuth 密码页未找到输入框")
    if not _fill_input_like_user(page, input_selector, password):
        raise RuntimeError("OAuth 密码页填写失败")
    log(f"  OAuth 密码页输入框: {input_selector}")
    _browser_pause(page)

    submit_started = time.monotonic()
    submit_selector = _click_first(page, PASSWORD_SUBMIT_SELECTORS, timeout=8)
    if submit_selector:
        log(f"  OAuth 密码页已点击继续按钮: {submit_selector}")
    elif _submit_form_with_fallback(page, input_selector):
        log("  OAuth 密码页使用表单 fallback 提交")
    else:
        raise RuntimeError("OAuth 密码页未找到 Continue 按钮")

    deadline = time.time() + 20
    while time.time() < deadline:
        current_url = str(page.url or "")
        observed_failure = _observed_auth_failure(
            auth_observer,
            since=submit_started,
            current_url=current_url,
        )
        if observed_failure:
            return observed_failure
        state = _derive_registration_state_from_page(page)
        page_type = str(state.get("page_type") or "")
        if page_type in {"email_otp_verification", "about_you", "consent", "workspace_selection",
                         "organization_selection", "add_phone", "oauth_callback", "chatgpt_home", "external_url"}:
            return {"ok": True, "status": 200, "url": current_url, "data": None, "text": ""}
        if "code=" in current_url:
            return {"ok": True, "status": 200, "url": current_url, "data": None, "text": ""}
        error_text = _extract_auth_error_text(page)
        if error_text:
            return {"ok": False, "status": 400, "url": current_url, "data": None, "text": error_text}
        time.sleep(0.5)
    return {"ok": False, "status": 0, "url": str(page.url or ""), "data": None, "text": "OAuth 密码提交后未跳转"}


def _submit_password_via_page(
    page,
    password: str,
    log,
    auth_observer: AuthResponseObserver | None = None,
) -> dict:
    if _recover_signup_password_page(page, log):
        time.sleep(1)

    input_selector = _wait_for_any_selector(page, PASSWORD_INPUT_SELECTORS, timeout=15)
    if not input_selector:
        raise RuntimeError("密码页未找到输入框")
    if not _fill_input_like_user(page, input_selector, password):
        raise RuntimeError("密码页填写失败")
    log(f"密码页输入框: {input_selector}")
    _browser_pause(page)

    start_url = str(page.url or "")
    submit_started = time.monotonic()
    submit_selector = _click_first(page, PASSWORD_SUBMIT_SELECTORS, timeout=8)
    if submit_selector:
        log(f"密码页已点击继续按钮: {submit_selector}")
    elif _submit_form_with_fallback(page, input_selector):
        log("密码页未找到可点击 Continue，已使用表单 fallback 提交")
    else:
        raise RuntimeError("密码页未找到 Continue 按钮")

    deadline = time.time() + 20
    last_url = str(page.url or "")
    while time.time() < deadline:
        current_url = str(page.url or "")
        last_url = current_url or last_url
        observed_failure = _observed_auth_failure(
            auth_observer,
            since=submit_started,
            current_url=current_url,
        )
        if observed_failure:
            return observed_failure
        state = _derive_registration_state_from_page(page)
        page_type = str(state.get("page_type") or "")
        if page_type in {"email_otp_verification", "about_you", "add_phone", "oauth_callback", "chatgpt_home"}:
            return {"ok": True, "status": 200, "url": current_url, "data": None, "text": ""}
        if current_url != start_url and page_type and page_type not in {"create_account_password", "login_password"}:
            return {"ok": True, "status": 200, "url": current_url, "data": None, "text": ""}
        if page_type == "login_password" and _recover_signup_password_page(page, log):
            input_selector = _wait_for_any_selector(page, PASSWORD_INPUT_SELECTORS, timeout=5)
            if not input_selector:
                return {"ok": False, "status": 400, "url": current_url, "data": None, "text": "登录密码页恢复后未找到注册密码输入框"}
            if not _fill_input_like_user(page, input_selector, password):
                return {"ok": False, "status": 400, "url": current_url, "data": None, "text": "登录密码页恢复后密码重新填写失败"}
            submit_selector = _click_first(page, PASSWORD_SUBMIT_SELECTORS, timeout=5)
            if submit_selector:
                log(f"恢复后重新点击密码提交按钮: {submit_selector}")
                start_url = str(page.url or start_url)
                time.sleep(0.4)
                continue
            if _submit_form_with_fallback(page, input_selector):
                log("恢复后未找到密码提交按钮，已使用表单 fallback 提交")
                start_url = str(page.url or start_url)
                time.sleep(0.4)
                continue
            return {"ok": False, "status": 400, "url": current_url, "data": None, "text": "登录密码页恢复后未找到提交方式"}
        error_text = _extract_auth_error_text(page)
        if error_text:
            return {"ok": False, "status": 400, "url": current_url, "data": None, "text": error_text}
        time.sleep(0.5)
    return {"ok": False, "status": 0, "url": last_url, "data": None, "text": "密码页提交后未跳转"}


_OTP_INPUT_SELECTOR = (
    'input[autocomplete="one-time-code"], input[name="code" i], '
    'input[name*="otp" i], input[inputmode="numeric"], '
    'input[type="tel"], input[type="number"]'
)


def _fill_otp_input(page, otp: str, log) -> dict[str, Any]:
    errors: list[str] = []
    diagnostics: dict[str, Any] = {
        "ok": False,
        "method": "none",
        "candidates": 0,
        "visible": 0,
        "enabled": 0,
    }
    try:
        native = page.evaluate(
            """async ({ selector, code }) => {
                const candidates = Array.from(document.querySelectorAll(selector));
                const visible = candidates.filter((node) => {
                    const style = window.getComputedStyle(node);
                    const rect = node.getBoundingClientRect();
                    return style.display !== 'none' && style.visibility !== 'hidden'
                        && Number(style.opacity || 1) > 0 && rect.width > 0 && rect.height > 0;
                });
                const enabled = visible.filter((node) => !node.disabled && !node.readOnly);
                const setValue = (node, value) => {
                    const setter = Object.getOwnPropertyDescriptor(
                        window.HTMLInputElement.prototype, 'value'
                    )?.set;
                    if (setter) setter.call(node, value);
                    else node.value = value;
                    node.dispatchEvent(new Event('input', { bubbles: true }));
                    node.dispatchEvent(new Event('change', { bubbles: true }));
                };
                let method = 'none';
                let readback = '';
                const split = enabled.length >= code.length
                    && enabled.slice(0, code.length).every((node) => node.maxLength === 1);
                if (split) {
                    enabled.slice(0, code.length).forEach((node, index) => {
                        node.focus();
                        setValue(node, code[index]);
                    });
                    method = 'native-split';
                    readback = enabled.slice(0, code.length).map((node) => node.value).join('');
                } else if (enabled.length) {
                    const target = enabled.find((node) => node.maxLength !== 1) || enabled[0];
                    target.focus();
                    setValue(target, code);
                    method = 'native-single';
                    readback = String(target.value || '');
                }
                await new Promise((resolve) => window.setTimeout(resolve, 50));
                if (split) {
                    readback = enabled.slice(0, code.length).map((node) => node.value).join('');
                } else if (enabled.length) {
                    readback = String((enabled.find((node) => node.maxLength !== 1) || enabled[0]).value || '');
                }
                return {
                    ok: readback === code,
                    method,
                    candidates: candidates.length,
                    visible: visible.length,
                    enabled: enabled.length,
                    readbackLength: readback.length,
                };
            }""",
            {"selector": _OTP_INPUT_SELECTOR, "code": otp},
        )
        if isinstance(native, dict):
            diagnostics.update(native)
        if diagnostics.get("ok"):
            log(
                "OTP 输入完成 "
                f"method={diagnostics.get('method')} candidates={diagnostics.get('candidates', 0)} "
                f"visible={diagnostics.get('visible', 0)} enabled={diagnostics.get('enabled', 0)} "
                f"code_length={len(otp)} numeric={'yes' if otp.isdigit() else 'no'}"
            )
            return diagnostics
    except Exception as exc:
        errors.append(f"native:{type(exc).__name__}")

    try:
        candidates = page.locator(_OTP_INPUT_SELECTOR)
        count = int(candidates.count())
        diagnostics["candidates"] = max(int(diagnostics.get("candidates") or 0), count)
        for index in range(count):
            target = candidates.nth(index)
            try:
                if not target.is_visible(timeout=1200):
                    continue
                diagnostics["visible"] = max(int(diagnostics.get("visible") or 0), 1)
                if hasattr(target, "is_enabled") and not target.is_enabled(timeout=1200):
                    continue
                diagnostics["enabled"] = max(int(diagnostics.get("enabled") or 0), 1)
                target.fill(otp, timeout=2000)
                readback = str(target.input_value(timeout=1200) or "").strip()
                if readback == otp:
                    diagnostics.update(
                        {"ok": True, "method": "playwright-fill", "readbackLength": len(readback)}
                    )
                    log(
                        "OTP 输入完成 method=playwright-fill "
                        f"candidates={diagnostics['candidates']} visible={diagnostics['visible']} "
                        f"enabled={diagnostics['enabled']} code_length={len(otp)} "
                        f"numeric={'yes' if otp.isdigit() else 'no'}"
                    )
                    return diagnostics
            except Exception as exc:
                errors.append(f"locator:{type(exc).__name__}")
    except Exception as exc:
        errors.append(f"locator-list:{type(exc).__name__}")

    diagnostics["errors"] = sorted(set(errors))[:4]
    log(
        "OTP 输入失败 "
        f"candidates={diagnostics.get('candidates', 0)} visible={diagnostics.get('visible', 0)} "
        f"enabled={diagnostics.get('enabled', 0)} errors={','.join(diagnostics['errors']) or '-'} "
        f"code_length={len(otp)} numeric={'yes' if otp.isdigit() else 'no'}"
    )
    return diagnostics


def _submit_otp_via_page(
    page,
    code: str,
    log,
    auth_observer: AuthResponseObserver | None = None,
) -> dict:
    otp = str(code or "").strip()
    if not otp:
        return {"ok": False, "status": 400, "url": page.url, "data": None, "text": "验证码为空"}

    try:
        page.wait_for_load_state("domcontentloaded", timeout=5000)
    except Exception as exc:
        log(f"OTP 页面加载状态未就绪 type={type(exc).__name__}")
    time.sleep(0.5)

    submit_started = time.monotonic()
    fill_result: dict[str, Any] = {}
    captured_response = None

    def _is_otp_response(response) -> bool:
        response_url = str(getattr(response, "url", "") or "")
        return "/api/accounts/" in response_url and (
            "otp" in response_url.lower() or "authorize" in response_url.lower()
        )

    expect_response = getattr(page, "expect_response", None)
    try:
        if callable(expect_response):
            with expect_response(_is_otp_response, timeout=12000) as response_info:
                fill_result = _fill_otp_input(page, otp, log)
                if fill_result.get("ok"):
                    _browser_pause(page)
                    submit_selector = _click_first(
                        page,
                        [
                            'button[type="submit"]',
                            'button[data-testid="continue-button"]',
                            'button:has-text("Continue")',
                            'button:has-text("continue")',
                            'button:has-text("Verify")',
                            'button:has-text("verify")',
                            'button:has-text("Next")',
                            'button:has-text("next")',
                            'button:has-text("続ける")',
                            'button:has-text("確認")',
                            'button:has-text("認証")',
                            'button:has-text("次へ")',
                        ],
                        timeout=8,
                    )
                    if submit_selector:
                        log(f"验证码页已点击继续按钮: {submit_selector}")
                    else:
                        log("验证码输入后未见按钮，等待自动提交结果")
            try:
                captured_response = response_info.value
            except Exception:
                captured_response = None
        else:
            fill_result = _fill_otp_input(page, otp, log)
    except Exception as exc:
        # Keep the observer-based fallback below when no matching network
        # response is observed.  The input diagnostics remain authoritative.
        if not fill_result:
            fill_result = {"ok": False, "errors": [type(exc).__name__]}

    if not fill_result.get("ok"):
        return {
            "ok": False,
            "status": 0,
            "url": page.url,
            "data": None,
            "text": (
                "BROWSER_STATE_UNKNOWN: OTP input interaction failed "
                f"candidates={fill_result.get('candidates', 0)} "
                f"visible={fill_result.get('visible', 0)} "
                f"enabled={fill_result.get('enabled', 0)} "
                f"errors={','.join(fill_result.get('errors') or []) or '-'}"
            ),
        }

    if captured_response is None and not callable(expect_response):
        _browser_pause(page)
        submit_selector = _click_first(
            page,
            [
                'button[type="submit"]',
                'button[data-testid="continue-button"]',
                'button:has-text("Continue")',
                'button:has-text("continue")',
                'button:has-text("Verify")',
                'button:has-text("verify")',
                'button:has-text("Next")',
                'button:has-text("next")',
                'button:has-text("続ける")',
                'button:has-text("確認")',
                'button:has-text("認証")',
                'button:has-text("次へ")',
            ],
            timeout=8,
        )
        if submit_selector:
            log(f"验证码页已点击继续按钮: {submit_selector}")
        else:
            log("验证码输入后未见按钮，等待自动提交结果")

    if captured_response is not None:
        captured_status = int(getattr(captured_response, "status", 0) or 0)
        captured_headers = dict(getattr(captured_response, "headers", {}) or {})
        captured_location = str(captured_headers.get("location") or "")
        if captured_location:
            try:
                captured_location = urlparse(captured_location).path or captured_location[:160]
            except Exception:
                captured_location = captured_location[:160]
        if 300 <= captured_status < 400 or captured_location:
            return {
                "ok": True,
                "status": captured_status or 200,
                "url": page.url,
                "data": {"page_type": "", "continue_url": captured_location},
                "text": "",
            }

    def _pump_auth_events(milliseconds: int = 250) -> None:
        # Playwright response callbacks are dispatched while its dispatcher
        # is pumped.  ``time.sleep`` leaves a synchronous Playwright page
        # unable to deliver the 302 emitted by the single-field OTP form.
        try:
            page.wait_for_timeout(milliseconds)
        except Exception:
            time.sleep(max(milliseconds, 0) / 1000)

    deadline = time.time() + 20
    last_url = page.url
    while time.time() < deadline:
        _pump_auth_events(250)
        current_url = page.url
        last_url = current_url or last_url
        observed_failure = _observed_auth_failure(
            auth_observer,
            since=submit_started,
            current_url=current_url,
        )
        if observed_failure:
            return observed_failure
        observed = auth_observer.latest(since=submit_started) if auth_observer else None
        if observed:
            status = int(observed.get("http_status") or 0)
            page_type = str(observed.get("page_type") or "")
            continue_path = str(observed.get("continue_path") or "")
            if page_type in {
                "about_you",
                "consent",
                "workspace_selection",
                "organization_selection",
                "add_phone",
                "oauth_callback",
                "chatgpt_home",
            } or 300 <= status < 400:
                return {
                    "ok": True,
                    "status": status or 200,
                    "url": current_url,
                    "data": {
                        "page_type": page_type,
                        "continue_url": continue_path,
                    },
                    "text": "",
                }
        if "about-you" in current_url:
            return {"ok": True, "status": 200, "url": current_url, "data": None, "text": ""}
        if "add-phone" in current_url or "chatgpt.com" in current_url or "code=" in current_url:
            return {"ok": True, "status": 200, "url": current_url, "data": None, "text": ""}
        if "consent" in current_url or "workspace" in current_url or "organization" in current_url:
            return {"ok": True, "status": 200, "url": current_url, "data": None, "text": ""}
        try:
            error_text = page.locator("text=Invalid code").first.text_content(timeout=400)
        except Exception:
            error_text = ""
        if error_text:
            return {"ok": False, "status": 400, "url": current_url, "data": None, "text": error_text}
        _pump_auth_events(250)
    # One final dispatcher turn catches a redirect response that was emitted
    # during the last keypress but delivered after the page URL was sampled.
    _pump_auth_events(500)
    observed = auth_observer.latest(since=submit_started) if auth_observer else None
    if observed:
        status = int(observed.get("http_status") or 0)
        continue_path = str(observed.get("continue_path") or "")
        if 300 <= status < 400 or continue_path:
            return {
                "ok": True,
                "status": status or 200,
                "url": page.url,
                "data": {
                    "page_type": str(observed.get("page_type") or ""),
                    "continue_url": continue_path,
                },
                "text": "",
            }
    return {"ok": False, "status": 0, "url": last_url, "data": None, "text": "验证码页提交后未跳转"}


def _submit_about_you_via_page(
    page,
    log,
    auth_observer: AuthResponseObserver | None = None,
) -> dict:
    from .constants import generate_random_user_info

    user_info = generate_random_user_info()
    name = str(user_info.get("name") or "").strip()
    birthdate = str(user_info.get("birthdate") or "").strip()
    if not name or not birthdate:
        raise RuntimeError("about_you 数据生成失败")
    date_parts = birthdate.split("-")
    if len(date_parts) == 3:
        yyyy, mm, dd = date_parts
        us_birthdate = f"{mm}/{dd}/{yyyy}"
        cn_birthdate = f"{yyyy}/{mm}/{dd}"
    else:
        us_birthdate = birthdate
        cn_birthdate = birthdate.replace("-", "/")
    log("about_you 表单数据已生成")

    def _fill_locator(locator, value: str) -> bool:
        try:
            target = locator.first
            target.wait_for(state="visible", timeout=1500)
            target.click(timeout=1500)
            _browser_pause(page, headed=False)
            try:
                applied = bool(
                    target.evaluate(
                        """
                        (input, nextValue) => {
                          const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')?.set;
                          if (!setter) return false;
                          setter.call(input, nextValue);
                          input.dispatchEvent(new Event('input', { bubbles: true }));
                          input.dispatchEvent(new Event('change', { bubbles: true }));
                          return String(input.value || '') === String(nextValue || '');
                        }
                        """,
                        value,
                    )
                )
            except Exception:
                applied = False
            if not applied:
                target.fill("")
                target.type(value, delay=random.randint(25, 70))
            try:
                target.dispatch_event("blur")
            except Exception:
                pass
            final_val = str(target.input_value() or "").strip()
            return final_val == str(value).strip()
        except Exception:
            return False

    def _locator_from_visible_input_entry(entry: dict):
        try:
            visible_index = int(entry.get("visibleIndex"))
        except Exception:
            return None
        return page.locator("input:visible:not([type='hidden']):not([disabled]):not([readonly])").nth(visible_index)

    def _fill_visible_input_entry(entry: dict | None, value: str) -> bool:
        if not entry:
            return False
        locator = _locator_from_visible_input_entry(entry)
        if locator is None:
            return False
        return _fill_locator(locator, value)

    def _resolve_visible_input_selector(selectors: list[str]) -> str | None:
        for selector in selectors:
            try:
                locator = page.locator(selector).first
                locator.wait_for(state="visible", timeout=500)
                return selector
            except Exception:
                continue
        return None

    def _fill_second_visible_input(values: list[str], excluded_visible_indices: set[int] | None = None) -> bool:
        """兜底：about_you 卡片一般是 Full name + Birthday/Age 两个输入框。"""
        try:
            locator = page.locator(
                "input:visible:not([type='hidden']):not([disabled]):not([readonly])"
            )
            count = locator.count()
            if count < 2:
                return False
            excluded = {int(value) for value in (excluded_visible_indices or set())}
            target_index = None
            for idx in range(count):
                if idx not in excluded:
                    target_index = idx
                    if idx > 0:
                        break
            if target_index is None:
                return False
            target = locator.nth(target_index)
            target.click(timeout=1200)
            _browser_pause(page, headed=False)
            for value in values:
                try:
                    target.fill("")
                except Exception:
                    pass
                try:
                    target.type(str(value), delay=random.randint(18, 45))
                except Exception:
                    continue
                final_val = str(target.input_value() or "").strip()
                if final_val:
                    return True
            return False
        except Exception:
            return False

    def _has_visible(locator) -> bool:
        try:
            locator.first.wait_for(state="visible", timeout=700)
            return True
        except Exception:
            return False

    def _fill_birthday_selects(yyyy: str, mm: str, dd: str) -> bool:
        """处理 Month/Day/Year 下拉样式的生日控件。"""
        try:
            select_locator = page.locator("select:visible")
            count = select_locator.count()
            if count < 2:
                return False

            month_num = int(mm)
            day_num = int(dd)
            year_num = int(yyyy)
            month_short = time.strftime("%b", time.strptime(str(month_num), "%m"))
            month_full = time.strftime("%B", time.strptime(str(month_num), "%m"))

            assigned = {"month": False, "day": False, "year": False}

            for i in range(count):
                sel = select_locator.nth(i)
                try:
                    options = sel.locator("option")
                    option_count = options.count()
                except Exception:
                    option_count = 0
                if option_count <= 0:
                    continue

                texts: list[str] = []
                for idx in range(min(option_count, 80)):
                    try:
                        texts.append(str(options.nth(idx).inner_text(timeout=300) or "").strip())
                    except Exception:
                        continue
                joined = " ".join(texts).lower()

                try:
                    if (not assigned["month"]) and (
                        "january" in joined or "february" in joined or "march" in joined or "april" in joined
                    ):
                        for candidate in (month_full, month_short, str(month_num), f"{month_num:02d}"):
                            try:
                                sel.select_option(label=candidate, timeout=800)
                                assigned["month"] = True
                                break
                            except Exception:
                                try:
                                    sel.select_option(value=candidate, timeout=800)
                                    assigned["month"] = True
                                    break
                                except Exception:
                                    continue
                        continue

                    if (not assigned["year"]) and any(str(y) in joined for y in (year_num, year_num - 1, year_num + 1, 2026, 2025)):
                        for candidate in (str(year_num),):
                            try:
                                sel.select_option(label=candidate, timeout=800)
                                assigned["year"] = True
                                break
                            except Exception:
                                try:
                                    sel.select_option(value=candidate, timeout=800)
                                    assigned["year"] = True
                                    break
                                except Exception:
                                    continue
                        continue

                    if (not assigned["day"]) and any(str(x) in joined for x in (" 1 ", "2", "30", "31")):
                        for candidate in (str(day_num), f"{day_num:02d}"):
                            try:
                                sel.select_option(label=candidate, timeout=800)
                                assigned["day"] = True
                                break
                            except Exception:
                                try:
                                    sel.select_option(value=candidate, timeout=800)
                                    assigned["day"] = True
                                    break
                                except Exception:
                                    continue
                except Exception:
                    continue

            # 下拉顺序兜底：month/day/year
            if count >= 3:
                try:
                    if not assigned["month"]:
                        select_locator.nth(0).select_option(label=month_short, timeout=800)
                        assigned["month"] = True
                except Exception:
                    pass
                try:
                    if not assigned["day"]:
                        select_locator.nth(1).select_option(label=str(day_num), timeout=800)
                        assigned["day"] = True
                except Exception:
                    pass
                try:
                    if not assigned["year"]:
                        select_locator.nth(2).select_option(label=str(year_num), timeout=800)
                        assigned["year"] = True
                except Exception:
                    pass

            return assigned["month"] and assigned["day"] and assigned["year"]
        except Exception:
            return False

    visible_inputs = _collect_visible_text_inputs(page)
    if visible_inputs:
        log(
            "about_you 可见输入框: "
            + " | ".join(
                f"#{int(item.get('visibleIndex', 0))} {(_about_you_input_hints(item) or '-')[:80]}"
                for item in visible_inputs[:4]
            )
        )
    ordered_visible_entries = sorted(
        [item for item in visible_inputs if str(item.get("visibleIndex", "")).isdigit()],
        key=lambda item: int(item.get("visibleIndex", 0)),
    )
    name_entry = _pick_best_about_you_input(visible_inputs, "name")
    age_entry = _pick_best_about_you_input(
        visible_inputs,
        "age",
        exclude_visible_indices={int(name_entry.get("visibleIndex"))} if name_entry and str(name_entry.get("visibleIndex", "")).isdigit() else set(),
    )

    name_candidates = [
        page.get_by_label(re.compile(r"full\s*name", re.IGNORECASE)),
        page.get_by_label(re.compile(r"全名|姓名|氏名|お名前|フルネーム", re.IGNORECASE)),
        page.get_by_role("textbox", name=re.compile(r"full\s*name|name", re.IGNORECASE)),
        page.get_by_role("textbox", name=re.compile(r"全名|姓名|氏名|お名前|フルネーム", re.IGNORECASE)),
        page.locator("input[autocomplete='name']"),
        page.locator("input[name*='name' i]"),
        page.locator("input[id*='name' i]"),
        page.locator("input[name*='姓名']"),
        page.locator("input[id*='姓名']"),
        page.locator(
            "xpath=//*[contains(translate(normalize-space(string(.)),'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'full name')]/following::input[1]"
        ),
        page.locator("xpath=//*[contains(normalize-space(string(.)),'全名') or contains(normalize-space(string(.)),'姓名')]/following::input[1]"),
    ]
    birthday_candidates = [
        page.get_by_label(re.compile(r"birthday|date of birth|birth", re.IGNORECASE)),
        page.get_by_label(re.compile(r"生日|出生|生年月日|誕生日", re.IGNORECASE)),
        page.get_by_role("textbox", name=re.compile(r"birthday|date of birth|birth", re.IGNORECASE)),
        page.get_by_role("textbox", name=re.compile(r"生日|出生|生年月日|誕生日", re.IGNORECASE)),
        page.get_by_placeholder(re.compile(r"mm.?dd.?yyyy|yyyy.?mm.?dd|birthday|生日|生年月日|誕生日", re.IGNORECASE)),
        page.locator("input[name*='birth' i]"),
        page.locator("input[id*='birth' i]"),
        page.locator("input[placeholder*='MM' i]"),
        page.locator("input[placeholder*='DD' i]"),
        page.locator("input[placeholder*='YYYY' i]"),
        page.locator("input[placeholder*='年']"),
        page.locator("input[placeholder*='月']"),
        page.locator("input[placeholder*='日']"),
        page.locator("input[inputmode='numeric']"),
        page.locator(
            "xpath=//*[contains(translate(normalize-space(string(.)),'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'birthday')]/following::input[1]"
        ),
        page.locator("xpath=//*[contains(normalize-space(string(.)),'生日') or contains(normalize-space(string(.)),'出生')]/following::input[1]"),
        page.locator("input[type='date']"),
    ]

    age_years = None
    try:
        birth_year = int(str(birthdate).split("-")[0])
        current_year = int(time.strftime("%Y"))
        age_years = max(25, min(40, current_year - birth_year))
    except Exception:
        age_years = random.randint(25, 35)

    age_candidates = [
        page.get_by_label(re.compile(r"age", re.IGNORECASE)),
        page.get_by_label(re.compile(r"年龄|年齢", re.IGNORECASE)),
        page.get_by_role("textbox", name=re.compile(r"age", re.IGNORECASE)),
        page.get_by_role("textbox", name=re.compile(r"年龄|年齢", re.IGNORECASE)),
        page.locator("input[name*='age' i]"),
        page.locator("input[id*='age' i]"),
        page.locator("input[placeholder*='Age' i]"),
        page.locator("input[placeholder*='年龄']"),
        page.locator("input[placeholder*='年齢']"),
        page.locator(
            "xpath=//*[contains(translate(normalize-space(string(.)),'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'age')]/following::input[1]"
        ),
        page.locator("xpath=//*[contains(normalize-space(string(.)),'年龄')]/following::input[1]"),
    ]

    fill_result = {"name": False, "birthdate": False, "age": False, "month": False, "day": False, "year": False}
    if _fill_visible_input_entry(name_entry, name):
        fill_result["name"] = True
    if not fill_result.get("name"):
        for candidate in name_candidates:
            if _fill_locator(candidate, name):
                fill_result["name"] = True
                break
    mode_probe = {}
    try:
        mode_probe = page.evaluate(
            """
            () => {
              const labels = Array.from(document.querySelectorAll('label'))
                .map((n) => String(n.textContent || '').trim().toLowerCase())
                .filter(Boolean);
              const placeholders = Array.from(document.querySelectorAll('input'))
                .map((n) => String(n.placeholder || '').trim().toLowerCase())
                .filter(Boolean);
              const headings = Array.from(document.querySelectorAll('h1,h2,h3'))
                .map((n) => String(n.textContent || '').trim().toLowerCase())
                .filter(Boolean);
              const allText = labels.concat(placeholders).concat(headings);
              const hasAge = allText.some((t) => t === 'age' || t === 'edad' || t === 'âge' || t === 'alter' || t === 'idade' || t === '年齢' || t.includes('how old') || t.includes('年龄') || t.includes('年齢') || t.includes('나이'));
              const hasBirthday = allText.some((t) =>
                t.includes('birthday') || t.includes('date of birth') || t.includes('birth') || t.includes('生日') || t.includes('出生') || t.includes('生年月日') || t.includes('誕生日') || t.includes('fecha de nacimiento') || t.includes('nascimento') || t.includes('geburtstag') || t.includes('naissance')
              );
              return { labels, placeholders, headings, hasAge, hasBirthday };
            }
            """
        ) or {}
    except Exception:
        mode_probe = {}

    has_age_label = bool(mode_probe.get("hasAge"))
    has_birthday_label = bool(mode_probe.get("hasBirthday"))
    has_age_field = any(_has_visible(candidate) for candidate in age_candidates[:3])
    has_birthday_field = any(_has_visible(candidate) for candidate in birthday_candidates[:3])
    has_birthday_select = False
    try:
        has_birthday_select = page.locator("select:visible").count() >= 2
    except Exception:
        has_birthday_select = False
    if has_birthday_select:
        about_mode = "birthday_select"
    elif (has_age_label and not has_birthday_label) or (has_age_field and not has_birthday_field):
        about_mode = "age"
    else:
        about_mode = "birthday"
    log(f"about_you 页面模式: {about_mode} labels={mode_probe.get('labels', [])[:4]}")
    direct_name_selector = _resolve_visible_input_selector(
        [
            'input[name="name"]',
            'input[name="full_name"]',
            'input[autocomplete="name"]',
            'input[placeholder*="全名"]',
            'input[placeholder*="name" i]',
            'input[id*="name" i]:not([type="hidden"])',
        ]
    )
    direct_age_selector = _resolve_visible_input_selector(
        [
            'input[name="age"]',
            'input[placeholder="Age"]',
            'input[placeholder="age"]',
            'input[placeholder*="年龄"]',
            'input[id*="age" i]',
        ]
    )
    if about_mode == "age" and len(ordered_visible_entries) >= 2:
        name_entry = ordered_visible_entries[0]
        age_entry = ordered_visible_entries[1]
        log(
            f"about_you age 输入框映射: name=#{int(name_entry.get('visibleIndex', 0))}, "
            f"age=#{int(age_entry.get('visibleIndex', 0))}"
        )
    if about_mode == "age":
        log(
            "about_you age 直接定位: "
            f"name={direct_name_selector or '-'}, age={direct_age_selector or '-'}"
        )

    def _fill_segmented_date(mm: str, dd: str, yyyy: str) -> bool:
        """处理 MM / DD / YYYY 分段日期输入框（React DateField 样式）。
        特征：一个 Birthday label 下有多个小 input 或 div[data-type] 段。"""
        try:
            # 方式1: div[data-type] 段 (React Aria DateField)
            month_seg = page.locator('div[data-type="month"], input[data-type="month"]')
            day_seg = page.locator('div[data-type="day"], input[data-type="day"]')
            year_seg = page.locator('div[data-type="year"], input[data-type="year"]')
            if month_seg.count() > 0 and day_seg.count() > 0 and year_seg.count() > 0:
                return _fill_react_aria_date_segments(page, mm, dd, yyyy, log)

            # 方式2: 单个 date input 里有 MM/DD/YYYY 占位符
            # 点击输入框，然后按顺序输入 MM DD YYYY（Tab 切换段）
            date_input = page.locator("input[placeholder*='MM'], input[placeholder*='mm'], input[type='date']")
            if date_input.count() > 0:
                date_input.first.click(force=True)
                time.sleep(0.2)
                page.keyboard.type(mm, delay=50)
                page.keyboard.type(dd, delay=50)
                page.keyboard.type(yyyy, delay=50)
                return True

            # 方式3: Birthday label 下的第二个可见 input，直接点击后按数字键输入
            birthday_input = page.get_by_label(re.compile(r"birthday|birth", re.IGNORECASE))
            if birthday_input.count() > 0:
                birthday_input.first.click(force=True)
                time.sleep(0.2)
                page.keyboard.type(mm, delay=50)
                page.keyboard.type(dd, delay=50)
                page.keyboard.type(yyyy, delay=50)
                return True

            # 方式4: 第二个可见 input（name 是第一个）
            inputs = page.locator("input:visible:not([type='hidden']):not([disabled])")
            if inputs.count() >= 2:
                target = inputs.nth(1)
                target.click(force=True)
                time.sleep(0.3)
                # 先清空
                page.keyboard.press("Control+a")
                page.keyboard.press("Backspace")
                time.sleep(0.1)
                # 输入 MM，Tab 到 DD，Tab 到 YYYY
                page.keyboard.type(mm, delay=80)
                time.sleep(0.3)
                page.keyboard.type(dd, delay=80)
                time.sleep(0.3)
                page.keyboard.type(yyyy, delay=80)
                time.sleep(0.3)
                # 验证是否填入了正确的值
                val = str(target.input_value() or "").strip()
                if val and val != target.get_attribute("placeholder"):
                    return True
                # 如果直接输入不行，试 Tab 切换
                target.click(force=True)
                time.sleep(0.2)
                page.keyboard.press("Control+a")
                page.keyboard.press("Backspace")
                for i, part in enumerate([mm, dd, yyyy]):
                    page.keyboard.type(part, delay=80)
                    if i < 2:
                        page.keyboard.press("Tab")
                        time.sleep(0.2)
                return True
        except Exception:
            pass
        return False

    if about_mode == "birthday_select":
        if len(date_parts) == 3 and _fill_birthday_selects(yyyy, mm, dd):
            fill_result["month"] = True
            fill_result["day"] = True
            fill_result["year"] = True
            fill_result["birthdate"] = True
    elif about_mode == "age":
        if direct_name_selector and _fill_input_like_user(page, direct_name_selector, name):
            fill_result["name"] = True
        elif _fill_visible_input_entry(name_entry, name):
            fill_result["name"] = True
        if age_years is not None:
            if direct_age_selector and _fill_input_like_user(page, direct_age_selector, str(age_years)):
                fill_result["age"] = True
            elif _fill_visible_input_entry(age_entry, str(age_years)):
                fill_result["age"] = True
            if not fill_result.get("age") and len(ordered_visible_entries) < 2:
                for candidate in age_candidates:
                    if _fill_locator(candidate, str(age_years)):
                        fill_result["age"] = True
                        break
        # fallback: 直接找 placeholder="Age" 的输入框
        if not fill_result.get("age") and age_years is not None and len(ordered_visible_entries) < 2:
            try:
                age_input = page.locator("input[placeholder='Age'], input[placeholder='age']")
                if age_input.count() > 0:
                    age_input.first.click(force=True)
                    time.sleep(0.2)
                    age_input.first.fill("")
                    age_input.first.type(str(age_years), delay=random.randint(30, 60))
                    fill_result["age"] = True
            except Exception:
                pass
        if not fill_result.get("age") and age_years is not None:
            excluded_indices = set()
            if name_entry and str(name_entry.get("visibleIndex", "")).isdigit():
                excluded_indices.add(int(name_entry.get("visibleIndex")))
            if _fill_second_visible_input([str(age_years)], excluded_visible_indices=excluded_indices):
                fill_result["age"] = True
        if len(date_parts) == 3 and _sync_hidden_birthday_input(page, f"{yyyy}-{mm}-{dd}", log):
            fill_result["birthdate"] = True
    elif about_mode == "birthday" or about_mode == "birthday_text":
        # 先尝试分段日期输入（MM / DD / YYYY 格式的 DateField）
        if len(date_parts) == 3 and _fill_segmented_date(mm, dd, yyyy):
            fill_result["birthdate"] = True
            log("about_you 使用分段日期输入成功")
        # 再尝试普通文本输入
        if not fill_result.get("birthdate"):
            for candidate in birthday_candidates:
                if _fill_locator(candidate, cn_birthdate):
                    fill_result["birthdate"] = True
                    break
                if _fill_locator(candidate, us_birthdate):
                    fill_result["birthdate"] = True
                    break
                if _fill_locator(candidate, birthdate):
                    fill_result["birthdate"] = True
                    break
                if _fill_locator(candidate, cn_birthdate.replace("/", "")):
                    fill_result["birthdate"] = True
                    break
                if _fill_locator(candidate, us_birthdate.replace("/", "")):
                    fill_result["birthdate"] = True
                    break
        if not fill_result.get("birthdate"):
            fallback_values = [cn_birthdate, cn_birthdate.replace("/", " / "), cn_birthdate.replace("/", ""), us_birthdate, us_birthdate.replace("/", " / "), us_birthdate.replace("/", ""), birthdate]
            if _fill_second_visible_input(fallback_values):
                fill_result["birthdate"] = True

    log(f"about_you 填写结果: {fill_result}")
    if not fill_result.get("name"):
        raise RuntimeError("about_you 未成功填写 Full name")
    if not (
        fill_result.get("birthdate")
        or fill_result.get("age")
        or (fill_result.get("month") and fill_result.get("day") and fill_result.get("year"))
    ):
        raise RuntimeError("about_you 未成功填写 Birthday/Age")
    _browser_pause(page)

    submit_started = time.monotonic()
    submit_started = time.monotonic()
    submit_selector = _click_first(
        page,
        [
            'button:has-text("Finish creating account")',
            'button:has-text("finish creating account")',
            'button[type="submit"]',
            'button[data-testid="continue-button"]',
            'button:has-text("Continue")',
            'button:has-text("continue")',
            'button:has-text("Next")',
            'button:has-text("next")',
        ],
        timeout=8,
    )
    if not submit_selector:
        raise RuntimeError("about_you 未找到提交按钮")
    log(f"about_you 已点击继续按钮: {submit_selector}")

    deadline = time.time() + 20
    retried_generic_validation = False
    last_url = page.url
    while time.time() < deadline:
        current_url = page.url
        last_url = current_url or last_url
        observed_failure = _observed_auth_failure(
            auth_observer,
            since=submit_started,
            current_url=current_url,
        )
        if observed_failure:
            return observed_failure
        observed = auth_observer.latest(since=submit_started) if auth_observer else None
        if observed and str(observed.get("page_type") or "") in {
            "about_you",
            "consent",
            "workspace_selection",
            "organization_selection",
            "add_phone",
            "oauth_callback",
            "chatgpt_home",
        }:
            return {
                "ok": True,
                "status": int(observed.get("http_status") or 200),
                "url": current_url,
                "data": observed,
                "text": "",
            }
        observed_failure = _observed_auth_failure(
            auth_observer,
            since=submit_started,
            current_url=current_url,
        )
        if observed_failure:
            return observed_failure
        if "code=" in current_url or "chatgpt.com" in current_url:
            return {"ok": True, "status": 200, "url": current_url, "data": None, "text": ""}
        if "add-phone" in current_url:
            return {"ok": True, "status": 200, "url": current_url, "data": None, "text": ""}
        try:
            error_text = page.locator("text=Sorry, we cannot create your account").first.text_content(timeout=500)
        except Exception:
            error_text = ""
        if not error_text:
            try:
                error_text = page.locator("text=Enter a valid age to continue").first.text_content(timeout=300)
            except Exception:
                error_text = ""
        if not error_text:
            try:
                error_text = page.locator("text=doesn't look right").first.text_content(timeout=300)
            except Exception:
                error_text = ""
        if not error_text:
            try:
                error_text = page.locator("[role='alert']").first.text_content(timeout=300)
            except Exception:
                error_text = ""
        if not error_text:
            try:
                error_text = page.locator(".error, [class*='error'], [class*='Error']").first.text_content(timeout=300)
            except Exception:
                error_text = ""
        if error_text and "oai_log" not in error_text and "SSR_HTML" not in error_text:
            normalized_error = str(error_text).strip().lower()
            if (
                about_mode == "age"
                and not retried_generic_validation
                and ("doesn't look right" in normalized_error or "try again" in normalized_error)
            ):
                retried_generic_validation = True
                log("about_you age 模式提交被拒，重新同步 Full name/Age/hidden birthday 后重试一次...")
                if direct_name_selector and _fill_input_like_user(page, direct_name_selector, name):
                    fill_result["name"] = True
                elif _fill_visible_input_entry(name_entry, name):
                    fill_result["name"] = True
                elif len(ordered_visible_entries) < 2:
                    for candidate in name_candidates:
                        if _fill_locator(candidate, name):
                            fill_result["name"] = True
                            break
                if age_years is not None:
                    if direct_age_selector and _fill_input_like_user(page, direct_age_selector, str(age_years)):
                        fill_result["age"] = True
                    elif _fill_visible_input_entry(age_entry, str(age_years)):
                        fill_result["age"] = True
                    elif len(ordered_visible_entries) < 2:
                        for candidate in age_candidates:
                            if _fill_locator(candidate, str(age_years)):
                                fill_result["age"] = True
                                break
                if len(date_parts) == 3 and _sync_hidden_birthday_input(page, f"{yyyy}-{mm}-{dd}", log):
                    fill_result["birthdate"] = True
                _browser_pause(page)
                retry_submit_selector = _click_first(
                    page,
                    [
                        'button:has-text("Finish creating account")',
                        'button:has-text("finish creating account")',
                        'button[type="submit"]',
                        'button[data-testid="continue-button"]',
                        'button:has-text("Continue")',
                        'button:has-text("continue")',
                        'button:has-text("Next")',
                        'button:has-text("next")',
                    ],
                    timeout=5,
                )
                if retry_submit_selector:
                    log(f"about_you 重试提交按钮: {retry_submit_selector}")
                    time.sleep(0.5)
                    continue
            return {"ok": False, "status": 400, "url": current_url, "data": None, "text": error_text}
        time.sleep(0.5)
    return {"ok": False, "status": 0, "url": last_url, "data": None, "text": "about_you 提交后未跳转"}


def _emit_stage(stage_callback, stage: RegistrationStage, message: str, action: str = "enter") -> None:
    if callable(stage_callback):
        stage_callback(stage, message, action)


def _browser_registration_flow(
    page,
    email: str,
    password: str,
    otp_callback,
    log,
    *,
    stage_callback=None,
    challenge_guard: BrowserChallengeGuard | None = None,
    auth_observer: AuthResponseObserver | None = None,
) -> dict:
    device_id = str(uuid.uuid4())
    _seed_browser_device_id(page, device_id)
    state = None
    _emit_stage(stage_callback, RegistrationStage.EMAIL_SUBMIT, "提交注册邮箱")
    try:
        log("使用 ChatGPT NextAuth 注册入口启动浏览器注册")
        state = _start_browser_signup_via_authorize(page, email, device_id, log)
    except Exception as exc:
        log(f"ChatGPT NextAuth 注册入口失败: {exc}")
        challenge_resolved = bool(challenge_guard and challenge_guard.resolve(page))
        if challenge_resolved:
            log("挑战页已处理，重试 ChatGPT NextAuth 注册入口")
            state = _start_browser_signup_via_authorize(page, email, device_id, log)
        if state is None:
            # Navigation recovery stays inside this browser attempt and mode.
            recoverable_entry_error = _is_execution_context_error(exc) or any(
                token in str(exc).lower()
                for token in ("csrf", "authorize", "signin", "execution context")
            )
            if not recoverable_entry_error:
                raise
            log("回退到 OpenAI 页面注册入口…")
            try:
                state = _start_browser_signup_via_page(page, email, log)
            except Exception as page_exc:
                if challenge_guard and challenge_guard.resolve(page):
                    state = _start_browser_signup_via_page(page, email, log)
                else:
                    log(f"页面注册入口也失败: {page_exc}")
                    raise RuntimeError(
                        f"NextAuth 入口失败且页面入口失败: {exc}; {page_exc}"
                    ) from page_exc
    if not state:
        raise RuntimeError("注册入口未返回状态")
    auth_cookies = _get_cookies(page)
    log(
        "授权态 cookies: "
        f"login_session={'yes' if auth_cookies.get('login_session') else 'no'}, "
        f"oai-did={'yes' if auth_cookies.get('oai-did') else 'no'}"
    )
    log(f"注册状态起点: page={state.get('page_type') or '-'} url={(state.get('current_url') or '')[:100]}")
    register_submitted = False
    otp_trigger_emitted = False
    seen_states: dict[str, int] = {}

    for step in range(12):
        if challenge_guard and challenge_guard.resolve(page):
            state = _derive_registration_state_from_page(page)
        signature = "|".join(
            [
                str(state.get("page_type") or ""),
                str(state.get("method") or ""),
                str(state.get("continue_url") or ""),
                str(state.get("current_url") or ""),
            ]
        )
        seen_states[signature] = seen_states.get(signature, 0) + 1
        log(
            f"注册状态推进: step={step+1} page={state.get('page_type') or '-'} "
            f"next={str(state.get('continue_url') or '')[:60]} seen={seen_states[signature]}"
        )
        if seen_states[signature] > 2:
            raise RuntimeError(f"注册状态卡住: page={state.get('page_type') or '-'}")

        auth_page_error = _classify_authentication_error_page(page)
        if auth_page_error:
            raise RuntimeError(auth_page_error)

        if _is_registration_complete(state):
            _emit_stage(stage_callback, RegistrationStage.CALLBACK, "完成授权回调")
            _handle_post_signup_onboarding(page, log)
            return _extract_flow_state(None, page.url)

        if _is_password_registration(state):
            if register_submitted:
                raise RuntimeError("重复进入密码注册阶段")
            _emit_stage(stage_callback, RegistrationStage.OTP_TRIGGER, "提交密码并触发邮箱验证码")
            log("提交注册密码...")
            pre_cookies = _get_cookies(page)
            log(
                "密码阶段 cookies: "
                f"login_session={'yes' if pre_cookies.get('login_session') else 'no'}, "
                f"oai-client-auth-session={'yes' if pre_cookies.get('oai-client-auth-session') else 'no'}"
            )
            reg_resp = _submit_password_via_page(page, password, log, auth_observer)
            log(f"密码页提交状态: {reg_resp.get('status', 0)}")
            if not reg_resp.get("ok"):
                if challenge_guard and challenge_guard.resolve(page):
                    state = _derive_registration_state_from_page(page)
                    continue
                raise RuntimeError(f"密码页提交失败: {(reg_resp.get('text') or '')[:300]}")
            register_submitted = True
            otp_trigger_emitted = True
            state = _extract_flow_state(reg_resp.get("data"), reg_resp.get("url", page.url))
            if not state.get("page_type") or _is_password_registration(state):
                state = _derive_registration_state_from_page(page)
            continue

        if str(state.get("page_type") or "") == "login_password":
            if _recover_signup_password_page(page, log):
                state = _derive_registration_state_from_page(page)
                continue
            log("注册流程落到已有账号登录密码页，按登录流程继续认证...")
            login_resp = _submit_oauth_password_direct(page, password, log, auth_observer)
            log(f"登录密码页提交状态: {login_resp.get('status', 0)}")
            if not login_resp.get("ok"):
                if challenge_guard and challenge_guard.resolve(page):
                    state = _derive_registration_state_from_page(page)
                    continue
                raise RuntimeError(f"登录密码页提交失败: {(login_resp.get('text') or '')[:300]}")
            state = _extract_flow_state(login_resp.get("data"), login_resp.get("url", page.url))
            if not state.get("page_type"):
                state = _derive_registration_state_from_page(page)
            continue

        if _is_email_otp(state):
            if not otp_callback:
                raise RuntimeError("ChatGPT 注册需要邮箱验证码但未提供 otp_callback")
            if not otp_trigger_emitted:
                _emit_stage(
                    stage_callback,
                    RegistrationStage.OTP_TRIGGER,
                    "授权流程已触发邮箱验证码",
                )
                otp_trigger_emitted = True
            _emit_stage(stage_callback, RegistrationStage.OTP_WAIT, "等待 ChatGPT 验证码")
            log("等待 ChatGPT 验证码")
            code = otp_callback()
            if not code:
                raise RuntimeError("未获取到验证码")
            _emit_stage(stage_callback, RegistrationStage.OTP_SUBMIT, "提交邮箱验证码")
            otp_resp = _submit_otp_via_page(page, code, log, auth_observer)
            log(f"验证码页提交状态: {otp_resp.get('status', 0)}")
            if not otp_resp.get("ok"):
                if challenge_guard and challenge_guard.resolve(page):
                    state = _derive_registration_state_from_page(page)
                    continue
                raise RuntimeError(f"验证码校验失败: {(otp_resp.get('text') or '')[:300]}")
            state = _extract_flow_state(otp_resp.get("data"), otp_resp.get("url", page.url))
            if not state.get("page_type"):
                state = _derive_registration_state_from_page(page)
            continue

        if _is_about_you(state):
            _emit_stage(stage_callback, RegistrationStage.PROFILE_CREATE, "创建 ChatGPT 资料")
            log("提交 about_you 信息...")
            target_url = _normalize_url(
                str(state.get("current_url") or state.get("continue_url") or f"{OPENAI_AUTH}/about-you"),
                OPENAI_AUTH,
            )
            if "about-you" not in str(page.url):
                log(f"跳转到 about_you 页面: {target_url[:120]}")
                _goto_with_retry(page, target_url, wait_until="domcontentloaded", timeout=30000, log=log)
            about_resp = _submit_about_you_via_page(page, log, auth_observer)
            log(f"about_you 提交状态: {about_resp.get('status', 0)}")
            if not about_resp.get("ok"):
                if challenge_guard and challenge_guard.resolve(page):
                    state = _derive_registration_state_from_page(page)
                    continue
                raise RuntimeError(f"about_you 提交失败: {(about_resp.get('text') or '')[:300]}")
            state = _extract_flow_state(about_resp.get("data"), about_resp.get("url", page.url))
            if not state.get("page_type"):
                state = _derive_registration_state_from_page(page)
            continue

        if _requires_registration_navigation(state):
            target_url = _normalize_url(str(state.get("continue_url") or state.get("current_url") or ""), OPENAI_AUTH)
            if not target_url:
                raise RuntimeError("缺少可跟随的 continue_url")
            _goto_with_retry(page, target_url, wait_until="domcontentloaded", timeout=30000, log=log)
            state = _extract_flow_state(None, page.url)
            continue

        raise RuntimeError(f"未支持的注册状态: page={state.get('page_type') or '-'}")

    raise RuntimeError("注册状态机超出最大步数")


class ChatGPTBrowserRegister:
    def __init__(
        self,
        *,
        headless: bool,
        proxy: Optional[str] = None,
        otp_callback: Optional[Callable[[], str]] = None,
        log_fn: Callable[[str], None] = print,
        browser_profile: Optional[dict[str, Any]] = None,
        attempt_id: str = "",
        artifact_root: Optional[str] = None,
        turnstile_solver: Optional[Callable[[str, str], str]] = None,
        stage_callback: Optional[Callable[[RegistrationStage, str, str], None]] = None,
    ):
        self.headless = headless
        self.proxy = proxy
        self.otp_callback = otp_callback
        self.log = log_fn
        self.turnstile_solver = turnstile_solver
        self.stage_callback = stage_callback
        self.browser_profile = dict(browser_profile or {})
        self.attempt_id = str(attempt_id or "")
        self._engine = CamoufoxEngine(
            headless=bool(headless),
            proxy=proxy,
            profile=browser_profile,
            attempt_id=attempt_id,
            artifact_root=artifact_root,
        )

    def _open_browser(self):
        return self._engine.open()

    def run(self, email: str, password: str) -> dict:
        with self._open_browser() as browser:
            page = browser.new_page()
            self._engine.artifacts.observe(page)
            challenge_guard = BrowserChallengeGuard(
                solver=self.turnstile_solver,
                log=self.log,
                proxy=self.proxy,
                user_agent=str(self.browser_profile.get("user_agent") or ""),
                attempt_id=self.attempt_id,
            )
            challenge_guard.observe(page)
            auth_observer = AuthResponseObserver()
            auth_observer.observe(page)
            try:
                self.log("启动浏览器上下文注册状态机")
                final_state = _browser_registration_flow(
                    page,
                    email,
                    password,
                    self.otp_callback,
                    self.log,
                    stage_callback=self.stage_callback,
                    challenge_guard=challenge_guard,
                    auth_observer=auth_observer,
                )
                self.log(f"注册流程完成: page={final_state.get('page_type') or '-'}")

                # 获取 session token 和 cookies
                cookies_dict = _get_cookies(page)
                session_info = _fetch_chatgpt_session_from_page(page, cookies_dict, self.log)
                result = {
                    "email": email,
                    "password": password,
                    "account_id": session_info.get("account_id", ""),
                    "access_token": session_info.get("access_token", ""),
                    "refresh_token": session_info.get("refresh_token", ""),
                    "id_token": session_info.get("id_token", ""),
                    "session_token": session_info.get("session_token", ""),
                    "workspace_id": session_info.get("workspace_id", ""),
                    "cookies": session_info.get("cookies", "") or _cookies_to_header(cookies_dict),
                    "profile": session_info.get("profile", {}),
                    "expires_at": session_info.get("expires_at", ""),
                    "session": session_info.get("session", {}),
                    "registration_state": final_state,
                }
                return result
            except BaseException as exc:
                latest_auth = auth_observer.latest()
                if latest_auth:
                    self.log(
                        "OpenAI auth response "
                        f"status={latest_auth.get('http_status') or 0} "
                        f"error_code={latest_auth.get('error_code') or '-'} "
                        f"page_type={latest_auth.get('page_type') or '-'} "
                        f"flow_epoch={latest_auth.get('flow_epoch') or '-'}"
                    )
                evidence = self._engine.artifacts.capture(
                    page,
                    exc,
                    secrets=(email, password, self.proxy or ""),
                )
                self.log("Browser failure evidence captured")
                wrapped = RuntimeError(str(exc))
                wrapped.artifacts = evidence
                raise wrapped from exc

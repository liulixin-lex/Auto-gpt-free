"""CLIProxyAPI (CPA) / Sub2API remote account sync clients.

Official upload formats:
- CLIProxyAPI: POST /v0/management/auth-files
  Codex OAuth token JSON only: type=codex + access_token/refresh_token/...
  Does NOT support agentIdentity auth files for call routing.
- Sub2API: POST /api/v1/admin/accounts/data
  type=sub2api-data with accounts[].credentials.auth_mode=agentIdentity
  Agent identity can call without mailbox/OTP after identity registration.
"""
from __future__ import annotations

import json
import logging
from typing import Any
from urllib.parse import quote, urljoin

from curl_cffi import requests as cffi_requests

logger = logging.getLogger(__name__)


def _get_config(key: str, default: str = "") -> str:
    try:
        from core.config_store import config_store

        return str(config_store.get(key, default) or default).strip()
    except Exception:
        return default


def _truthy(value: str | bool | None) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _http(
    method: str,
    url: str,
    *,
    headers: dict | None = None,
    json_body: Any = None,
    data: bytes | None = None,
    timeout: int = 30,
) -> tuple[int, Any, str]:
    kwargs: dict[str, Any] = {
        "headers": headers or {},
        "proxies": None,
        "verify": False,
        "timeout": timeout,
        "impersonate": "chrome110",
    }
    if data is not None:
        kwargs["data"] = data
    elif json_body is not None:
        kwargs["json"] = json_body
    resp = cffi_requests.request(method.upper(), url, **kwargs)
    text = resp.text or ""
    parsed: Any = text
    try:
        parsed = resp.json()
    except Exception:
        pass
    return int(resp.status_code), parsed, text


# ---------------------------------------------------------------------------
# CLIProxyAPI / CPA
# ---------------------------------------------------------------------------


def cpa_base_url(override: str | None = None) -> str:
    return (override or _get_config("cpa_api_url")).rstrip("/")


def cpa_api_key(override: str | None = None) -> str:
    return override or _get_config("cpa_api_key")


def cpa_headers(api_key: str | None = None) -> dict[str, str]:
    key = cpa_api_key(api_key)
    return {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }


def test_cpa(api_url: str | None = None, api_key: str | None = None) -> tuple[bool, str]:
    base = cpa_base_url(api_url)
    key = cpa_api_key(api_key)
    if not base:
        return False, "CPA 地址未配置"
    if not key:
        return False, "CPA API Key 未配置"
    try:
        status, _, text = _http(
            "GET",
            f"{base}/v0/management/auth-files",
            headers=cpa_headers(key),
            timeout=15,
        )
        if status in (200, 204):
            return True, "CLIProxyAPI 连接成功"
        if status in (401, 403):
            return False, "连接成功，但 API Key 无效"
        return False, f"异常状态码 HTTP {status}: {text[:200]}"
    except Exception as exc:
        return False, f"连接失败: {exc}"


def upload_codex_token_to_cpa(
    token_json: dict,
    *,
    email: str,
    api_url: str | None = None,
    api_key: str | None = None,
) -> tuple[bool, str, str]:
    """Upload official Codex OAuth token JSON to CLIProxyAPI auth-files.

    CLIProxyAPI does NOT support agentIdentity. Required fields:
    type=codex, email, access_token, account_id; refresh_token strongly recommended.
    """
    base = cpa_base_url(api_url)
    key = cpa_api_key(api_key)
    if not base:
        return False, "CPA 地址未配置", ""
    if not key:
        return False, "CPA API Key 未配置", ""

    payload = dict(token_json or {})
    payload["type"] = "codex"
    if email and not payload.get("email"):
        payload["email"] = email
    if not str(payload.get("access_token") or "").strip():
        return False, "缺少 access_token", ""
    if not str(payload.get("account_id") or "").strip():
        return False, "缺少 account_id", ""

    filename = f"{(payload.get('email') or email or 'account').strip()}.json"
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    url = f"{base}/v0/management/auth-files?name={quote(filename)}"
    try:
        status, parsed, text = _http(
            "POST",
            url,
            headers=cpa_headers(key),
            data=body,
            timeout=45,
        )
        if status in (200, 201, 207):
            return True, "已上传 Codex OAuth 凭证到 CLIProxyAPI", filename
        msg = f"上传失败 HTTP {status}"
        if isinstance(parsed, dict):
            msg = str(parsed.get("message") or parsed.get("error") or msg)
        else:
            msg = f"{msg}: {text[:200]}"
        return False, msg, filename
    except Exception as exc:
        return False, f"上传异常: {exc}", filename


# Legacy name kept for imports; intentionally points to codex OAuth upload.
upload_agent_identity_to_cpa = upload_codex_token_to_cpa


def list_cpa_auth_files(
    *,
    api_url: str | None = None,
    api_key: str | None = None,
) -> tuple[bool, list[dict], str]:
    base = cpa_base_url(api_url)
    key = cpa_api_key(api_key)
    if not base or not key:
        return False, [], "CPA 未配置"
    try:
        status, parsed, text = _http(
            "GET",
            f"{base}/v0/management/auth-files",
            headers=cpa_headers(key),
            timeout=30,
        )
        if status != 200:
            return False, [], f"列表失败 HTTP {status}: {text[:200]}"
        files = []
        if isinstance(parsed, dict):
            files = list(parsed.get("files") or [])
        elif isinstance(parsed, list):
            files = parsed
        return True, files, "ok"
    except Exception as exc:
        return False, [], str(exc)


def delete_cpa_auth_file(
    name: str,
    *,
    api_url: str | None = None,
    api_key: str | None = None,
) -> tuple[bool, str]:
    base = cpa_base_url(api_url)
    key = cpa_api_key(api_key)
    if not base or not key:
        return False, "CPA 未配置"
    clean = (name or "").strip()
    if not clean:
        return False, "文件名为空"
    if not clean.endswith(".json"):
        clean = f"{clean}.json"
    try:
        status, parsed, text = _http(
            "DELETE",
            f"{base}/v0/management/auth-files?name={quote(clean)}",
            headers=cpa_headers(key),
            timeout=30,
        )
        if status in (200, 204):
            return True, f"已删除远程文件 {clean}"
        msg = f"删除失败 HTTP {status}"
        if isinstance(parsed, dict):
            msg = str(parsed.get("message") or parsed.get("error") or msg)
        else:
            msg = f"{msg}: {text[:160]}"
        return False, msg
    except Exception as exc:
        return False, str(exc)


# ---------------------------------------------------------------------------
# Sub2API
# ---------------------------------------------------------------------------


def sub2api_base_url(override: str | None = None) -> str:
    return (override or _get_config("sub2api_base_url")).rstrip("/")


def sub2api_token(override: str | None = None) -> str:
    return (override or _get_config("sub2api_token")).strip()


def sub2api_login(
    *,
    base_url: str | None = None,
    email: str | None = None,
    password: str | None = None,
) -> tuple[bool, str, str]:
    """Login admin and return (ok, token_or_msg, detail)."""
    base = sub2api_base_url(base_url)
    mail = (email if email is not None else _get_config("sub2api_email")).strip()
    pwd = password if password is not None else _get_config("sub2api_password")
    if not base:
        return False, "", "Sub2API 地址未配置"
    if not mail or not pwd:
        return False, "", "Sub2API 邮箱/密码未配置"
    url = urljoin(base + "/", "api/v1/auth/login")
    try:
        status, parsed, text = _http(
            "POST",
            url,
            headers={"Content-Type": "application/json"},
            json_body={"email": mail, "password": pwd},
            timeout=20,
        )
        if status not in (200, 201) or not isinstance(parsed, dict):
            return False, "", f"登录失败 HTTP {status}: {text[:200]}"
        # response shapes vary: {data:{access_token}} / {access_token} / {token}
        data = parsed.get("data") if isinstance(parsed.get("data"), dict) else parsed
        token = (
            data.get("access_token")
            or data.get("token")
            or data.get("accessToken")
            or parsed.get("access_token")
            or parsed.get("token")
            or ""
        )
        token = str(token or "").strip()
        if not token:
            return False, "", f"登录响应无 token: {text[:200]}"
        return True, token, "登录成功"
    except Exception as exc:
        return False, "", f"登录异常: {exc}"


def resolve_sub2api_token(
    *,
    base_url: str | None = None,
    token: str | None = None,
    email: str | None = None,
    password: str | None = None,
    persist: bool = True,
) -> tuple[bool, str, str]:
    direct = (token if token is not None else sub2api_token()).strip()
    if direct:
        return True, direct, "使用已保存 Token"
    ok, new_token, msg = sub2api_login(base_url=base_url, email=email, password=password)
    if ok and new_token and persist:
        try:
            from core.config_store import config_store

            config_store.set("sub2api_token", new_token)
        except Exception:
            pass
    return ok, new_token, msg


def sub2api_headers(token: str) -> dict[str, str]:
    """Admin auth headers. Sub2API accepts Bearer and/or X-API-Key."""
    tok = str(token or "").strip()
    return {
        "Authorization": f"Bearer {tok}",
        "X-API-Key": tok,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def test_sub2api(
    *,
    base_url: str | None = None,
    token: str | None = None,
    email: str | None = None,
    password: str | None = None,
) -> tuple[bool, str]:
    base = sub2api_base_url(base_url)
    if not base:
        return False, "Sub2API 地址未配置"
    ok, tok, msg = resolve_sub2api_token(
        base_url=base, token=token, email=email, password=password, persist=True
    )
    if not ok:
        return False, msg
    try:
        status, parsed, text = _http(
            "GET",
            f"{base}/api/v1/admin/accounts?page=1&page_size=1",
            headers=sub2api_headers(tok),
            timeout=20,
        )
        if status == 200:
            return True, "Sub2API 连接成功"
        if status in (401, 403):
            return False, "Token 无效或无管理员权限"
        return False, f"异常状态码 HTTP {status}: {text[:200]}"
    except Exception as exc:
        return False, f"连接失败: {exc}"


def _normalize_sub2api_data_payload(sub2api_payload: dict) -> dict:
    data = dict(sub2api_payload or {})
    data["type"] = "sub2api-data"
    data.setdefault("version", 1)
    data.setdefault("proxies", [])
    accounts = list(data.get("accounts") or [])
    identity = (
        data.get("agent_identity")
        if isinstance(data.get("agent_identity"), dict)
        else {}
    )
    export_mode = str(data.get("_export_mode") or "").strip().lower()
    top_auth = str(data.get("auth_mode") or "").strip()
    tokens = data.get("tokens") if isinstance(data.get("tokens"), dict) else {}
    prefer_agent = bool(identity) and export_mode != "codex_oauth"
    normalized_accounts = []
    for acc in accounts:
        if not isinstance(acc, dict):
            continue
        item = dict(acc)
        item["platform"] = item.get("platform") or "openai"
        item["type"] = item.get("type") or "oauth"
        creds = dict(item.get("credentials") or {})
        # Agent Identity path
        if prefer_agent or str(creds.get("auth_mode") or "").lower() == "agentidentity":
            if identity:
                creds.setdefault("auth_mode", "agentIdentity")
                for key in (
                    "agent_runtime_id",
                    "agent_private_key",
                    "task_id",
                    "account_id",
                    "chatgpt_account_id",
                    "chatgpt_user_id",
                    "chatgpt_account_is_fedramp",
                    "email",
                    "plan_type",
                    "workspace_id",
                ):
                    if key in identity and key not in creds:
                        creds[key] = identity[key]
                if "account_id" in identity:
                    creds.setdefault("chatgpt_account_id", identity["account_id"])
                    creds.setdefault("account_id", identity["account_id"])
            creds["auth_mode"] = "agentIdentity"
        else:
            # Codex OAuth fallback — keep tokens, do NOT force agentIdentity.
            creds.setdefault("auth_mode", top_auth or "oauth")
            if tokens.get("access_token") and not creds.get("access_token"):
                creds["access_token"] = tokens["access_token"]
            if tokens.get("refresh_token") and not creds.get("refresh_token"):
                creds["refresh_token"] = tokens["refresh_token"]
            if tokens.get("id_token") and not creds.get("id_token"):
                creds["id_token"] = tokens["id_token"]
            if str(creds.get("auth_mode") or "").lower() in {"", "oauth", "codex"}:
                creds["auth_mode"] = "oauth"
        item["credentials"] = creds
        item.setdefault("auto_pause_on_expired", True)
        item.setdefault("concurrency", item.get("concurrency") or 3)
        item.setdefault("priority", item.get("priority") or 50)
        normalized_accounts.append(item)
    data["accounts"] = normalized_accounts
    return data


def _sub2api_import_result_ok(parsed: Any, text: str, status: int) -> tuple[bool, str]:
    if status not in (200, 201):
        detail = text[:240]
        if isinstance(parsed, dict):
            detail = str(
                parsed.get("message")
                or parsed.get("error")
                or (parsed.get("data") or {}).get("message")
                or detail
            )
        return False, f"导入失败 HTTP {status}: {detail}"
    result = parsed.get("data") if isinstance(parsed, dict) and isinstance(parsed.get("data"), dict) else parsed
    if not isinstance(result, dict):
        return True, "已导入 Sub2API"
    created = int(
        result.get("account_created")
        or result.get("created")
        or 0
    )
    updated = int(result.get("updated") or 0)
    failed = int(result.get("account_failed") or result.get("failed") or 0)
    skipped = int(result.get("skipped") or 0)
    if failed and not created and not updated:
        return False, f"导入全部失败: {json.dumps(result, ensure_ascii=False)[:240]}"
    return True, f"已导入 Sub2API（新建 {created}，更新 {updated}，跳过 {skipped}，失败 {failed}）"


def upload_agent_identity_to_sub2api(
    sub2api_payload: dict,
    *,
    base_url: str | None = None,
    token: str | None = None,
) -> tuple[bool, str, list[int]]:
    """Import agent-identity payload into Sub2API.

    Primary: POST /api/v1/admin/accounts/data  (sub2api-data)
    Fallback: POST /api/v1/admin/accounts/import/codex-session
              with agentIdentity auth.json content (official codex import path)
    """
    base = sub2api_base_url(base_url)
    ok, tok, msg = resolve_sub2api_token(base_url=base, token=token, persist=True)
    if not base:
        return False, "Sub2API 地址未配置", []
    if not ok:
        return False, msg, []

    if "accounts" not in (sub2api_payload or {}):
        return False, "payload 缺少 accounts", []

    data = _normalize_sub2api_data_payload(sub2api_payload)
    if not data.get("accounts"):
        return False, "payload accounts 为空", []

    headers = sub2api_headers(tok)
    export_mode = str(data.get("_export_mode") or "").strip().lower()
    first_creds = (data["accounts"][0].get("credentials") or {}) if data["accounts"] else {}
    is_oauth_fallback = export_mode == "codex_oauth" or str(
        first_creds.get("auth_mode") or data.get("auth_mode") or ""
    ).lower() in {"oauth", "codex"}

    # Strip internal markers before upload
    data.pop("_export_mode", None)
    data.pop("_warning", None)

    # 1) Data import (backup/import path)
    try:
        status, parsed, text = _http(
            "POST",
            f"{base}/api/v1/admin/accounts/data",
            headers=headers,
            json_body={"data": data, "skip_default_group_bind": True},
            timeout=60,
        )
        ok_result, result_msg = _sub2api_import_result_ok(parsed, text, status)
        if ok_result:
            suffix = " [oauth]" if is_oauth_fallback else ""
            return True, f"{result_msg}{suffix}", []
        data_err = result_msg
    except Exception as exc:
        data_err = f"data 导入异常: {exc}"

    # 2) Codex session import — upstream freeAgentIdentity styles:
    #    - agentIdentity: contents=[{auth_mode, agent_identity}]
    #    - oauth fallback: content=Codex auth.json with tokens
    if is_oauth_fallback:
        tokens = data.get("tokens") if isinstance(data.get("tokens"), dict) else {}
        if not tokens.get("access_token"):
            tokens = {
                "access_token": first_creds.get("access_token"),
                "refresh_token": first_creds.get("refresh_token")
                or first_creds.get("refresh_token"),
                "id_token": first_creds.get("id_token"),
            }
            tokens = {k: v for k, v in tokens.items() if v}
        auth_json: dict[str, Any] = {
            "OPENAI_API_KEY": None,
            "tokens": tokens,
            "last_refresh": data.get("last_refresh")
            or (data["accounts"][0].get("extra") or {}).get("last_refresh"),
            "email": first_creds.get("email")
            or data.get("email")
            or (data["accounts"][0].get("extra") or {}).get("email"),
            "account_id": first_creds.get("account_id")
            or first_creds.get("chatgpt_account_id")
            or data.get("account_id"),
        }
        session_bodies = [
            {
                "content": json.dumps(auth_json, ensure_ascii=False),
                "update_existing": True,
                "skip_default_group_bind": True,
            },
            # Upstream-style multi-item import
            {
                "contents": [json.dumps(auth_json, ensure_ascii=False)],
                "update_existing": True,
            },
        ]
        tag = "codex-session-oauth"
    else:
        identity = (
            data.get("agent_identity")
            if isinstance(data.get("agent_identity"), dict)
            else {}
        )
        if not identity:
            identity = {
                "agent_runtime_id": first_creds.get("agent_runtime_id"),
                "agent_private_key": first_creds.get("agent_private_key"),
                "task_id": first_creds.get("task_id"),
                "account_id": first_creds.get("account_id")
                or first_creds.get("chatgpt_account_id"),
                "chatgpt_user_id": first_creds.get("chatgpt_user_id"),
                "email": first_creds.get("email"),
                "plan_type": first_creds.get("plan_type"),
                "chatgpt_account_is_fedramp": first_creds.get(
                    "chatgpt_account_is_fedramp", False
                ),
            }
        # Compact entry accepted by current Sub2API (upstream upload path)
        compact = {
            "auth_mode": "agentIdentity",
            "agent_identity": identity,
        }
        full_auth = {
            "auth_mode": "agentIdentity",
            "OPENAI_API_KEY": None,
            "agent_identity": identity,
        }
        session_bodies = [
            {
                "contents": [json.dumps(compact, ensure_ascii=False)],
                "update_existing": True,
            },
            {
                "content": json.dumps(full_auth, ensure_ascii=False),
                "update_existing": True,
                "skip_default_group_bind": True,
            },
        ]
        tag = "codex-session"

    last_session_err = ""
    for body in session_bodies:
        try:
            status, parsed, text = _http(
                "POST",
                f"{base}/api/v1/admin/accounts/import/codex-session",
                headers=headers,
                json_body=body,
                timeout=60,
            )
            ok_result, result_msg = _sub2api_import_result_ok(parsed, text, status)
            if ok_result:
                return True, f"{result_msg} [{tag}]", []
            last_session_err = result_msg
        except Exception as exc:
            last_session_err = str(exc)
    return False, f"{data_err}; codex-session: {last_session_err}", []


def list_sub2api_accounts(
    *,
    platform: str = "openai",
    search: str = "",
    page_size: int = 100,
    base_url: str | None = None,
    token: str | None = None,
) -> tuple[bool, list[dict], str]:
    base = sub2api_base_url(base_url)
    ok, tok, msg = resolve_sub2api_token(base_url=base, token=token, persist=True)
    if not base:
        return False, [], "Sub2API 地址未配置"
    if not ok:
        return False, [], msg
    accounts: list[dict] = []
    page = 1
    try:
        while page <= 50:
            qs = f"page={page}&page_size={page_size}"
            if platform:
                qs += f"&platform={quote(platform)}"
            if search:
                qs += f"&search={quote(search)}"
            status, parsed, text = _http(
                "GET",
                f"{base}/api/v1/admin/accounts?{qs}",
                headers=sub2api_headers(tok),
                timeout=30,
            )
            if status != 200:
                return False, accounts, f"列表失败 HTTP {status}: {text[:200]}"
            data = parsed.get("data") if isinstance(parsed, dict) else parsed
            items: list = []
            if isinstance(data, dict):
                items = list(data.get("items") or data.get("accounts") or data.get("list") or [])
            elif isinstance(data, list):
                items = data
            accounts.extend([x for x in items if isinstance(x, dict)])
            if len(items) < page_size:
                break
            page += 1
        return True, accounts, "ok"
    except Exception as exc:
        return False, accounts, str(exc)


def delete_sub2api_account(
    account_id: int | str,
    *,
    base_url: str | None = None,
    token: str | None = None,
) -> tuple[bool, str]:
    base = sub2api_base_url(base_url)
    ok, tok, msg = resolve_sub2api_token(base_url=base, token=token, persist=True)
    if not base:
        return False, "Sub2API 地址未配置"
    if not ok:
        return False, msg
    aid = str(account_id).strip()
    if not aid:
        return False, "account_id 为空"
    try:
        status, parsed, text = _http(
            "DELETE",
            f"{base}/api/v1/admin/accounts/{quote(aid)}",
            headers=sub2api_headers(tok),
            timeout=30,
        )
        if status in (200, 204):
            return True, f"已删除 Sub2API 账号 #{aid}"
        detail = text[:200]
        if isinstance(parsed, dict):
            detail = str(parsed.get("message") or parsed.get("error") or detail)
        return False, f"删除失败 HTTP {status}: {detail}"
    except Exception as exc:
        return False, str(exc)


def find_sub2api_account_ids_by_email(
    email: str,
    *,
    base_url: str | None = None,
    token: str | None = None,
) -> list[int]:
    mail = (email or "").strip().lower()
    if not mail:
        return []
    ok, items, _ = list_sub2api_accounts(search=mail, base_url=base_url, token=token)
    if not ok:
        return []
    found: list[int] = []
    for item in items:
        name = str(item.get("name") or "").strip().lower()
        extra = item.get("extra") if isinstance(item.get("extra"), dict) else {}
        creds = item.get("credentials") if isinstance(item.get("credentials"), dict) else {}
        candidates = {
            name,
            str(extra.get("email") or "").strip().lower(),
            str(creds.get("email") or "").strip().lower(),
        }
        if mail in candidates or any(mail in c for c in candidates if c):
            try:
                found.append(int(item.get("id")))
            except Exception:
                continue
    return found

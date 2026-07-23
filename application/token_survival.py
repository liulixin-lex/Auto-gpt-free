"""ChatGPT token survival: access refresh + password re-login (chatgpt2api-inspired).

Direct connection is fine; uses proxy_runtime / pool when available.
"""
from __future__ import annotations

import base64
import json
import logging
import time
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlencode

from sqlmodel import Session, select

from core.account_graph import load_account_graphs, patch_account_graph
from core.db import AccountModel, AccountOverviewModel, engine
from core.proxy_runtime import resolve_proxy_for_url, session_kwargs_for_url

logger = logging.getLogger(__name__)

AUTH_BASE = "https://auth.openai.com"
CHATGPT_APP = "https://chatgpt.com"


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _decode_jwt_payload(token: str) -> dict:
    try:
        parts = str(token or "").split(".")
        if len(parts) < 2:
            return {}
        payload = parts[1] + "=" * (-len(parts[1]) % 4)
        return json.loads(base64.urlsafe_b64decode(payload.encode("ascii")))
    except Exception:
        return {}


def _token_exp(access_token: str) -> int:
    claims = _decode_jwt_payload(access_token)
    try:
        return int(claims.get("exp") or 0)
    except Exception:
        return 0


def _needs_refresh(access_token: str, *, skew_seconds: int = 600) -> bool:
    exp = _token_exp(access_token)
    if not exp:
        return False
    return exp - int(time.time()) <= skew_seconds


def _session_for_auth(proxy: str | None = None):
    from curl_cffi import requests

    kwargs = session_kwargs_for_url(
        f"{AUTH_BASE}/",
        explicit_proxy=proxy,
        pool_proxy=None,
        impersonate="chrome136",
        timeout=45,
    )
    return requests.Session(**kwargs)


def try_refresh_access_token(
    refresh_token: str,
    *,
    proxy: str | None = None,
    client_id: str = "",
) -> dict[str, str]:
    """Refresh ChatGPT OAuth access token when refresh_token is present."""
    refresh_token = str(refresh_token or "").strip()
    if not refresh_token:
        raise RuntimeError("缺少 refresh_token")
    # Codex / ChatGPT web often use different clients; try platform client id first.
    cid = str(client_id or "").strip() or "app_2SKx67EdpoN0G6j64rFvigXD"
    session = _session_for_auth(proxy)
    try:
        resp = session.post(
            f"{AUTH_BASE}/oauth/token",
            data=urlencode(
                {
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                    "client_id": cid,
                    "redirect_uri": "https://platform.openai.com/auth/callback",
                }
            ),
            headers={
                "content-type": "application/x-www-form-urlencoded",
                "accept": "application/json",
                "origin": "https://platform.openai.com",
                "referer": "https://platform.openai.com/",
            },
        )
        data = {}
        try:
            data = resp.json() if hasattr(resp, "json") else {}
        except Exception:
            data = {}
        if int(getattr(resp, "status_code", 0) or 0) >= 400 or not data.get("access_token"):
            raise RuntimeError(
                f"refresh 失败 HTTP {getattr(resp, 'status_code', 0)}: "
                f"{str(data.get('error') or data.get('error_description') or getattr(resp, 'text', ''))[:200]}"
            )
        return {
            "access_token": str(data.get("access_token") or ""),
            "refresh_token": str(data.get("refresh_token") or refresh_token),
            "id_token": str(data.get("id_token") or ""),
            "expires_in": str(data.get("expires_in") or ""),
        }
    finally:
        try:
            session.close()
        except Exception:
            pass


def try_password_login(
    email: str,
    password: str,
    *,
    proxy: str | None = None,
) -> dict[str, str]:
    """Best-effort password session recovery via NextAuth CSRF + authorize.

    Not a full re-registration; aims to recover session cookies / access token
    for already-registered accounts (chatgpt2api re-login inspiration).
    """
    email = str(email or "").strip()
    password = str(password or "")
    if not email or not password:
        raise RuntimeError("密码重登需要邮箱和密码")
    session = _session_for_auth(proxy)
    try:
        session.get(f"{CHATGPT_APP}/", allow_redirects=True)
        csrf_resp = session.get(f"{CHATGPT_APP}/api/auth/csrf")
        csrf = {}
        try:
            csrf = csrf_resp.json() if hasattr(csrf_resp, "json") else {}
        except Exception:
            csrf = {}
        csrf_token = str(csrf.get("csrfToken") or "").strip()
        if not csrf_token:
            raise RuntimeError("密码重登 CSRF 获取失败")
        # Email/password login is fragile without full browser; attempt credentials
        # sign-in endpoint used by some ChatGPT flows.
        signin = session.post(
            f"{CHATGPT_APP}/api/auth/callback/credentials",
            data=urlencode(
                {
                    "csrfToken": csrf_token,
                    "email": email,
                    "password": password,
                    "redirect": "false",
                    "json": "true",
                    "callbackUrl": f"{CHATGPT_APP}/",
                }
            ),
            headers={
                "content-type": "application/x-www-form-urlencoded",
                "accept": "application/json",
                "origin": CHATGPT_APP,
                "referer": f"{CHATGPT_APP}/",
            },
            allow_redirects=True,
        )
        sess = session.get(f"{CHATGPT_APP}/api/auth/session")
        payload = {}
        try:
            payload = sess.json() if hasattr(sess, "json") else {}
        except Exception:
            payload = {}
        access = str(payload.get("accessToken") or "").strip()
        if not access:
            raise RuntimeError(
                f"密码重登未拿到 access_token (HTTP {getattr(signin, 'status_code', 0)}/"
                f"{getattr(sess, 'status_code', 0)})"
            )
        return {
            "access_token": access,
            "session_token": str(payload.get("sessionToken") or ""),
            "expires": str(payload.get("expires") or ""),
        }
    finally:
        try:
            session.close()
        except Exception:
            pass


def _account_auth_bits(session: Session, account_id: int) -> dict[str, Any]:
    from core.platform_accounts import build_platform_account

    model = session.get(AccountModel, account_id)
    if not model:
        return {}
    acc = build_platform_account(session, model)
    extra = dict(getattr(acc, "extra", None) or {})
    return {
        "model": model,
        "email": acc.email,
        "password": acc.password,
        "access_token": str(extra.get("access_token") or acc.token or ""),
        "refresh_token": str(extra.get("refresh_token") or ""),
        "id_token": str(extra.get("id_token") or ""),
        "client_id": str(extra.get("client_id") or extra.get("oauth_client_id") or ""),
        "extra": extra,
    }


def keepalive_chatgpt_accounts(
    *,
    limit: int = 40,
    log_fn=None,
    try_password: bool = True,
) -> dict[str, int]:
    """Refresh near-expiry tokens; optional password re-login on hard failure."""
    log = log_fn or logger.info
    results = {
        "checked": 0,
        "refreshed": 0,
        "relogin_ok": 0,
        "skipped": 0,
        "failed": 0,
    }
    with Session(engine) as session:
        overviews = session.exec(
            select(AccountOverviewModel)
            .where(AccountOverviewModel.lifecycle_status.in_(["registered", "trial", "subscribed"]))
            .limit(limit)
        ).all()
        account_ids = [int(o.account_id) for o in overviews]

    for aid in account_ids:
        results["checked"] += 1
        try:
            with Session(engine) as session:
                bits = _account_auth_bits(session, aid)
                if not bits:
                    results["skipped"] += 1
                    continue
                access = bits["access_token"]
                refresh = bits["refresh_token"]
                if not access and not refresh:
                    results["skipped"] += 1
                    continue
                if access and not _needs_refresh(access) and refresh:
                    # Soft keepalive path: only touch if expiring soon
                    results["skipped"] += 1
                    continue
                proxy = resolve_proxy_for_url(
                    f"{AUTH_BASE}/oauth/token",
                    explicit_proxy=None,
                    pool_proxy=None,
                )
                updated_extra = dict(bits["extra"])
                ok = False
                method = ""
                if refresh:
                    try:
                        tokens = try_refresh_access_token(
                            refresh,
                            proxy=proxy,
                            client_id=bits.get("client_id") or "",
                        )
                        updated_extra["access_token"] = tokens["access_token"]
                        if tokens.get("refresh_token"):
                            updated_extra["refresh_token"] = tokens["refresh_token"]
                        if tokens.get("id_token"):
                            updated_extra["id_token"] = tokens["id_token"]
                        ok = True
                        method = "refresh"
                        results["refreshed"] += 1
                    except Exception as exc:
                        log(f"  token refresh 失败 {bits['email']}: {exc}")
                if not ok and try_password and bits.get("password"):
                    try:
                        tokens = try_password_login(
                            bits["email"],
                            bits["password"],
                            proxy=proxy,
                        )
                        updated_extra["access_token"] = tokens["access_token"]
                        if tokens.get("session_token"):
                            updated_extra["session_token"] = tokens["session_token"]
                        ok = True
                        method = "password"
                        results["relogin_ok"] += 1
                    except Exception as exc:
                        log(f"  密码重登失败 {bits['email']}: {exc}")
                if not ok:
                    results["failed"] += 1
                    continue
                model = session.get(AccountModel, aid)
                if not model:
                    continue
                patch_account_graph(
                    session,
                    model,
                    summary_updates={
                        "token_keepalive_at": _utcnow_iso(),
                        "token_keepalive_method": method,
                        "valid": True,
                    },
                )
                try:
                    from core.db import AccountCredentialModel
                    from sqlmodel import select as sel

                    for key_name in ("access_token", "refresh_token", "id_token", "session_token"):
                        val = str(updated_extra.get(key_name) or "").strip()
                        if not val:
                            continue
                        row = session.exec(
                            sel(AccountCredentialModel)
                            .where(AccountCredentialModel.account_id == aid)
                            .where(AccountCredentialModel.scope == "platform")
                            .where(AccountCredentialModel.key == key_name)
                        ).first()
                        if row:
                            row.value = val
                            row.updated_at = datetime.now(timezone.utc)
                            session.add(row)
                        else:
                            session.add(
                                AccountCredentialModel(
                                    account_id=aid,
                                    scope="platform",
                                    provider_name=model.platform,
                                    credential_type="token",
                                    key=key_name,
                                    value=val,
                                    is_primary=key_name == "access_token",
                                    source="token_survival",
                                )
                            )
                except Exception as persist_exc:
                    log(f"  {bits['email']}: 凭证写入警告 {persist_exc}")
                model.updated_at = datetime.now(timezone.utc)
                session.add(model)
                session.commit()
                log(f"  {bits['email']}: keepalive ok via {method}")
        except Exception as exc:
            results["failed"] += 1
            log(f"  account_id={aid}: keepalive 异常 {exc}")
    log(
        f"Token 续命完成: checked={results['checked']} refreshed={results['refreshed']} "
        f"relogin={results['relogin_ok']} fail={results['failed']} skip={results['skipped']}"
    )
    return results

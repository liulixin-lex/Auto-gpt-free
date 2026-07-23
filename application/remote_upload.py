"""Upload usable credentials to CLIProxyAPI / Sub2API with format-correct payloads.

Official formats (2026):
- CLIProxyAPI auth-files: OAuth codex token JSON
  {type,email,access_token,refresh_token,id_token,account_id,expired,last_refresh}
  Runtime uses Metadata.access_token + refresh_token; NO agentIdentity support.
- Sub2API admin import:
  * POST /api/v1/admin/accounts/data  body {data: {type:sub2api-data, accounts:[...]}}
  * credentials.auth_mode=agentIdentity + agent_runtime_id/agent_private_key/...
  Runtime signs agent assertions; no mailbox/OTP needed after identity is registered.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from sqlmodel import Session

from application.account_exports import (
    _generate_cpa_token_json,
    _make_agent_identity_sub2api_json,
)
from core.account_graph import load_account_graphs, patch_account_graph
from core.config_store import config_store
from core.db import AccountModel, engine
from domain.accounts import AccountRecord
from infrastructure.accounts_repository import AccountsRepository
from platforms.chatgpt.cpa_upload import upload_to_cpa
from platforms.chatgpt.remote_sync import (
    delete_cpa_auth_file,
    delete_sub2api_account,
    find_sub2api_account_ids_by_email,
    upload_agent_identity_to_sub2api,
)

logger = logging.getLogger(__name__)


def _truthy(key: str, default: str = "false") -> bool:
    return str(config_store.get(key, default) or default).strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def sync_targets() -> list[str]:
    raw = str(config_store.get("sync_target", "none") or "none").strip().lower()
    if raw in {"both", "all"}:
        return ["cpa", "sub2api"]
    if raw in {"cpa", "cliproxyapi", "cliproxy"}:
        return ["cpa"]
    if raw in {"sub2api", "s2a"}:
        return ["sub2api"]
    return []


def is_auto_upload_enabled() -> bool:
    return _truthy("auto_upload_enabled") and bool(sync_targets())


def _record_from_id(account_id: int) -> AccountRecord | None:
    return AccountsRepository().get(int(account_id))


def _credential_map(record: AccountRecord) -> dict[str, str]:
    out: dict[str, str] = {}
    for item in record.credentials or []:
        if not isinstance(item, dict):
            continue
        key = str(item.get("key") or "").strip()
        val = str(item.get("value") or "").strip()
        if key and val:
            out[key] = val
    overview = record.overview or {}
    for key in (
        "access_token",
        "refresh_token",
        "id_token",
        "session_token",
        "account_id",
        "chatgpt_account_id",
    ):
        if key not in out and overview.get(key):
            out[key] = str(overview[key]).strip()
    return out


def validate_for_cpa(record: AccountRecord) -> tuple[bool, str, dict]:
    """Build and validate official CLIProxyAPI codex OAuth token JSON."""
    token = _generate_cpa_token_json(record)
    access = str(token.get("access_token") or "").strip()
    account_id = str(token.get("account_id") or "").strip()
    email = str(token.get("email") or "").strip()
    refresh = str(token.get("refresh_token") or "").strip()
    if not email:
        return False, "缺少 email，无法上传 CLIProxyAPI", {}
    if not access:
        return False, "缺少 access_token，CLIProxyAPI 无法调用", {}
    if not account_id:
        return False, "缺少 account_id，CLIProxyAPI 认证文件无效", {}
    if not refresh:
        # still uploadable but will die after access token expiry
        token["_warning"] = "缺少 refresh_token：access_token 过期后 CLIProxyAPI 无法自动续期"
    token["type"] = "codex"
    return True, "ok", token


def validate_for_sub2api(record: AccountRecord) -> tuple[bool, str, dict]:
    """Build Sub2API import payload: prefer Agent Identity, else Codex OAuth."""
    try:
        payload = _make_agent_identity_sub2api_json(record)
    except Exception as exc:
        return False, f"Sub2API 凭证构建失败: {exc}", {}

    if payload.get("type") != "sub2api-data":
        return False, "payload type 必须是 sub2api-data", {}
    accounts = payload.get("accounts") or []
    if not accounts:
        return False, "payload 缺少 accounts", {}
    creds = (accounts[0] or {}).get("credentials") or {}
    identity = payload.get("agent_identity") or {}
    auth_mode = str(
        creds.get("auth_mode") or payload.get("auth_mode") or ""
    ).strip()
    export_mode = str(payload.get("_export_mode") or "").strip()

    if auth_mode == "agentIdentity" or identity:
        required = {
            "agent_runtime_id": creds.get("agent_runtime_id")
            or identity.get("agent_runtime_id"),
            "agent_private_key": creds.get("agent_private_key")
            or identity.get("agent_private_key"),
            "account_id": creds.get("account_id")
            or creds.get("chatgpt_account_id")
            or identity.get("account_id"),
            "chatgpt_user_id": creds.get("chatgpt_user_id")
            or identity.get("chatgpt_user_id"),
        }
        missing = [k for k, v in required.items() if not str(v or "").strip()]
        if missing:
            return False, f"Agent Identity 缺少字段: {', '.join(missing)}", {}
        if not str(creds.get("task_id") or identity.get("task_id") or "").strip():
            existing = str(payload.get("_warning") or "").strip()
            note = "未包含 task_id，Sub2API 首次请求会自动注册 task"
            payload["_warning"] = f"{existing}; {note}" if existing else note
        return True, "ok", payload

    # Codex OAuth / upstream freeAgentIdentity Sub2API OAuth shape
    tokens = payload.get("tokens") if isinstance(payload.get("tokens"), dict) else {}
    access = str(
        creds.get("access_token") or tokens.get("access_token") or ""
    ).strip()
    if not access:
        return False, "OAuth 回退缺少 access_token", {}
    # Ensure credentials usable by Sub2API oauth import (upstream fields).
    if not creds.get("chatgpt_account_id") and (
        creds.get("account_id") or payload.get("account_id")
    ):
        creds["chatgpt_account_id"] = creds.get("account_id") or payload.get(
            "account_id"
        )
        accounts[0]["credentials"] = creds
        payload["accounts"] = accounts
    if export_mode != "codex_oauth":
        payload["_export_mode"] = "codex_oauth"
    if not payload.get("_warning"):
        payload["_warning"] = (
            "Agent Registry 不可用，已按上游 freeAgentIdentity 导出 Sub2API OAuth"
        )
    return True, "ok", payload


def ensure_local_usable(account_id: int) -> tuple[bool, str, AccountRecord | None]:
    """Reject invalid/expired local accounts before remote upload."""
    record = _record_from_id(account_id)
    if not record:
        return False, f"账号不存在: {account_id}", None
    lifecycle = str(record.lifecycle_status or "").lower()
    if lifecycle in {"invalid", "expired", "banned"}:
        return False, f"本地状态为 {lifecycle}，拒绝上传无效凭证", record
    validity = str(record.validity_status or "").lower()
    overview = record.overview or {}
    if validity == "invalid" or overview.get("valid") is False:
        return False, "本地探测为失效，拒绝上传", record
    creds = _credential_map(record)
    if not (
        creds.get("access_token")
        or creds.get("refresh_token")
        or creds.get("id_token")
        or creds.get("session_token")
    ):
        return False, "本地无可用 token 凭证", record
    return True, "ok", record


def _patch_remote_meta(account_id: int, updates: dict[str, Any]) -> None:
    with Session(engine) as session:
        model = session.get(AccountModel, int(account_id))
        if not model:
            return
        patch_account_graph(session, model, summary_updates=updates)
        session.add(model)
        session.commit()


def upload_account_to_remotes(
    account_id: int,
    *,
    targets: list[str] | None = None,
) -> dict[str, Any]:
    """Upload one local account with target-specific official formats."""
    targets = list(targets or sync_targets())
    results: dict[str, Any] = {
        "account_id": account_id,
        "targets": {},
        "formats": {
            "cpa": "codex-oauth-token",
            "sub2api": "agent-identity-sub2api-data",
        },
    }
    if not targets:
        results["ok"] = False
        results["error"] = "未选择同步目标"
        return results

    usable, reason, record = ensure_local_usable(account_id)
    if not usable or not record:
        results["ok"] = False
        results["error"] = reason
        return results

    email = record.email
    ok_any = False
    errors: list[str] = []
    warnings: list[str] = []
    meta: dict[str, Any] = {}

    if "cpa" in targets:
        ok_build, msg, token = validate_for_cpa(record)
        if not ok_build:
            results["targets"]["cpa"] = {"ok": False, "message": msg, "format": "codex-oauth-token"}
            errors.append(f"CLIProxyAPI: {msg}")
        else:
            if token.pop("_warning", None):
                warnings.append(f"CLIProxyAPI: {token.get('_warning', 'refresh_token 缺失')}")
            # strip non-official keys before upload
            clean = {
                k: token[k]
                for k in (
                    "access_token",
                    "account_id",
                    "email",
                    "expired",
                    "id_token",
                    "last_refresh",
                    "refresh_token",
                    "type",
                )
                if k in token
            }
            clean["type"] = "codex"
            ok, up_msg = upload_to_cpa(clean)
            remote_name = f"{email}.json"
            results["targets"]["cpa"] = {
                "ok": ok,
                "message": up_msg,
                "remote_name": remote_name,
                "format": "codex-oauth-token",
            }
            if ok:
                ok_any = True
                meta["remote_cpa_name"] = remote_name
                meta["remote_cpa_uploaded"] = True
                meta["remote_cpa_format"] = "codex-oauth-token"
            else:
                errors.append(f"CLIProxyAPI: {up_msg}")

    if "sub2api" in targets:
        ok_build, msg, payload = validate_for_sub2api(record)
        if not ok_build:
            results["targets"]["sub2api"] = {
                "ok": False,
                "message": msg,
                "format": "sub2api-credential",
            }
            errors.append(f"Sub2API: {msg}")
        else:
            warn = payload.get("_warning")
            if warn:
                warnings.append(f"Sub2API: {warn}")
            export_mode = str(payload.get("_export_mode") or "agent-identity")
            fmt = (
                "codex-oauth"
                if export_mode == "codex_oauth"
                else "agent-identity"
            )
            ok, up_msg, _ids = upload_agent_identity_to_sub2api(payload)
            results["targets"]["sub2api"] = {
                "ok": ok,
                "message": up_msg,
                "format": fmt,
            }
            if ok:
                ok_any = True
                meta["remote_sub2api_uploaded"] = True
                meta["remote_sub2api_format"] = fmt
                try:
                    found = find_sub2api_account_ids_by_email(email)
                    if found:
                        meta["remote_sub2api_id"] = found[0]
                        meta["remote_sub2api_ids"] = found
                except Exception:
                    pass
            else:
                errors.append(f"Sub2API: {up_msg}")

    if meta:
        meta["remote_sync_at"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        _patch_remote_meta(account_id, meta)

    results["ok"] = ok_any
    if warnings:
        results["warning"] = "; ".join(warnings)
    if errors and not ok_any:
        results["error"] = "; ".join(errors)
    elif errors:
        results["warning"] = "; ".join(
            [*(warnings or []), *errors]
        )
    return results


# Backward-compatible alias used by auto_ops / api
upload_account_agent_identity = upload_account_to_remotes


def delete_remote_for_account(account_id: int, *, email: str = "") -> dict[str, Any]:
    """Delete matching remote credentials for a local account on enabled targets."""
    targets = sync_targets()
    out: dict[str, Any] = {"account_id": account_id, "targets": {}}
    if not targets:
        out["ok"] = True
        out["skipped"] = True
        return out

    graph = {}
    with Session(engine) as session:
        graphs = load_account_graphs(session, [int(account_id)])
        graph = graphs.get(int(account_id), {})
        if not email:
            model = session.get(AccountModel, int(account_id))
            email = model.email if model else ""

    overview = graph.get("overview") or {}
    ok_any = False
    errors: list[str] = []

    if "cpa" in targets:
        candidates = []
        stored = str(overview.get("remote_cpa_name") or "").strip()
        if stored:
            candidates.append(stored)
        if email:
            candidates.append(f"{email}.json")
            # common CPA naming patterns
            candidates.append(f"codex-{email}.json")
            candidates.append(f"codex-{email}-plus.json")
        seen: set[str] = set()
        deleted = False
        last_msg = "未找到可删除文件"
        for name in candidates:
            if not name or name in seen:
                continue
            seen.add(name)
            ok, msg = delete_cpa_auth_file(name)
            last_msg = msg
            if ok:
                deleted = True
                out["targets"]["cpa"] = {"ok": True, "message": msg, "remote_name": name}
                ok_any = True
                break
        if not deleted:
            out["targets"]["cpa"] = {"ok": False, "message": last_msg}
            errors.append(last_msg)

    if "sub2api" in targets:
        ids: list[int] = []
        raw_id = overview.get("remote_sub2api_id")
        if raw_id:
            try:
                ids.append(int(raw_id))
            except Exception:
                pass
        for x in overview.get("remote_sub2api_ids") or []:
            try:
                ids.append(int(x))
            except Exception:
                pass
        # Always also search by email — stored id may be stale/wrong.
        if email:
            try:
                found = find_sub2api_account_ids_by_email(email)
                for fid in found:
                    if fid not in ids:
                        ids.append(fid)
            except Exception:
                pass
        if not ids:
            # Nothing on remote with this email → already clean.
            out["targets"]["sub2api"] = {
                "ok": True,
                "message": "远程无匹配账号（视为已清除）",
                "ids": [],
            }
            ok_any = True
        else:
            msgs = []
            all_ok = True
            for aid in sorted(set(ids)):
                ok, msg = delete_sub2api_account(aid)
                msgs.append(msg)
                if ok:
                    ok_any = True
                else:
                    # 404 / already gone counts as success
                    if "404" in str(msg) or "not found" in str(msg).lower():
                        ok_any = True
                    else:
                        all_ok = False
            out["targets"]["sub2api"] = {
                "ok": all_ok or ok_any,
                "message": "; ".join(msgs),
                "ids": ids,
            }
            if not (all_ok or ok_any):
                errors.append("; ".join(msgs))

    out["ok"] = ok_any or not errors
    if errors and not ok_any:
        out["error"] = "; ".join(errors)
    return out

"""Auto probe / delete invalid remotes / register replenish orchestration."""
from __future__ import annotations

import json
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Any

from sqlmodel import Session, select

from application.remote_upload import (
    delete_remote_for_account,
    is_auto_upload_enabled,
    sync_targets,
    upload_account_agent_identity,
)
from application.tasks import (
    TASK_STATUS_FAILED,
    TASK_STATUS_SUCCEEDED,
    TaskLogger,
    cancel_active_register_tasks,
    create_register_task,
    get_or_create_persistent_task,
    has_active_register_task,
    list_active_register_task_ids,
    prune_all_task_events,
    prune_task_events,
    _run_single_account_check,
)
from core.account_graph import load_account_graphs, patch_account_graph
from core.base_platform import AccountStatus
from core.config_store import config_store
from core.db import AccountModel, AccountOverviewModel, TaskModel, engine
from infrastructure.accounts_repository import AccountsRepository
from platforms.chatgpt.remote_sync import list_sub2api_accounts
from services.task_runtime import task_runtime

logger = logging.getLogger(__name__)

# Keep a rolling ring of recent auto-ops log lines for status API / Jobs header.
_RECENT_LOG_MAX = 80
_recent_logs: list[dict[str, Any]] = []
_recent_lock = threading.Lock()


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _truthy(key: str, default: str = "false") -> bool:
    return str(config_store.get(key, default) or default).strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _int_cfg(key: str, default: int) -> int:
    try:
        return int(str(config_store.get(key, str(default)) or default).strip())
    except Exception:
        return default


def _push_recent(message: str, *, level: str = "info") -> None:
    entry = {"at": _utcnow_iso(), "level": level, "message": message}
    with _recent_lock:
        _recent_logs.append(entry)
        if len(_recent_logs) > _RECENT_LOG_MAX:
            del _recent_logs[: len(_recent_logs) - _RECENT_LOG_MAX]


def get_recent_auto_ops_logs(limit: int = 40) -> list[dict[str, Any]]:
    limit = min(max(int(limit or 40), 1), _RECENT_LOG_MAX)
    with _recent_lock:
        return list(_recent_logs[-limit:])


def _interval_seconds() -> float:
    """Ops cycle interval. Prefer seconds config; fall back to minutes.

    Fast-drain mode for Sub2API: default 30s, min 10s.
    """
    if config_store.get("auto_ops_interval_seconds", ""):
        try:
            return float(max(int(str(config_store.get("auto_ops_interval_seconds") or "30")), 10))
        except Exception:
            pass
    minutes = max(_int_cfg("auto_probe_interval_minutes", 1), 0)
    if minutes <= 0:
        return 30.0
    # If user sets minutes >= 1, honor it, but allow sub-minute via seconds key.
    return float(minutes) * 60.0


def get_auto_ops_status() -> dict[str, Any]:
    interval_sec = int(_interval_seconds())
    interval_min_display = max(_int_cfg("auto_probe_interval_minutes", 1), 0)
    last_cycle = config_store.get("auto_ops_last_cycle_at", "")
    next_eta = ""
    if last_cycle and getattr(auto_ops_manager, "_running", False):
        try:
            from datetime import datetime as dt

            last = dt.fromisoformat(str(last_cycle).replace("Z", "+00:00"))
            next_ts = last.timestamp() + interval_sec
            next_eta = datetime.fromtimestamp(next_ts, tz=timezone.utc).isoformat().replace(
                "+00:00", "Z"
            )
        except Exception:
            next_eta = ""
    return {
        "auto_upload_enabled": is_auto_upload_enabled(),
        "sync_target": config_store.get("sync_target", "none"),
        "auto_probe_enabled": _truthy("auto_probe_enabled"),
        "auto_probe_interval_minutes": interval_min_display,
        "auto_ops_interval_seconds": interval_sec,
        "auto_delete_remote_enabled": _truthy("auto_delete_remote_enabled"),
        "auto_register_enabled": _truthy("auto_register_enabled"),
        "auto_replenish_enabled": _truthy("auto_replenish_enabled"),
        "auto_replenish_target": _int_cfg("auto_replenish_target", 10),
        "auto_register_count": _int_cfg("auto_register_count", 5),
        "auto_register_concurrency": _int_cfg("auto_register_concurrency", 5),
        "auto_register_executor": config_store.get("auto_register_executor", "")
        or config_store.get("default_executor", "protocol"),
        "auto_sync_delete_enabled": _truthy("auto_sync_delete_enabled", "true"),
        "last_probe_at": config_store.get("auto_ops_last_probe_at", ""),
        "last_probe_result": config_store.get("auto_ops_last_probe_result", ""),
        "last_replenish_at": config_store.get("auto_ops_last_replenish_at", ""),
        "last_cycle_at": last_cycle,
        "last_cycle_summary": config_store.get("auto_ops_last_cycle_summary", ""),
        "next_cycle_at": next_eta,
        "running": bool(getattr(auto_ops_manager, "_running", False)),
        "cycle_in_progress": bool(getattr(auto_ops_manager, "_cycle_in_progress", False)),
        "register_task_active": has_active_register_task(),
        "active_register_task_ids": list_active_register_task_ids(),
        "last_register_error_class": config_store.get("auto_ops_last_register_error_class", ""),
        "register_fail_streak": _int_cfg("auto_ops_register_fail_streak", 0),
        "recent_logs": get_recent_auto_ops_logs(30),
        "register_metrics": _safe_register_metrics(),
    }


def _safe_register_metrics() -> dict[str, Any]:
    try:
        from application.register_metrics import get_register_metrics

        return get_register_metrics(limit=8)
    except Exception:
        return {}


def stop_auto_probe(*, cancel_registers: bool = False) -> dict[str, Any]:
    """Turn off automatic probe (and optionally stop register tasks)."""
    config_store.set("auto_probe_enabled", "false")
    _push_recent("用户手动停止自动探测", level="warning")
    out: dict[str, Any] = {
        "ok": True,
        "auto_probe_enabled": False,
        "register": None,
    }
    if cancel_registers:
        out["register"] = cancel_active_register_tasks(reason="用户停止自动探测时一并取消注册")
    return out


def start_auto_probe() -> dict[str, Any]:
    """Re-enable automatic probe cycles."""
    config_store.set("auto_probe_enabled", "true")
    _push_recent("用户手动开启自动探测")
    try:
        auto_ops_manager.kick()
    except Exception:
        pass
    return {"ok": True, "auto_probe_enabled": True}


def stop_auto_replenish(*, cancel_running: bool = True) -> dict[str, Any]:
    """Turn off auto register/replenish and cancel in-flight register tasks."""
    config_store.set("auto_register_enabled", "false")
    config_store.set("auto_replenish_enabled", "false")
    _push_recent("用户手动停止自动补号/注册", level="warning")
    register = None
    if cancel_running:
        register = cancel_active_register_tasks(reason="用户手动停止注册任务")
        _push_recent(
            f"已请求取消注册任务 cancelled={register.get('cancelled', 0)}",
            level="warning",
        )
    return {
        "ok": True,
        "auto_register_enabled": False,
        "auto_replenish_enabled": False,
        "register": register,
    }


def start_auto_replenish() -> dict[str, Any]:
    """Re-enable auto register + replenish and clear fail-streak so a cycle can start soon."""
    config_store.set("auto_register_enabled", "true")
    config_store.set("auto_replenish_enabled", "true")
    config_store.set("auto_ops_register_fail_streak", "0")
    _push_recent("用户手动开启自动补号/注册")
    try:
        auto_ops_manager.kick()
    except Exception:
        pass
    return {
        "ok": True,
        "auto_register_enabled": True,
        "auto_replenish_enabled": True,
    }


def stop_all_auto_ops(*, cancel_registers: bool = True) -> dict[str, Any]:
    """Emergency stop: probe + delete + replenish off, cancel register tasks."""
    config_store.set("auto_probe_enabled", "false")
    config_store.set("auto_delete_remote_enabled", "false")
    config_store.set("auto_register_enabled", "false")
    config_store.set("auto_replenish_enabled", "false")
    _push_recent("用户手动停止全部自动运维", level="warning")
    register = None
    if cancel_registers:
        register = cancel_active_register_tasks(reason="用户停止全部自动运维")
    return {
        "ok": True,
        "auto_probe_enabled": False,
        "auto_delete_remote_enabled": False,
        "auto_register_enabled": False,
        "auto_replenish_enabled": False,
        "register": register,
    }


def count_active_accounts(platform: str = "chatgpt") -> int:
    active = {"registered", "trial", "subscribed"}
    with Session(engine) as session:
        rows = session.exec(
            select(AccountOverviewModel).where(
                AccountOverviewModel.lifecycle_status.in_(list(active))
            )
        ).all()
        if not rows:
            return 0
        ids = [int(r.account_id) for r in rows]
        models = session.exec(
            select(AccountModel).where(AccountModel.id.in_(ids))
        ).all()
        return sum(1 for m in models if m.platform == platform)


def mark_account_invalid(account_id: int) -> None:
    with Session(engine) as session:
        model = session.get(AccountModel, int(account_id))
        if not model:
            return
        model.updated_at = datetime.now(timezone.utc)
        patch_account_graph(
            session,
            model,
            lifecycle_status=AccountStatus.INVALID.value,
            summary_updates={"valid": False, "checked_at": _utcnow_iso()},
        )
        session.add(model)
        session.commit()


def purge_account_everywhere(
    account_id: int,
    *,
    email: str = "",
    log_fn=None,
) -> dict[str, Any]:
    """Delete remote (Sub2API/CPA) then local. Treat 'remote not found' as OK."""
    log = log_fn or (lambda m, **_k: None)
    out: dict[str, Any] = {
        "account_id": account_id,
        "remote_ok": False,
        "local_ok": False,
    }
    mail = email
    if not mail:
        with Session(engine) as session:
            model = session.get(AccountModel, int(account_id))
            mail = (model.email if model else "") or ""

    if sync_targets() and _truthy("auto_delete_remote_enabled"):
        try:
            remote = delete_remote_for_account(int(account_id), email=mail)
            # missing remote is fine — nothing left to delete
            msg = str(remote.get("error") or "")
            targets = remote.get("targets") or {}
            sub = targets.get("sub2api") if isinstance(targets, dict) else None
            sub_msg = str((sub or {}).get("message") or "")
            not_found = (
                "未找到" in msg
                or "未找到" in sub_msg
                or remote.get("skipped")
            )
            out["remote"] = remote
            out["remote_ok"] = bool(remote.get("ok") or not_found)
            if out["remote_ok"]:
                log(f"⚡ 远程清除 {mail or account_id}: {sub_msg or msg or 'ok'}")
            else:
                log(
                    f"远程清除失败 {mail or account_id}: {msg or remote}",
                    level="warning",
                )
        except Exception as exc:
            out["remote_error"] = str(exc)
            log(f"远程清除异常 {mail or account_id}: {exc}", level="error")
    else:
        out["remote_ok"] = True
        out["remote_skipped"] = True

    # Always drop local invalid rows when auto-delete is on.
    if _truthy("auto_delete_remote_enabled") or _truthy(
        "auto_delete_local_invalid", "true"
    ):
        try:
            out["local_ok"] = bool(AccountsRepository().delete(int(account_id)))
            if out["local_ok"]:
                log(f"⚡ 本地已删 {mail or account_id}")
            else:
                log(f"本地删除跳过（不存在） {mail or account_id}")
                out["local_ok"] = True  # already gone
        except Exception as exc:
            out["local_error"] = str(exc)
            log(f"本地删除失败 {mail or account_id}: {exc}", level="warning")
    else:
        out["local_ok"] = True
        out["local_skipped"] = True

    out["ok"] = bool(out.get("remote_ok") and out.get("local_ok"))
    return out


def sweep_stale_invalid_accounts(*, log_fn=None, limit: int = 50) -> dict[str, Any]:
    """Retry-purge accounts already marked invalid/expired (delete may have failed)."""
    log = log_fn or (lambda m, **_k: None)
    if not (
        _truthy("auto_delete_remote_enabled")
        or _truthy("auto_delete_local_invalid", "true")
    ):
        return {"skipped": True}

    stale = {"invalid", "expired", "banned"}
    with Session(engine) as session:
        models = session.exec(
            select(AccountModel)
            .where(AccountModel.platform == "chatgpt")
            .order_by(AccountModel.id.desc())
        ).all()
        graphs = load_account_graphs(session, [int(m.id) for m in models if m.id])

    victims = [
        m
        for m in models
        if graphs.get(int(m.id or 0), {}).get("lifecycle_status") in stale
    ]
    if limit > 0:
        victims = victims[: int(limit)]

    if not victims:
        return {"total": 0, "purged": 0, "failed": 0}

    quiet = lambda m, **_k: None  # noqa: E731
    log(f"清扫失效号 · 待处理 {len(victims)}")
    purged = 0
    failed = 0
    for m in victims:
        aid = int(m.id or 0)
        res = purge_account_everywhere(aid, email=m.email or "", log_fn=quiet)
        if res.get("ok"):
            purged += 1
        else:
            failed += 1
            log(f"清扫失败 {m.email or aid}", level="warning")
    log(f"清扫完成 · 已清 {purged} · 失败 {failed}")
    return {"total": len(victims), "purged": purged, "failed": failed}


def run_probe_cycle(
    *,
    platform: str = "chatgpt",
    limit: int = 0,
    log_fn=None,
    also_delete_local: bool = False,
) -> dict[str, Any]:
    """Probe active accounts; mark invalid; optionally delete remotes (and local)."""
    log = log_fn or (lambda m, **_k: None)
    active = {"registered", "trial", "subscribed"}
    with Session(engine) as session:
        q = select(AccountModel).where(AccountModel.platform == platform)
        models = session.exec(q.order_by(AccountModel.id.desc())).all()
        graphs = load_account_graphs(session, [int(m.id) for m in models if m.id])

    targets = [
        m
        for m in models
        if graphs.get(int(m.id or 0), {}).get("lifecycle_status") in active
    ]
    if limit and limit > 0:
        targets = targets[: int(limit)]

    results = {
        "total": len(targets),
        "valid": 0,
        "invalid": 0,
        "error": 0,
        "remote_deleted": 0,
        "remote_delete_failed": 0,
        "local_deleted": 0,
    }
    invalid_ids: list[int] = []

    total = len(targets)
    log(f"探测开始 · {total} 个有效号")
    # Parallel probe — Sub2API drain needs fast invalid detection.
    probe_workers = min(max(total, 1), 8)
    # Progress cadence: every 10% or every 25 accounts, whichever is finer.
    progress_step = max(5, min(25, max(total // 10, 1))) if total else 1
    done_count = 0
    # Quiet purge logging: only surface failures + compact invalid line.
    quiet_log = lambda m, **_k: None  # noqa: E731

    def _probe_one(model: AccountModel) -> tuple[int, str, str, Exception | None]:
        aid = int(model.id or 0)
        email = model.email or str(aid)
        try:
            valid, _ = _run_single_account_check(aid)
            return aid, email, "valid" if valid else "invalid", None
        except Exception as exc:
            return aid, email, "error", exc

    delete_on_invalid = (
        _truthy("auto_delete_remote_enabled")
        or also_delete_local
        or _truthy("auto_delete_local_invalid", "true")
    )

    if targets:
        with ThreadPoolExecutor(max_workers=probe_workers) as pool:
            futures = [pool.submit(_probe_one, m) for m in targets]
            for fut in as_completed(futures):
                aid, email, state, exc = fut.result()
                done_count += 1
                if state == "valid":
                    results["valid"] += 1
                elif state == "invalid":
                    results["invalid"] += 1
                    invalid_ids.append(aid)
                    # One line per invalid (not 3+ purge lines).
                    if delete_on_invalid:
                        res = purge_account_everywhere(
                            aid, email=email, log_fn=quiet_log
                        )
                        if res.get("remote_ok"):
                            results["remote_deleted"] += 1
                        else:
                            results["remote_delete_failed"] += 1
                        if res.get("local_ok"):
                            results["local_deleted"] += 1
                        else:
                            mark_account_invalid(aid)
                        remote_tag = "远程+本地" if res.get("remote_ok") else "本地"
                        if res.get("ok"):
                            log(f"✗ 失效已清 {email} ({remote_tag})", level="warning")
                        else:
                            log(
                                f"✗ 失效 {email} 清除失败 remote={res.get('remote_ok')} local={res.get('local_ok')}",
                                level="error",
                            )
                    else:
                        mark_account_invalid(aid)
                        log(f"✗ 失效 {email}", level="warning")
                else:
                    results["error"] += 1
                    logger.warning("probe error account=%s: %s", aid, exc)
                    log(f"! 异常 {email}: {exc}", level="error")
                # Milestone progress only (not per-valid spam).
                if done_count == total or done_count % progress_step == 0:
                    log(
                        f"探测进度 {done_count}/{total} · "
                        f"有效 {results['valid']} · 失效 {results['invalid']} · 异常 {results['error']}"
                    )

    # Retry anything stuck in invalid/expired (previous delete failures).
    if delete_on_invalid:
        try:
            sweep = sweep_stale_invalid_accounts(log_fn=log, limit=80)
            results["stale_sweep"] = sweep
            results["local_deleted"] += int(sweep.get("purged") or 0)
        except Exception as exc:
            log(f"失效号清扫异常: {exc}", level="warning")

    config_store.set("auto_ops_last_probe_at", _utcnow_iso())
    summary_line = (
        f"有效 {results['valid']} · 失效 {results['invalid']} · "
        f"异常 {results['error']} · 远程删 {results['remote_deleted']} · "
        f"本地删 {results['local_deleted']}"
    )
    config_store.set("auto_ops_last_probe_result", summary_line)
    log(f"探测完成 · {summary_line}")
    return results


def sync_remote_orphans(*, log_fn=None) -> dict[str, Any]:
    """If remote Sub2API has accounts that local already deleted/invalid, clean them.

    Also: if remote account missing while local still active with remote_sub2api_id,
    clear local remote meta (remote was manually deleted).
    """
    log = log_fn or (lambda m, **_k: None)
    if "sub2api" not in sync_targets():
        return {"skipped": True, "reason": "sub2api_not_target"}
    if not _truthy("auto_sync_delete_enabled", "true"):
        return {"skipped": True, "reason": "sync_delete_disabled"}

    ok, remote_items, msg = list_sub2api_accounts(platform="openai")
    if not ok:
        log(f"拉取 Sub2API 列表失败: {msg}", level="warning")
        return {"ok": False, "error": msg}

    remote_by_email: dict[str, list[int]] = {}
    remote_ids: set[int] = set()
    for item in remote_items:
        try:
            rid = int(item.get("id"))
        except Exception:
            continue
        remote_ids.add(rid)
        name = str(item.get("name") or "").strip().lower()
        extra = item.get("extra") if isinstance(item.get("extra"), dict) else {}
        creds = item.get("credentials") if isinstance(item.get("credentials"), dict) else {}
        for cand in {
            name,
            str(extra.get("email") or "").strip().lower(),
            str(creds.get("email") or "").strip().lower(),
        }:
            if cand and "@" in cand:
                remote_by_email.setdefault(cand, []).append(rid)

    with Session(engine) as session:
        models = session.exec(
            select(AccountModel).where(AccountModel.platform == "chatgpt")
        ).all()
        graphs = load_account_graphs(session, [int(m.id) for m in models if m.id])

    local_emails_active: set[str] = set()
    local_all_emails: set[str] = set()
    local_remote_ids: set[int] = set()
    invalid_local_emails: set[str] = set()
    for m in models:
        email = str(m.email or "").strip().lower()
        if email:
            local_all_emails.add(email)
        g = graphs.get(int(m.id or 0), {})
        life = str(g.get("lifecycle_status") or "").lower()
        overview = g.get("overview") or {}
        rid = overview.get("remote_sub2api_id")
        if rid:
            try:
                local_remote_ids.add(int(rid))
            except Exception:
                pass
        for x in overview.get("remote_sub2api_ids") or []:
            try:
                local_remote_ids.add(int(x))
            except Exception:
                pass
        if life in {"registered", "trial", "subscribed"}:
            if email:
                local_emails_active.add(email)
        elif life in {"invalid", "expired", "banned"}:
            if email:
                invalid_local_emails.add(email)

    # Remote exists but local invalid / missing → delete remote
    from platforms.chatgpt.remote_sync import delete_sub2api_account

    deleted_remote = 0
    failed = 0
    for email, ids in remote_by_email.items():
        if email in local_emails_active:
            continue
        # delete if local invalid or no local account at all
        if email in invalid_local_emails or email not in local_all_emails:
            for rid in sorted(set(ids)):
                ok_del, dmsg = delete_sub2api_account(rid)
                if ok_del:
                    deleted_remote += 1
                else:
                    failed += 1
                    log(f"同步删远程失败 {email} #{rid}: {dmsg}", level="warning")

    # Local has remote_id that no longer exists on Sub2API → clear meta (remote manually deleted)
    cleared = 0
    for m in models:
        g = graphs.get(int(m.id or 0), {})
        overview = g.get("overview") or {}
        life = str(g.get("lifecycle_status") or "").lower()
        if life not in {"registered", "trial", "subscribed"}:
            continue
        rid = overview.get("remote_sub2api_id")
        try:
            rid_i = int(rid) if rid is not None else 0
        except Exception:
            rid_i = 0
        if rid_i and rid_i not in remote_ids:
            with Session(engine) as session:
                model = session.get(AccountModel, int(m.id or 0))
                if model:
                    patch_account_graph(
                        session,
                        model,
                        summary_updates={
                            "remote_sub2api_id": None,
                            "remote_sub2api_ids": [],
                            "remote_sub2api_uploaded": False,
                            "remote_missing_at": _utcnow_iso(),
                        },
                    )
                    session.add(model)
                    session.commit()
                    cleared += 1

    out = {
        "ok": True,
        "remote_total": len(remote_items),
        "deleted_remote": deleted_remote,
        "failed": failed,
        "cleared_local_meta": cleared,
    }
    if deleted_remote or cleared or failed:
        log(
            f"双向同步 · 远程 {out['remote_total']} · 删孤儿 {deleted_remote} · "
            f"清映射 {cleared} · 失败 {failed}"
        )
    else:
        log(f"双向同步 · 远程 {out['remote_total']} · 无变更")
    return out


# After a zero-success register batch, wait before spawning another (stops task spam).
# Keep short enough for fast recovery once proxy/mailbox is fixed.
_REPLENISH_FAIL_COOLDOWN_BASE_SEC = 60
_REPLENISH_FAIL_COOLDOWN_MAX_SEC = 300
_REPLENISH_MIN_GAP_SEC = 20


def _parse_iso_ts(value: str) -> float:
    raw = str(value or "").strip()
    if not raw:
        return 0.0
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
    except Exception:
        return 0.0


def _recent_register_zero_success() -> bool:
    """True if the latest finished auto-ops register batch had 0 successes."""
    with Session(engine) as session:
        rows = session.exec(
            select(TaskModel)
            .where(TaskModel.type == "register")
            .where(TaskModel.status.in_([TASK_STATUS_FAILED, TASK_STATUS_SUCCEEDED]))
            .order_by(TaskModel.created_at.desc())
            .limit(8)
        ).all()
    for row in rows:
        try:
            payload = row.get_payload() if hasattr(row, "get_payload") else {}
            extra = dict((payload or {}).get("extra") or {})
            if extra.get("triggered_by") != "auto_ops":
                continue
            if row.success_count and int(row.success_count) > 0:
                return False
            result = row.get_result() if hasattr(row, "get_result") else {}
            data = (result or {}).get("data") or {}
            if int(data.get("success") or 0) > 0:
                return False
            return True
        except Exception:
            continue
    return False


def _replenish_blocked_by_cooldown(log_fn=None) -> dict[str, Any] | None:
    """Rate-limit auto register spawns so failed batches cannot flood the job list."""
    log = log_fn or (lambda m, **_k: None)
    now = time.time()
    last_at = _parse_iso_ts(config_store.get("auto_ops_last_replenish_at", ""))
    if last_at and now - last_at < _REPLENISH_MIN_GAP_SEC:
        wait = int(_REPLENISH_MIN_GAP_SEC - (now - last_at))
        log(f"跳过补号：距上次启动不足 {_REPLENISH_MIN_GAP_SEC}s（剩余 {wait}s）")
        return {"skipped": True, "reason": "min_gap", "wait_seconds": wait}

    streak = max(_int_cfg("auto_ops_register_fail_streak", 0), 0)
    if streak <= 0 and not _recent_register_zero_success():
        return None

    if streak <= 0 and _recent_register_zero_success():
        streak = max(streak, 1)
        config_store.set("auto_ops_register_fail_streak", str(streak))

    cooldown = min(
        _REPLENISH_FAIL_COOLDOWN_MAX_SEC,
        _REPLENISH_FAIL_COOLDOWN_BASE_SEC * (2 ** max(streak - 1, 0)),
    )
    if last_at and now - last_at < cooldown:
        wait = int(cooldown - (now - last_at))
        log(
            f"跳过补号：连续注册失败退避中 streak={streak} "
            f"cooldown={cooldown}s 剩余 {wait}s（配置代理或修好注册后再自动恢复）"
        )
        return {
            "skipped": True,
            "reason": "fail_cooldown",
            "streak": streak,
            "cooldown_seconds": cooldown,
            "wait_seconds": wait,
        }
    return None


def maybe_start_replenish_register(log_fn=None) -> dict[str, Any] | None:
    """Start a register task when pool is below target and auto register/replenish enabled."""
    log = log_fn or (lambda m, **_k: None)
    if not (_truthy("auto_register_enabled") or _truthy("auto_replenish_enabled")):
        return None

    if has_active_register_task():
        log("跳过补号：已有注册任务在排队/执行中")
        return {
            "skipped": True,
            "reason": "register_active",
        }

    blocked = _replenish_blocked_by_cooldown(log_fn=log)
    if blocked:
        return blocked

    target = max(_int_cfg("auto_replenish_target", 10), 0)
    active = count_active_accounts("chatgpt")
    if target <= 0:
        return None
    if active >= target:
        log(f"号池充足 active={active} target={target}，跳过补号")
        return {
            "skipped": True,
            "reason": "pool_full",
            "active": active,
            "target": target,
        }

    deficit = target - active
    batch = max(_int_cfg("auto_register_count", 5), 1)
    count = min(deficit, batch, 20)
    workers = max(_int_cfg("auto_register_concurrency", 5), 1)
    workers = min(workers, count, 20)
    executor = (
        config_store.get("auto_register_executor", "")
        or config_store.get("default_executor", "protocol")
        or "protocol"
    )
    identity = config_store.get("default_identity_provider", "mailbox") or "mailbox"
    # Per-account fail-fast budget (seconds). Drop hung mailbox and free the worker.
    account_timeout = max(_int_cfg("auto_register_account_timeout_seconds", 180), 45)
    account_timeout = min(account_timeout, 600)
    otp_timeout = max(_int_cfg("auto_register_otp_timeout_seconds", 90), 30)
    otp_timeout = min(otp_timeout, max(account_timeout - 15, 30))
    payload = {
        "count": count,
        "concurrency": workers,
        "executor_type": executor,
        "captcha_solver": "auto",
        "account_timeout_seconds": account_timeout,
        "extra": {
            "identity_provider": identity,
            "auto_download_agent_identity": False,
            "triggered_by": "auto_ops",
            # Ensure upload path is on for replenish batches (settings still gate).
            "auto_upload_sub2api_agent_identity": True,
            "account_timeout_seconds": account_timeout,
            "otp_timeout": otp_timeout,
        },
    }
    task = create_register_task(payload)
    task_runtime.wake_up()
    config_store.set("auto_ops_last_replenish_at", _utcnow_iso())
    log(
        f"启动补号注册任务 task={task.get('id')} count={count} "
        f"concurrency={workers} active={active} target={target} executor={executor}"
    )
    return {
        "started": True,
        "task_id": task.get("id"),
        "count": count,
        "concurrency": workers,
        "active": active,
        "target": target,
    }


def run_auto_ops_cycle(*, as_task: bool = True) -> dict[str, Any]:
    """One full ops cycle; logs append into a single long-lived auto_ops task."""
    task_logger: TaskLogger | None = None
    task_id = ""
    if as_task:
        # One window forever — do NOT create a new task per cycle.
        task = get_or_create_persistent_task(
            task_type="auto_ops",
            platform="chatgpt",
            session_key="persistent_task_auto_ops",
            payload={"session": "auto_ops", "rolling": True},
        )
        task_id = str(task.get("id") or "")
        task_logger = TaskLogger(task_id)

    def log(msg: str, level: str = "info", **_kwargs) -> None:
        _push_recent(msg, level=level)
        if task_logger:
            task_logger.log(msg, level=level)
        else:
            print(f"[auto-ops] {msg}")

    summary: dict[str, Any] = {"at": _utcnow_iso(), "task_id": task_id}
    try:
        log(
            "── 运维周期开始 ── "
            f"probe={_truthy('auto_probe_enabled')} "
            f"delete_remote={_truthy('auto_delete_remote_enabled')} "
            f"replenish={_truthy('auto_replenish_enabled') or _truthy('auto_register_enabled')} "
            f"targets={','.join(sync_targets()) or 'none'}"
        )

        # Replenish first when short, so there is no probe-then-wait gap.
        started_register = False
        if _truthy("auto_register_enabled") or _truthy("auto_replenish_enabled"):
            summary["replenish"] = maybe_start_replenish_register(log_fn=log)
            started_register = bool((summary.get("replenish") or {}).get("started"))
        else:
            summary["replenish"] = {"skipped": True}

        if _truthy("auto_probe_enabled"):
            summary["probe"] = run_probe_cycle(platform="chatgpt", log_fn=log)
        else:
            summary["probe"] = {"skipped": True}
            log("跳过探测（未开启）")
            # Still purge stuck invalid rows even when probe is off.
            if _truthy("auto_delete_remote_enabled"):
                summary["stale_sweep"] = sweep_stale_invalid_accounts(log_fn=log)

        try:
            summary["remote_sync"] = sync_remote_orphans(log_fn=log)
        except Exception as exc:
            summary["remote_sync"] = {"ok": False, "error": str(exc)}
            log(f"双向同步异常: {exc}", level="error")

        # After purge / remote sync, top up only if this cycle did not already start one.
        if _truthy("auto_register_enabled") or _truthy("auto_replenish_enabled"):
            if started_register or has_active_register_task():
                log("周期末尾跳过补号：本周期已启动或仍有注册任务")
                summary["replenish_after"] = {"skipped": True, "reason": "already_started"}
            else:
                log("周期末尾立即补号检查（消除空档）")
                summary["replenish_after"] = maybe_start_replenish_register(log_fn=log)

        config_store.set("auto_ops_last_cycle_at", summary["at"])
        config_store.set(
            "auto_ops_last_cycle_summary",
            json.dumps(summary, ensure_ascii=False, default=str)[:800],
        )
        log("── 运维周期完成 ──")
        # Rolling log retention: keep auto_ops window bounded; housekeep others.
        # Silent by default — only system-log large cleanups (avoid UI spam).
        try:
            if task_id:
                n = prune_task_events(task_id, task_type="auto_ops")
                if n >= 50:
                    logger.info("auto_ops log rotated task=%s deleted=%s", task_id, n)
            stats = prune_all_task_events()
            if stats.get("events_deleted") or stats.get("global_deleted"):
                logger.info("task event housekeep %s", stats)
        except Exception as prune_exc:
            logger.warning("task event prune failed: %s", prune_exc)
        if task_logger:
            # Keep the session task RUNNING so Jobs shows one continuous window.
            task_logger.set_result_data(summary)
    except Exception as exc:
        logger.exception("auto ops cycle failed")
        log(f"运维周期失败: {exc}", level="error")
        summary["error"] = str(exc)
        # Do not finish/kill the session task — next cycle continues logging.
        raise
    return summary


class AutoOpsManager:
    """Background loop: continuous probe + remote cleanup + auto replenish."""

    def __init__(self):
        self._running = False
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._last_probe_ts = 0.0
        self._cycle_in_progress = False

    def start(self):
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(
            target=self._loop, daemon=True, name="auto-ops-manager"
        )
        self._thread.start()
        print("[AutoOpsManager] 已启动")

    def stop(self):
        self._running = False

    def kick(self) -> dict[str, Any]:
        """Run one cycle immediately (manual)."""
        with self._lock:
            self._cycle_in_progress = True
            try:
                out = run_auto_ops_cycle(as_task=True)
                self._last_probe_ts = time.time()
                return out
            finally:
                self._cycle_in_progress = False

    def _loop(self):
        # Fast first cycle for Sub2API drain scenarios.
        time.sleep(5)
        while self._running:
            try:
                enabled_any = (
                    _truthy("auto_probe_enabled")
                    or _truthy("auto_register_enabled")
                    or _truthy("auto_replenish_enabled")
                    or _truthy("auto_delete_remote_enabled")
                )
                now = time.time()
                interval = _interval_seconds()
                if enabled_any and (now - self._last_probe_ts >= interval):
                    with self._lock:
                        self._cycle_in_progress = True
                        try:
                            print(
                                f"[AutoOpsManager] 执行自动运维周期 "
                                f"(interval={int(interval)}s)..."
                            )
                            _push_recent(
                                f"后台调度触发运维周期 interval={int(interval)}s"
                            )
                            run_auto_ops_cycle(as_task=True)
                            self._last_probe_ts = time.time()
                        finally:
                            self._cycle_in_progress = False
            except Exception as exc:
                print(f"[AutoOpsManager] 错误: {exc}")
                _push_recent(f"运维错误: {exc}", level="error")
            # re-check switches / interval every 2s for fast response
            for _ in range(2):
                if not self._running:
                    break
                time.sleep(1)


auto_ops_manager = AutoOpsManager()


def upload_after_register(account_ids: list[int], log_fn=None) -> list[dict[str, Any]]:
    """Best-effort auto upload for newly registered accounts.

    Formats:
    - CLIProxyAPI ← official codex OAuth token JSON
    - Sub2API ← agent_identity sub2api-data (no mailbox needed for later calls)
    Only uploads when local credentials validate as usable.
    Skips accounts already marked as uploaded to all enabled targets.
    """
    log = log_fn or (lambda m: None)
    if not is_auto_upload_enabled():
        return []
    targets = set(sync_targets())
    outs = []
    for aid in account_ids:
        try:
            # Skip if already uploaded to every configured target.
            with Session(engine) as session:
                graphs = load_account_graphs(session, [int(aid)])
                overview = (graphs.get(int(aid), {}) or {}).get("overview") or {}
            already = True
            if "cpa" in targets and not overview.get("remote_cpa_uploaded"):
                already = False
            if "sub2api" in targets and not overview.get("remote_sub2api_uploaded"):
                already = False
            if targets and already:
                outs.append(
                    {
                        "account_id": aid,
                        "ok": True,
                        "skipped": True,
                        "reason": "already_uploaded",
                    }
                )
                log(f"自动上传跳过 account_id={aid}（已推送）")
                continue

            result = upload_account_agent_identity(int(aid))
            outs.append(result)
            if result.get("ok"):
                parts = []
                for name, info in (result.get("targets") or {}).items():
                    if isinstance(info, dict) and info.get("ok"):
                        parts.append(f"{name}:{info.get('format') or 'ok'}")
                log(f"自动上传成功 account_id={aid} ({', '.join(parts) or 'ok'})")
                if result.get("warning"):
                    log(f"自动上传警告 account_id={aid}: {result['warning']}")
            else:
                log(f"自动上传失败 account_id={aid}: {result.get('error')}")
        except Exception as exc:
            outs.append({"account_id": aid, "ok": False, "error": str(exc)})
            log(f"自动上传异常 account_id={aid}: {exc}")
    return outs

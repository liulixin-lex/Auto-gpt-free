"""Panel access authentication (CLIProxyAPI-style management password).

Rules:
- First boot with no password → setup required (init UI).
- After password is set → every /api call needs a session token.
- ``APP_PASSWORD`` / ``MANAGEMENT_PASSWORD`` env acts as bootstrap password
  (plain text, not written to DB) — same idea as CLIProxyAPI MANAGEMENT_PASSWORD.
- UI-set password is stored as salted PBKDF2 hash in SQLite ``configs``.
- Login returns a random session token (not the password itself).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from typing import Any

from core.config_store import config_store

HASH_KEY = "access_password_hash"
SALT_KEY = "access_password_salt"
SESSIONS_KEY = "access_auth_sessions"
SETUP_DONE_KEY = "access_auth_setup_done"

_PBKDF2_ROUNDS = 200_000
_SESSION_TTL_SECONDS = 14 * 24 * 3600
_lock = threading.Lock()


def _env_password() -> str:
    for key in ("APP_PASSWORD", "MANAGEMENT_PASSWORD"):
        value = str(os.environ.get(key) or "").strip()
        if value:
            return value
    return ""


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii")


def _b64d(text: str) -> bytes:
    return base64.urlsafe_b64decode(text.encode("ascii"))


def _hash_password(password: str, salt: bytes) -> str:
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt,
        _PBKDF2_ROUNDS,
    )
    return _b64e(digest)


def has_stored_password() -> bool:
    return bool(str(config_store.get(HASH_KEY) or "").strip())


def password_configured() -> bool:
    """True when panel must require login (env bootstrap or stored hash)."""
    return bool(_env_password() or has_stored_password())


def setup_required() -> bool:
    """True when first-run init UI should be shown."""
    return not password_configured()


def set_password(password: str, *, force: bool = False) -> None:
    """Persist a new panel password hash.

    ``force=False`` only allows writing when no password is configured yet
    (first-run setup). Pass ``force=True`` for future change-password flows.
    """
    password = str(password or "")
    if len(password) < 6:
        raise ValueError("密码至少 6 位")
    if len(password) > 128:
        raise ValueError("密码过长")
    if not force and not setup_required():
        raise ValueError("访问密码已设置，请直接登录")
    salt = secrets.token_bytes(16)
    digest = _hash_password(password, salt)
    with _lock:
        config_store.set_many(
            {
                SALT_KEY: _b64e(salt),
                HASH_KEY: digest,
                SETUP_DONE_KEY: "1",
            }
        )


def verify_password(password: str) -> bool:
    candidate = str(password or "")
    if not candidate:
        return False
    env = _env_password()
    if env and hmac.compare_digest(candidate, env):
        return True
    salt_raw = str(config_store.get(SALT_KEY) or "").strip()
    digest = str(config_store.get(HASH_KEY) or "").strip()
    if not salt_raw or not digest:
        return False
    try:
        salt = _b64d(salt_raw)
    except Exception:
        return False
    return hmac.compare_digest(_hash_password(candidate, salt), digest)


def _load_sessions() -> dict[str, Any]:
    raw = config_store.get(SESSIONS_KEY, "{}")
    try:
        data = json.loads(raw or "{}")
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _save_sessions(sessions: dict[str, Any]) -> None:
    config_store.set(SESSIONS_KEY, json.dumps(sessions, ensure_ascii=False))


def _purge_expired(sessions: dict[str, Any]) -> dict[str, Any]:
    now = time.time()
    return {
        token: meta
        for token, meta in sessions.items()
        if isinstance(meta, dict) and float(meta.get("exp") or 0) > now
    }


def create_session() -> str:
    token = secrets.token_urlsafe(32)
    now = time.time()
    with _lock:
        sessions = _purge_expired(_load_sessions())
        sessions[token] = {
            "exp": now + _SESSION_TTL_SECONDS,
            "created_at": now,
        }
        # Cap stored sessions to avoid unbounded growth.
        if len(sessions) > 50:
            ordered = sorted(sessions.items(), key=lambda item: float(item[1].get("exp") or 0))
            sessions = dict(ordered[-50:])
        _save_sessions(sessions)
    return token


def revoke_session(token: str) -> None:
    token = str(token or "").strip()
    if not token:
        return
    with _lock:
        sessions = _load_sessions()
        if token in sessions:
            sessions.pop(token, None)
            _save_sessions(sessions)


def validate_session_token(token: str) -> bool:
    token = str(token or "").strip()
    if not token:
        return False
    # Backward-compatible: clients that still store the raw password as token.
    if verify_password(token):
        return True
    with _lock:
        sessions = _purge_expired(_load_sessions())
        meta = sessions.get(token)
        if not isinstance(meta, dict):
            # Persist purge if needed.
            if len(sessions) != len(_load_sessions()):
                _save_sessions(sessions)
            return False
        if float(meta.get("exp") or 0) <= time.time():
            sessions.pop(token, None)
            _save_sessions(sessions)
            return False
        # Opportunistic purge write-back.
        current = _load_sessions()
        if len(sessions) != len(current):
            _save_sessions(sessions)
        return True


def extract_bearer(authorization: str | None) -> str:
    header = str(authorization or "").strip()
    if header.lower().startswith("bearer "):
        return header[7:].strip()
    return ""


def auth_status() -> dict[str, Any]:
    return {
        "setup_required": setup_required(),
        "required": password_configured(),
        "has_env_password": bool(_env_password()),
        "has_stored_password": has_stored_password(),
    }

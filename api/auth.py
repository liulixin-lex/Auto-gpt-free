from __future__ import annotations

from fastapi import APIRouter, Header, Request, Response
from pydantic import BaseModel, Field

from core.access_auth import (
    auth_status,
    create_session,
    extract_bearer,
    password_configured,
    revoke_session,
    set_password,
    setup_required,
    validate_session_token,
    verify_password,
)

router = APIRouter(prefix="/auth", tags=["auth"])


class LoginRequest(BaseModel):
    password: str = ""


class SetupRequest(BaseModel):
    password: str = Field(default="", min_length=1)
    password_confirm: str = ""


@router.get("/check")
def auth_check(authorization: str | None = Header(default=None)):
    """Return whether setup/login is required and whether current token is valid."""
    status = auth_status()
    token = extract_bearer(authorization)
    authenticated = bool(token and password_configured() and validate_session_token(token))
    return {
        **status,
        "authenticated": authenticated,
    }


@router.post("/setup")
def auth_setup(body: SetupRequest, response: Response):
    """First-run: set the panel access password (only when not yet configured)."""
    if not setup_required():
        return {"ok": False, "error": "访问密码已设置，请直接登录"}

    password = str(body.password or "")
    confirm = str(body.password_confirm or "")
    if password != confirm:
        return {"ok": False, "error": "两次输入的密码不一致"}
    try:
        set_password(password)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}

    token = create_session()
    response.set_cookie(
        key="_auth",
        value=token,
        httponly=True,
        samesite="lax",
        max_age=14 * 24 * 3600,
        path="/",
    )
    return {
        "ok": True,
        "token": token,
        "message": "初始访问密码已设置",
    }


@router.post("/login")
def auth_login(body: LoginRequest, response: Response):
    if setup_required():
        return {"ok": False, "error": "请先完成初始密码设置", "code": "setup_required"}
    if not password_configured():
        return {"ok": True, "token": ""}
    if not verify_password(body.password):
        return {"ok": False, "error": "密码错误"}

    token = create_session()
    response.set_cookie(
        key="_auth",
        value=token,
        httponly=True,
        samesite="lax",
        max_age=14 * 24 * 3600,
        path="/",
    )
    return {"ok": True, "token": token}


@router.post("/logout")
def auth_logout(
    request: Request,
    response: Response,
    authorization: str | None = Header(default=None),
):
    token = extract_bearer(authorization)
    if not token:
        token = str(request.cookies.get("_auth") or "").strip()
    revoke_session(token)
    response.delete_cookie("_auth", path="/")
    return {"ok": True}

"""Bearer-token authentication middleware for the management panel.

When a panel password is configured (first-run setup or APP_PASSWORD env),
every ``/api/*`` request must carry a valid session token:

  - Header ``Authorization: Bearer <session_token>``
  - Cookie ``_auth=<session_token>``

Public endpoints: health/ready and auth bootstrap (check / setup / login).
Static UI assets remain public so the init/login pages can load.
"""

from __future__ import annotations

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware

from core.access_auth import extract_bearer, password_configured, setup_required, validate_session_token

_PUBLIC_EXACT = {
    "/api/auth/check",
    "/api/auth/setup",
    "/api/auth/login",
    "/api/auth/logout",
}
_PUBLIC_PREFIXES = (
    "/api/health",
    "/api/ready",
)


class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        path = request.url.path

        if not path.startswith("/api"):
            return await call_next(request)

        if path in _PUBLIC_EXACT or any(path.startswith(p) for p in _PUBLIC_PREFIXES):
            return await call_next(request)

        if setup_required():
            return Response(
                content='{"detail":"Setup required","code":"setup_required"}',
                status_code=401,
                media_type="application/json",
            )

        if not password_configured():
            return await call_next(request)

        token = extract_bearer(request.headers.get("authorization"))
        if not token:
            token = str(request.cookies.get("_auth") or "").strip()
        # EventSource cannot set Authorization headers; allow ?token= for log streams.
        if not token:
            token = str(request.query_params.get("token") or "").strip()

        if token and validate_session_token(token):
            return await call_next(request)

        return Response(
            content='{"detail":"Unauthorized"}',
            status_code=401,
            media_type="application/json",
        )
